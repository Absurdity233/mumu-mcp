"""Device and app state: what is on screen, what is running, and the knobs.

Everything here was verified against a live MuMu 6.8.x / Android 15 instance,
because several obvious approaches do not work on this image:

* `cmd clipboard` does not exist (`No shell command implementation`), and
  `dumpsys clipboard` prints nothing useful — so there is no clipboard tool here.
  Reading or setting the clipboard needs an instrumentation or an IME, and
  pretending otherwise would just return empty strings.
* `screenrecord` **does** work (a 2 s capture produced a 1.1 MB mp4).
* `settings put global http_proxy` works and is what apps actually read.
* `/system/etc/hosts` is writable through the root overlay, so DNS can be pinned.

The design rule is the same as the rest of the package: one call that answers a
question, rather than four calls whose output the model has to reconcile.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .adb import Adb
from .errors import AdbUnavailable, BadArgument, MuMuError, Timeout
from .manager import Manager

log = logging.getLogger("mumu_mcp.device")

ACTIVITY_RE = re.compile(
    r"(?:topResumedActivity|mResumedActivity)=[A-Za-z]+\{[^}\n]*?\su0\s+([\w.]+)/([\w.$]+)"
)
FOCUS_RE = re.compile(r"mCurrentFocus=[A-Za-z]+\{\S+\su0\s+([\w.]+)/([\w.$]+)")
BATTERY_FIELD = re.compile(r"^\s+(\w[\w ]*?):\s*(.+)$", re.MULTILINE)


@dataclass
class Foreground:
    """The app in front of the user right now."""

    package: str
    activity: str

    @property
    def component(self) -> str:
        return f"{self.package}/{self.activity}" if self.activity else self.package

    def to_dict(self) -> dict[str, Any]:
        return {"package": self.package, "activity": self.activity, "component": self.component}


def _battery_fields(dump: str) -> dict[str, Any]:
    """Parse `dumpsys battery` into a flat dict, numeric where it obviously is."""
    fields: dict[str, Any] = {}
    for m in BATTERY_FIELD.finditer(dump):
        key = m.group(1).strip().lower().replace(" ", "_")
        raw = m.group(2).strip()
        fields[key] = int(raw) if re.fullmatch(r"-?\d+", raw) else raw
    return fields


def _parse_ps(out: str) -> list[dict[str, Any]]:
    """Parse `ps -A -o PID,USER,NAME,%CPU,%MEM`, sorted by CPU descending.

    `NAME` is the second-to-last-ish column and may contain spaces, so the two
    numeric columns are taken from the end rather than by a fixed index.
    """
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        cpu, mem = parts[-2], parts[-1]
        if not re.fullmatch(r"\d+(?:\.\d+)?", cpu):
            continue
        rows.append({
            "pid": int(parts[0]),
            "user": parts[1],
            "name": " ".join(parts[2:-2]),
            "cpu_pct": float(cpu),
            "mem_pct": float(mem) if re.fullmatch(r"\d+(?:\.\d+)?", mem) else None,
        })
    rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
    return rows


class Device:
    """Read and adjust device-level state through the guest shell."""

    def __init__(self, adb: Adb, manager: Manager):
        self.adb = adb
        self.manager = manager

    # -- foreground --------------------------------------------------------
    def foreground(self, vmindex: int = 0) -> Foreground:
        """Resolve the top activity, falling back to the focused window.

        `mResumedActivity` and `mCurrentFocus` disagree during transitions and on
        some launchers, so both are read and the first hit wins.
        """
        out = self.adb.shell(
            vmindex,
            "dumpsys activity activities 2>/dev/null | grep -E "
            "'(mResumedActivity|topResumedActivity)' | head -3; "
            "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp' | head -3",
            timeout=25,
        )
        for regex in (ACTIVITY_RE, FOCUS_RE):
            m = regex.search(out)
            if m:
                return Foreground(package=m.group(1), activity=m.group(2))
        # Last resort: `dumpsys window` prints `mFocusedApp=... pkg/act tN`.
        loose = re.search(r"([\w.]+)/([\w.$]+)\s+t\d+", out)
        if loose:
            return Foreground(package=loose.group(1), activity=loose.group(2))
        raise AdbUnavailable("could not determine the foreground activity",
                             detail={"output": out[:400]})

    def wait_for_activity(self, vmindex: int, target: str, *, timeout: float = 30.0,
                          poll: float = 0.6) -> Foreground:
        """Block until the foreground package (or component) matches `target`.

        `target` may be a package, a `pkg/activity` component, or a regex.
        """
        if not target:
            raise BadArgument("target is required")
        pattern = re.compile(target)
        deadline = time.monotonic() + max(1.0, timeout)
        seen = ""
        while time.monotonic() < deadline:
            try:
                fg = self.foreground(vmindex)
            except MuMuError:
                time.sleep(poll)
                continue
            seen = fg.component
            if pattern.search(fg.package) or pattern.search(fg.component):
                return fg
            time.sleep(poll)
        raise Timeout(
            f"foreground never matched {target!r} in {timeout}s",
            detail={"last_foreground": seen},
        )

    # -- launching ---------------------------------------------------------
    def launch(self, vmindex: int, component: str = "", package: str = "",
               activity: str = "", action: str = "", data: str = "",
               extras: dict[str, str] | None = None, wait: bool = True,
               force_stop: bool = False) -> dict[str, Any]:
        """Start an activity with `am start`, optionally measuring the launch.

        `am start -W` reports `TotalTime`/`WaitTime` and whether the app was
        already running, which is the difference between a cold and a warm launch
        — worth having when you are timing an app rather than just poking it.
        """
        if not component:
            if package and activity:
                component = f"{package}/{activity}" if "/" in activity or activity.startswith(".") \
                    else f"{package}/{package}.{activity}"
            elif package:
                component = package
        if not (component or action):
            raise BadArgument("give component, package(+activity), or action")

        args = ["am", "start"]
        if wait:
            args.append("-W")
        if force_stop:
            args.append("-S")
        if action:
            args += ["-a", action]
        if data:
            args += ["-d", data]
        for key, value in (extras or {}).items():
            args += ["--es", key, value]
        if component:
            args += ["-n", component]

        out = self.adb.shell(vmindex, " ".join(args), timeout=60)
        payload: dict[str, Any] = {
            "component": component or None,
            "action": action or None,
            "raw": out.strip()[:1500],
        }
        for field in ("Status", "LaunchState", "Activity", "TotalTime", "WaitTime", "Complete"):
            m = re.search(rf"{field}:\s*(.+)", out)
            if m:
                payload[field.lower()] = m.group(1).strip()
        if payload.get("totalTime", "").isdigit():
            payload["total_ms"] = int(payload["totalTime"])
        already = "intent has been delivered to currently running top-most instance" in out
        payload["already_running"] = already
        if "Error" in out or "Exception" in out:
            raise BadArgument("am start failed", detail={"output": out.strip()[:1200]})
        return payload

    # -- device state ------------------------------------------------------
    def battery(self, vmindex: int = 0) -> dict[str, Any]:
        return _battery_fields(self.adb.shell(vmindex, "dumpsys battery", timeout=20))

    def set_battery(self, vmindex: int = 0, level: int | None = None,
                    status: int | None = None, temperature: int | None = None,
                    reset: bool = False) -> dict[str, Any]:
        """Override the reported battery — `reset=True` hands control back to the guest."""
        if reset:
            self.adb.shell(vmindex, "dumpsys battery reset", timeout=20)
            return {"reset": True, "battery": self.battery(vmindex)}
        if level is None and status is None and temperature is None:
            raise BadArgument("give level/status/temperature, or reset=True")
        cmds = []
        if level is not None:
            if not 0 <= level <= 100:
                raise BadArgument("level must be in [0, 100]")
            cmds.append(f"dumpsys battery set level {level}")
        if status is not None:
            if not 1 <= status <= 5:
                raise BadArgument("status: 1 unknown, 2 charging, 3 discharging, 4 not charging, 5 full")
            cmds.append(f"dumpsys battery set status {status}")
        if temperature is not None:
            cmds.append(f"dumpsys battery set temp {temperature}")
        self.adb.shell(vmindex, "; ".join(cmds), timeout=20)
        return {"applied": {"level": level, "status": status, "temperature": temperature},
                "battery": self.battery(vmindex)}

    # -- network knobs -----------------------------------------------------
    def proxy(self, vmindex: int = 0) -> dict[str, Any]:
        """Read the global HTTP proxy apps see (`settings global http_proxy`)."""
        raw = self.adb.shell(vmindex, "settings get global http_proxy", timeout=20).strip()
        active = bool(raw) and raw not in {"null", ":0"}
        return {"proxy": raw if active else None, "active": active}

    def set_proxy(self, vmindex: int = 0, host: str = "", port: int = 0) -> dict[str, Any]:
        """Point every app at an HTTP proxy — the usual way to inspect traffic.

        Clearing is `host=""` (sets `:0`, which Android treats as no proxy).
        """
        if not host:
            self.adb.shell(vmindex, "settings put global http_proxy :0", timeout=20)
            return {"proxy": None, "active": False, "cleared": True}
        if not 1 <= port <= 65535:
            raise BadArgument("port must be in [1, 65535]")
        value = f"{host}:{port}"
        self.adb.shell(vmindex, f"settings put global http_proxy {value}", timeout=20)
        return {"proxy": value, "active": True, "read_back": self.proxy(vmindex)}

    # -- hosts file --------------------------------------------------------
    def hosts(self, vmindex: int = 0) -> dict[str, Any]:
        out = self.adb.shell(vmindex, "cat /system/etc/hosts", timeout=20)
        entries = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                entries.append({"ip": parts[0], "hosts": parts[1:]})
        return {"raw": out, "entries": entries}

    def add_hosts(self, vmindex: int, entries: dict[str, str], *, replace: bool = False,
                  persist: bool = True) -> dict[str, Any]:
        """Append `ip -> hostname` mappings to `/system/etc/hosts`.

        Needs the root overlay (`mumu_root_system(True)`), because the file lives
        on the read-only system partition. `persist=True` keeps them across
        reboots; the overlay diff image is what makes that work.
        """
        if not entries:
            raise BadArgument("entries is required, e.g. {'10.0.2.2': 'api.example.com'}")
        state = self.adb.shell(vmindex, "[ -w /system/etc/hosts ] && echo w || echo ro").strip()
        if state != "w":
            raise BadArgument(
                "/system/etc/hosts is read-only; call mumu_root_system(True) first",
                detail={"hint": "that enables the overlay, which makes /system writable"},
            )
        lines = []
        if replace:
            lines.append("127.0.0.1\tlocalhost")
            lines.append("::1\tip6-localhost")
        for ip, host in entries.items():
            lines.append(f"{ip}\t{host}")
        joined = " ".join(entries.values())
        if replace:
            script = "printf '%s\\n' " + " ".join(f"'{ln}'" for ln in lines) + " > /system/etc/hosts"
        else:
            script = ("printf '%s\\n' " + " ".join(f"'{ln}'" for ln in lines)
                      + " >> /system/etc/hosts")
        self.adb.shell(vmindex, script, timeout=30)
        check = self.hosts(vmindex)
        applied = [e for e in check["entries"] if set(e["hosts"]) & set(joined.split())]
        return {
            "applied": entries,
            "verified": applied,
            "persist": persist,
            "note": "written through the root overlay; mumu_root_restore_system undoes it",
        }

    # -- storage / processes ----------------------------------------------
    def storage(self, vmindex: int = 0) -> dict[str, Any]:
        out = self.adb.shell(vmindex, "df -k /data /storage/emulated 2>/dev/null", timeout=20)
        rows = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 6 and parts[1].isdigit():
                total, used, avail = (int(parts[1]) * 1024, int(parts[2]) * 1024,
                                      int(parts[3]) * 1024)
                rows.append({"mount": parts[-1], "total": total, "used": used,
                             "available": avail,
                             "used_pct": round(used / total * 100, 1) if total else 0})
        return {"filesystems": rows}

    def processes(self, vmindex: int = 0, limit: int = 15) -> dict[str, Any]:
        """Top processes by CPU, plus load average and core count.

        Uses `ps -A -o PID,USER,NAME,%CPU,%MEM` rather than `top -o`: on this
        image `top -o %CPU` emits only the CPU column with no PID or name, which
        parses into nothing. `ps` has a stable, named column layout.
        """
        out = self.adb.shell(vmindex, "ps -A -o PID,USER,NAME,%CPU,%MEM 2>/dev/null", timeout=25)
        load = self.adb.shell(vmindex, "cat /proc/loadavg; nproc", timeout=15).splitlines()
        rows = _parse_ps(out)
        return {
            "loadavg": load[0].strip() if load else "",
            "cores": int(load[1]) if len(load) > 1 and load[1].strip().isdigit() else None,
            "process_count": len(rows),
            "top": rows[:max(1, limit)],
        }

    # -- one-call summary --------------------------------------------------
    def summary(self, vmindex: int = 0) -> dict[str, Any]:
        """Everything an agent usually needs before acting, in one call.

        Cheaper than five separate tools and, more importantly, internally
        consistent: the foreground app and the screen size come from the same
        moment.
        """
        out: dict[str, Any] = {"vmindex": vmindex}
        errors: dict[str, str] = {}

        try:
            out["foreground"] = self.foreground(vmindex).to_dict()
        except MuMuError as exc:
            errors["foreground"] = str(exc)
        try:
            size = self.adb.shell(vmindex, "wm size", timeout=15).strip().splitlines()[-1]
            density = self.adb.shell(vmindex, "wm density", timeout=15).strip().splitlines()[-1]
            out["screen"] = {
                "size": size.split(":")[-1].strip(),
                "density": density.split(":")[-1].strip(),
            }
        except MuMuError as exc:
            errors["screen"] = str(exc)
        try:
            out["battery"] = self.battery(vmindex)
        except MuMuError as exc:
            errors["battery"] = str(exc)
        try:
            ident = self.adb.shell(vmindex, "id", timeout=15)
            out["adbd_uid"] = 0 if "uid=0(" in ident else 2000
        except MuMuError as exc:
            errors["adbd_uid"] = str(exc)
        try:
            out["proxy"] = self.proxy(vmindex)
        except MuMuError as exc:
            errors["proxy"] = str(exc)
        try:
            out["storage"] = self.storage(vmindex)["filesystems"][:1]
        except MuMuError as exc:
            errors["storage"] = str(exc)
        if errors:
            out["errors"] = errors
        return out
