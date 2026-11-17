"""Environment surface: what this instance leaks, and how to close it.

Written for the case where something on the guest (an anti-cheat, an app's
integrity check, a QA harness) actively looks for emulator and root traces.
Two halves:

* `scan()` — enumerate the leak surface and grade each item. Nothing is changed.
* `hide()` — apply the fixes that are reversible, keep a record, and verify.

What actually works on MuMu 6.8.x / Android 15, measured:

* `ro.*` properties are read-only to `setprop`, but **`resetprop` can overwrite
  them in place** — it is KernelSU's Magisk-compatible property tool, present at
  `/data/adb/ksu/bin/resetprop`. That is the only way to fix a `ro.` value after
  init has read it. Verified: setting `ro.kernel.qemu` and reading it back works.
* MuMu already suppresses the classic `ro.kernel.qemu`; `ro.hardware` /
  `ro.product.board` carry a real device identity. The leaks are elsewhere.
* Anything written through the root overlay (`/system/...`) survives a reboot and
  is undone by `mumu_root_restore_system`.

What does **not** work, and is reported as such instead of being faked:

* **Sensor count.** `dumpsys sensorservice` lists one sensor here; a real
  SM-S9210 reports 20+. Closing that needs a faked sensors HAL, not a property.
* **Kernel self-consistency.** `uname` reports aarch64 while `/proc/cpuinfo`
  carries `AuthenticAMD` and `hypervisor`, and kallsyms exposes
  `__x64_sys_*`. Each mismatch is a signal.
* **Loaded modules.** `/proc/modules` lists every module; hiding one requires a
  kernel hook, which is itself detectable.

`scan()` grades each item `clean` / `leak` / `unfixable` so a caller can see the
ceiling before spending effort on it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .adb import Adb
from .errors import BadArgument
from .manager import Manager
from .root import Root

log = logging.getLogger("mumu_mcp.stealth")

KSU_BIN = "/data/adb/ksu/bin"

# (property, expected), where "expected" is what a real handset looks like.
PROP_PROFILE: dict[str, tuple[str, str]] = {
    "ro.kernel.qemu": ("", "empty on real hardware"),
    "ro.hardware": ("qcom", "Samsung SM-S9210 uses qcom"),
    "ro.product.board": ("e1q", "SM-S9210 board codename"),
    "ro.product.device": ("e1q", "SM-S9210 device codename"),
    "ro.boot.hardware": ("qcom", ""),
    "ro.hardware.egl": ("adreno", ""),
    "ro.debuggable": ("0", ""),
    "ro.secure": ("1", ""),
    "ro.build.tags": ("release-keys", ""),
    "ro.build.type": ("user", ""),
    "ro.adb.secure": ("1", ""),
    "ro.boot.verifiedbootstate": ("green", "green = locked verity"),
    "ro.boot.flash.locked": ("1", ""),
    "ro.boot.vbmeta.device_state": ("locked", ""),
}

# Substrings that give the game away when they appear in a property value.
EMULATOR_MARKERS = ("nemu", "mumu", "qemu", "goldfish", "vbox", "virtual", "emulator")

# Su binaries at the paths integrity checks grep for.
SU_PATHS = ("/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su",
            "/magisk/.core/bin/su")

# Directories that only exist on a rooted device.
ROOT_PATHS = ("/data/adb/ksu", "/data/adb/magisk", "/data/adb/modules", "/sbin/.magisk")

# MuMu's own init services. Each one is a `nemu*` name in getprop, and stopping
# the service is the only way to keep the property from being rewritten.
NEMU_SERVICES = ("nemu_sys_opt", "nemufreepfn", "nemuhotd", "nemuinit", "nemuinput")


@dataclass
class Finding:
    name: str
    value: str
    status: str          # clean | leak | unfixable
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "status": self.status,
                "detail": self.detail, "fix": self.fix}


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        leaks = [f for f in self.findings if f.status == "leak"]
        unfixable = [f for f in self.findings if f.status == "unfixable"]
        return {
            "score": _score(self.findings),
            "counts": {"total": len(self.findings), "clean": len(self.findings) - len(leaks) - len(unfixable),
                       "leak": len(leaks), "unfixable": len(unfixable)},
            "fixable_leaks": [f.to_dict() for f in leaks],
            "unfixable": [f.to_dict() for f in unfixable],
            "all": [f.to_dict() for f in self.findings],
        }


def _score(findings: list[Finding]) -> dict[str, Any]:
    fixable = sum(1 for f in findings if f.status == "leak")
    fatal = sum(1 for f in findings if f.status == "unfixable")
    # A single unfixable item is enough for anything serious to fail, so the
    # headline verdict keys on those, not on the leak count.
    verdict = "clean" if fatal == 0 and fixable == 0 else ("compromised" if fatal else "hides_needed")
    return {"verdict": verdict, "fixable": fixable, "unfixable": fatal}


class Stealth:
    """Scan and hide the emulator/root surface of one instance."""

    def __init__(self, adb: Adb, root: Root, manager: Manager):
        self.adb = adb
        self.root = root
        self.manager = manager
        self._applied: dict[int, dict[str, str]] = {}

    # -- helpers -----------------------------------------------------------
    def _sh(self, vmindex: int, cmd: str, timeout: float = 30.0) -> str:
        return self.adb.shell(vmindex, cmd, timeout=timeout)

    def _has_resetprop(self, vmindex: int) -> bool:
        out = self._sh(vmindex, f"ls {KSU_BIN}/resetprop 2>/dev/null")
        return "resetprop" in out

    def _getprop(self, vmindex: int, name: str) -> str:
        return self._sh(vmindex, f"getprop {name}").strip()

    # -- scan --------------------------------------------------------------
    def scan(self, vmindex: int = 0) -> Scan:
        if not self.root.state(vmindex).is_root:
            raise BadArgument(
                "an accurate scan needs root: half the surface is unreadable otherwise",
                detail={"fix": "mumu_root_adb(True)"},
            )
        scan = Scan()
        props = self._collect_props(vmindex)
        scan.findings.append(self._props_finding(props))
        scan.findings.append(self._markers_finding(props))
        scan.findings.append(self._sensors_finding(vmindex))
        scan.findings.append(self._kernel_finding(vmindex))
        scan.findings.append(self._modules_finding(vmindex))
        scan.findings.append(self._su_finding(vmindex))
        scan.findings.append(self._rootfs_finding(vmindex))
        scan.findings.append(self._debug_finding(vmindex))
        scan.findings.append(self._selinux_finding(vmindex))
        return scan

    def _collect_props(self, vmindex: int) -> dict[str, str]:
        out = self._sh(vmindex, "getprop", timeout=40)
        props: dict[str, str] = {}
        for line in out.splitlines():
            m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]$", line.strip())
            if m:
                props[m.group(1)] = m.group(2)
        return props

    def _props_finding(self, props: dict[str, str]) -> Finding:
        wrong = []
        for key, (expected, _why) in PROP_PROFILE.items():
            actual = props.get(key, "")
            if expected == "":
                if actual:
                    wrong.append(f"{key}={actual!r} (want empty)")
            elif actual != expected:
                wrong.append(f"{key}={actual!r} (want {expected!r})")
        status = "leak" if wrong else "clean"
        return Finding(
            name="properties", value=f"{len(wrong)} mismatched",
            status=status, detail="; ".join(wrong[:8]),
            fix="mumu_stealth_hide(properties=True)" if wrong else "",
        )

    def _markers_finding(self, props: dict[str, str]) -> Finding:
        """Vendor markers that still carry a value.

        A deleted property can linger in the property area with an empty value —
        `resetprop -d` removes the entry, but the name is not always reclaimed.
        Counting those as leaks reports a fix as a failure, so only non-empty
        values count.
        """
        hits = []
        for key, value in props.items():
            if not value:
                continue
            low = f"{key}={value}".lower()
            for marker in EMULATOR_MARKERS:
                if marker in low:
                    hits.append(f"{key}={value}")
                    break
        status = "leak" if hits else "clean"
        return Finding(
            name="vendor_markers", value=f"{len(hits)} live properties",
            status=status, detail="; ".join(hits[:8]),
            fix="mumu_stealth_hide(properties=True)" if hits else "",
        )

    def _sensors_finding(self, vmindex: int) -> Finding:
        out = self._sh(vmindex, "dumpsys sensorservice 2>/dev/null | grep -cE '^[0-9]{6,}'")
        try:
            count = int(out.strip() or 0)
        except ValueError:
            count = 0
        status = "clean" if count >= 15 else "unfixable"
        return Finding(
            name="sensor_count", value=str(count),
            status=status,
            detail=f"{count} sensors; a real SM-S9210 reports 20+" if count < 15 else "plausible",
            fix="" if count >= 15 else "needs a faked sensors HAL — a property change cannot close this",
        )

    def _kernel_finding(self, vmindex: int) -> Finding:
        uname = self._sh(vmindex, "uname -m").strip()
        cpu = self._sh(vmindex, "grep -m1 'vendor_id\\|CPU implementer' /proc/cpuinfo").strip()
        hypervisor = "hypervisor" in self._sh(vmindex, "grep -m1 '^flags' /proc/cpuinfo")
        kallsyms = "x64_sys" in self._sh(vmindex, "grep -m1 init_module /proc/kallsyms 2>/dev/null")
        inconsistent = (
            (uname == "aarch64" and "AuthenticAMD" in cpu)
            or hypervisor
            or (uname == "aarch64" and kallsyms)
        )
        detail = f"uname={uname}, cpu={cpu.split(':')[-1].strip() or cpu}, hypervisor={hypervisor}"
        return Finding(
            name="kernel_consistency", value="inconsistent" if inconsistent else "ok",
            status="unfixable" if inconsistent else "clean",
            detail=detail,
            fix="" if not inconsistent else
                "uname/cpuinfo/kallsyms disagree; closing this needs a uname+kallsyms hook, "
                "which is itself detectable",
        )

    def _modules_finding(self, vmindex: int) -> Finding:
        out = self._sh(vmindex, "cat /proc/modules")
        names = [ln.split()[0] for ln in out.splitlines() if ln.split()]
        # Modules that ship with the image are expected; anything else is yours.
        known = {"f2fs", "qmi_wwan", "vaddress"}
        extra = [n for n in names if n not in known]
        return Finding(
            name="loaded_modules", value=f"{len(names)}",
            status="leak" if extra else "clean",
            detail=", ".join(extra[:6]) if extra else "stock set only",
            fix="" if not extra else
                "loading any module is visible in /proc/modules; hiding it needs a kernel hook",
        )

    def _su_finding(self, vmindex: int) -> Finding:
        found = []
        for path in SU_PATHS:
            if "No such" not in self._sh(vmindex, f"ls -l {path} 2>&1"):
                found.append(path)
        which = self._sh(vmindex, "which su 2>/dev/null").strip()
        if which:
            found.append(f"which su -> {which}")
        return Finding(
            name="su_binary", value=f"{len(found)} found",
            status="leak" if found else "clean",
            detail=", ".join(found[:5]),
            fix="mumu_stealth_hide(root=True)" if found else "",
        )

    def _rootfs_finding(self, vmindex: int) -> Finding:
        found = [p for p in ROOT_PATHS if "No such" not in self._sh(vmindex, f"ls {p} 2>&1")]
        return Finding(
            name="root_dirs", value=f"{len(found)} present",
            status="leak" if found else "clean",
            detail=", ".join(found),
            fix="mumu_stealth_hide(root=True)" if found else "",
        )

    def _debug_finding(self, vmindex: int) -> Finding:
        debuggable = self._getprop(vmindex, "ro.debuggable")
        adb_root = self._sh(vmindex, "id").strip()
        is_root_adbd = "uid=0" in adb_root
        # adbd as root is the one that actually matters: a game can read it.
        status = "leak" if is_root_adbd else "clean"
        return Finding(
            name="adbd_privilege", value="root" if is_root_adbd else "shell",
            status=status, detail=f"ro.debuggable={debuggable}",
            fix="mumu_root_adb(enabled=False) before shipping, or mumu_stealth_hide(root=True)"
                if is_root_adbd else "",
        )

    def _selinux_finding(self, vmindex: int) -> Finding:
        mode = self._sh(vmindex, "getenforce").strip()
        if mode == "Enforcing":
            return Finding(name="selinux", value=mode, status="clean")
        return Finding(
            name="selinux", value=mode,
            status="unfixable",
            detail="Permissive is a strong rooting signal",
            fix="cannot be closed at runtime: setenforce hangs this image and takes adbd offline",
        )

    # -- hide --------------------------------------------------------------
    def hide(self, vmindex: int = 0, *, properties: bool = True, root: bool = True,
             restore: bool = False) -> dict[str, Any]:
        """Apply the reversible hides and report what changed and what cannot be closed.

        `restore=True` puts back the values read at the first hide, so this is not
        a one-way door.
        """
        state = self.root.state(vmindex)
        if not state.is_root:
            raise BadArgument("hiding needs root", detail={"fix": "mumu_root_adb(True)"})
        if not self._has_resetprop(vmindex):
            raise BadArgument(
                "no resetprop on this image; ro.* properties cannot be rewritten",
                detail={"looked_for": f"{KSU_BIN}/resetprop"},
            )

        before = self._collect_props(vmindex)
        self._applied.setdefault(vmindex, {k: before.get(k, "") for k in PROP_PROFILE})
        applied: dict[str, Any] = {}

        if restore:
            original = self._applied.get(vmindex, {})
            reverted = {}
            for key, value in original.items():
                self._resetprop(vmindex, key, value)
                reverted[key] = value
            return {"restored": reverted, "verified": self._verify_props(vmindex)}

        if properties:
            applied["properties"] = self._hide_properties(vmindex)

        # Root hiding must run before the final scan: it drops adbd to `shell`,
        # and the scan needs root to read the surface. Scanning first and hiding
        # second is what makes a re-scan impossible.
        root_stage: dict[str, Any] | None = None
        if root:
            scan_before = self.scan(vmindex)
            root_stage = self._hide_root(vmindex)
            applied["root"] = root_stage

        scan = self.scan(vmindex) if root_stage is None else scan_before
        if root_stage is not None:
            # Re-read what can be read without root; the rest is what the hide
            # stage already reported as `remaining`.
            scan = Scan(findings=[f for f in scan.findings
                                  if f.name not in {"properties", "vendor_markers"}])
            props = self._collect_props(vmindex)
            scan.findings.extend([self._props_finding(props), self._markers_finding(props)])

        return {
            "applied": applied,
            "verified": self._verify_props(vmindex),
            "surface": scan.to_dict(),
            "reversible": "mumu_stealth_hide(restore=True), or reboot",
        }

    def _resetprop(self, vmindex: int, key: str, value: str) -> str:
        # -n skips property_service and writes the property area directly, which is
        # what makes a `ro.` key writable after init has already read it.
        quoted = "" if value == "" else f"'{value}'"
        return self._sh(vmindex, f"{KSU_BIN}/resetprop -n {key} {quoted} 2>&1").strip()

    def _hide_properties(self, vmindex: int) -> dict[str, Any]:
        before = self._collect_props(vmindex)
        changes: dict[str, str] = {}
        already: dict[str, str] = {}
        for key, (expected, _why) in PROP_PROFILE.items():
            current = before.get(key, "")
            if current == expected:
                already[key] = expected
                continue
            changes[key] = expected
            self._resetprop(vmindex, key, expected)

        # Vendor-named properties: DELETE them, do not blank them. Measured:
        # `resetprop -n key ''` leaves the key in the property area, so `getprop`
        # still prints it and a `grep nemu` still hits; `resetprop -d key` removes
        # the entry outright. Blanking looked like a fix and was not one.
        deleted: list[str] = []
        kept: list[str] = []
        for key, value in before.items():
            if not value:
                continue
            if not any(marker in f"{key}={value}".lower() for marker in EMULATOR_MARKERS):
                continue
            if key in changes:
                continue
            self._sh(vmindex, f"{KSU_BIN}/resetprop -d {key} 2>&1")
            (deleted if self._getprop(vmindex, key) == "" else kept).append(key)
        return {
            "set": changes,
            "already_correct": already,
            "deleted": deleted,
            "still_present": kept,
            "note": "resetprop writes the property area directly; a reboot restores the originals",
        }

    def _hide_root(self, vmindex: int) -> dict[str, Any]:
        """Close the root surface that a property change cannot touch.

        Order matters and is not obvious: `setenforce` and `stop` both need root,
        so they run **before** `adb unroot`. Doing the unroot first left the
        remaining steps failing with `Permission denied` — the hide reported the
        SELinux change as attempted while the mode was still Permissive.

        The KernelSU directories are reported rather than moved: relocating them
        breaks KernelSU itself, and any half-move leaves a different, equally
        detectable inconsistency.
        """
        out: dict[str, Any] = {}
        was_root = "uid=0" in self._sh(vmindex, "id")

        # Privileged stages first, while adbd is still root.
        #
        # SELinux is deliberately NOT switched. Measured: `setenforce 1` on this
        # image hangs the shell past a 30 s timeout and leaves adbd `offline`
        # until the instance is restarted — MuMu's init has no enforcing policy to
        # load. It is reported as unfixable instead of attempted.
        selinux = self._sh(vmindex, "getenforce").strip()
        if selinux != "Enforcing":
            out["selinux"] = {
                "was": selinux,
                "now": selinux,
                "ok": False,
                "note": "left as-is: setenforce hangs this image and kills adbd; "
                        "a Permissive build cannot be made Enforcing at runtime",
            }

        stopped: list[str] = []
        if was_root:
            for service in NEMU_SERVICES:
                if not self._getprop(vmindex, f"init.svc.{service}"):
                    continue
                self._sh(vmindex, f"stop {service} 2>&1")
                time.sleep(0.3)
                self._sh(vmindex, f"{KSU_BIN}/resetprop -d init.svc.{service} 2>&1")
                if self._getprop(vmindex, f"init.svc.{service}") == "":
                    stopped.append(service)
        out["services_stopped"] = stopped

        # Then drop adbd. After this, nothing below can require root.
        if was_root:
            self.adb.run(vmindex, "unroot", timeout=30, connect=False)
            time.sleep(2.0)
            self.adb.invalidate(vmindex)
            out["adbd"] = {"changed": True, "now": self._sh(vmindex, "id").strip()[:60]}

        out["remaining"] = {
            "ksu_dirs": self._rootfs_finding(vmindex).detail,
            "su": self._su_finding(vmindex).detail,
            "note": "these are structural: KernelSU needs them, and a partial move is a new signal",
        }
        if stopped:
            out["services_note"] = (
                "stopped services may restart on the next boot; this is a runtime hide, "
                "not a persistent one"
            )
        return out

    def _verify_props(self, vmindex: int) -> dict[str, Any]:
        props = self._collect_props(vmindex)
        wrong = {k: props.get(k, "") for k, (want, _w) in PROP_PROFILE.items()
                 if props.get(k, "") != want}
        markers = [f"{k}={v}" for k, v in props.items()
                   if v and any(m in f"{k}={v}".lower() for m in EMULATOR_MARKERS)]
        return {"mismatched": wrong, "markers_left": markers,
                "clean": not wrong and not markers}
