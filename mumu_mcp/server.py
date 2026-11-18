"""MCP server for MuMu Player (Netease Android emulator).

63 tools over two independent channels:

* **manager** — `MuMuManager.exe`. Lifecycle, config, app management, guest
  shell. Works even when adb is off.
* **adb** — input, screenshots, file transfer, logcat. Needs adbd reachable;
  the endpoint is resolved per instance (`MuMu 12` NAT loopback or `MuMu 6.x`
  bridged LAN).

Every tool returns `{"ok": true, ...}` or `{"ok": false, "error": "<code>", ...}`.
Errors are values, not exceptions: an agent can branch on `error` instead of
parsing prose. Codes are listed in `mumu_mcp/errors.py`.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import constants as C
from .adb import Adb
from .device import Device
from .errors import (
    AdbUnavailable,
    BadArgument,
    MuMuError,
    Timeout,
)
from .flow import Flow, parse_steps
from .gesture import Gestures
from .manager import Manager, as_list
from .root import Root
from .stealth import Stealth
from .uname import UnamePatcher

logging.basicConfig(
    level=os.environ.get("MUMU_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mumu_mcp")

mcp = FastMCP(
    "mumu-mcp",
    instructions=(
        "Control Netease MuMu Player Android emulator. Start with mumu_diagnose to "
        "learn what is installed and which channels are live, then mumu_list. "
        "Prefer mumu_ui_dump + coordinates over blind tapping. All results are "
        "{\"ok\": bool}; on ok=false read the error code."
    ),
)

_state: dict[str, Any] = {"manager": None, "adb": None, "root": None}


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def manager() -> Manager:
    mgr = _state.get("manager")
    if mgr is None:
        mgr = Manager.autodetect(os.environ.get("MUMU_HOME") or None)
        _state["manager"] = mgr
    return mgr


def adb() -> Adb:
    inst = _state.get("adb")
    if inst is None:
        mgr = manager()
        binary = _find_adb(mgr)
        inst = Adb(
            binary=binary,
            manager=mgr,
            explicit=os.environ.get("MUMU_ADB") or None,
        )
        _state["adb"] = inst
    return inst


def root() -> Root:
    inst = _state.get("root")
    if inst is None:
        inst = Root(adb=adb(), manager=manager())
        _state["root"] = inst
    return inst


def _find_adb(mgr: Manager) -> Path:
    for cand in (mgr.home / "adb.exe", mgr.home / "adb"):
        if cand.is_file():
            return cand
    found = shutil.which("adb")
    if found:
        return Path(found)
    raise AdbUnavailable(
        "adb.exe not found next to MuMuManager and not on PATH",
        detail={"searched": str(mgr.home)},
    )


def _wrap(fn):
    """Turn MuMuError into a structured payload; never leak a traceback.

    `functools.wraps` keeps `__wrapped__` so tools can call each other's raw
    implementation without re-catching their own envelope.
    """

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        started = time.monotonic()
        try:
            result = fn(*a, **kw)
        except MuMuError as exc:
            payload = exc.to_dict()
            payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            return payload
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unhandled in %s", fn.__name__)
            return {
                "ok": False,
                "error": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
            }
        if isinstance(result, dict):
            result.setdefault("ok", True)
            result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
            return result
        return {"ok": True, "result": result, "elapsed_ms": int((time.monotonic() - started) * 1000)}

    return wrapper


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_diagnose() -> dict:
    """Report the install, version, instances, adb endpoints and which channels work.

    Run this first. It is the only call that tells you *why* something else would
    fail: no install, no instance, adbd unreachable, read-only setting.
    """
    mgr = manager()
    report: dict[str, Any] = {
        "manager": str(mgr.cli),
        "install_dir": str(mgr.home),
        "version": mgr.version(),
        "channels": {},
        "instances": {},
    }

    try:
        report["channels"]["manager"] = True
    except MuMuError:
        report["channels"]["manager"] = False

    try:
        instances = mgr.instances()
    except MuMuError as exc:
        instances = {}
        report["instances_error"] = exc.to_dict()

    for idx, info in instances.items():
        entry = {
            "name": info.get("name"),
            "state": info.get("player_state"),
            "android": info.get("android_version"),
            "running": info.get("is_android_started"),
            "reported_adb_port": info.get("adb_port"),
            "endpoint": None,
            "adb": False,
        }
        try:
            endpoint = adb().endpoint(int(idx))
            entry["endpoint"] = endpoint
            entry["adb"] = adb().shell_ok(int(idx), "echo ok", timeout=8)
        except MuMuError as exc:
            entry["adb_error"] = exc.to_dict()
        report["instances"][idx] = entry

    report["channels"]["adb"] = any(v.get("adb") for v in report["instances"].values())
    report["hints"] = _hints(report)
    return report


def _hints(report: dict) -> list[str]:
    hints: list[str] = []
    if not report["instances"]:
        hints.append("no instances: mumu_create to make one, then mumu_boot_ready")
    for idx, entry in report["instances"].items():
        if not entry["running"]:
            hints.append(f"index {idx} is not running: mumu_start")
        elif not entry["adb"]:
            hints.append(
                f"index {idx} has no adb: enable Settings > Others > ADB debug (local/remote), "
                "or use the manager-only tools (mumu_tap_sync, mumu_toolbar)"
            )
    return hints


@mcp.tool()
@_wrap
def mumu_list() -> dict:
    """List every instance with state, Android version, PID and adb port."""
    return {"instances": manager().instances()}


@mcp.tool()
@_wrap
def mumu_info(vmindex: int = 0) -> dict:
    """Detailed state of one instance (player_state, pid, vt, hyperv, windows)."""
    return {"instance": manager().instance(vmindex)}


@mcp.tool()
@_wrap
def mumu_create(count: int = 1, android_version: str = "", mini_disk: bool = False) -> dict:
    """Create new instance(s). android_version: '12', '15' or '' for auto."""
    args: list[Any] = ["create", "--number", max(1, int(count))]
    if mini_disk:
        args.append("--mini")
    if android_version:
        args += ["--version", android_version]
    return {"result": manager().run(*args, timeout=180).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_clone(vmindex: int = 0, count: int = 1) -> dict:
    """Clone an existing instance (fastest way to N parallel devices)."""
    return {"result": manager().run("clone", "--vmindex", vmindex, "--number", max(1, int(count)),
                                    timeout=300).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_delete(vmindex: int = 0) -> dict:
    """Delete an instance and its data disk. Irreversible."""
    return {"result": manager().run("delete", "--vmindex", vmindex, timeout=180).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_rename(vmindex: int = 0, name: str = "") -> dict:
    """Rename an instance."""
    if not name:
        raise BadArgument("name is required")
    return {"result": manager().run("rename", "--vmindex", vmindex, "--name", name, timeout=30).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_start(vmindex: int = 0) -> dict:
    """Boot an instance (returns as soon as the launch is accepted)."""
    return {"result": manager().run("control", "--vmindex", vmindex, "launch", timeout=60).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_stop(vmindex: int = 0) -> dict:
    """Shut an instance down."""
    return {"result": manager().run("control", "--vmindex", vmindex, "shutdown", timeout=90).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_reboot(vmindex: int = 0) -> dict:
    """Restart an instance."""
    return {"result": manager().run("control", "--vmindex", vmindex, "restart", timeout=90).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_boot_ready(vmindex: int = 0, timeout: int = 180, launch: bool = True) -> dict:
    """Start an instance if needed and block until Android finished booting.

    Polls `sys.boot_completed` through the manager shell channel, so it works
    before adb is up. Returns the elapsed time.
    """
    mgr = manager()
    if launch:
        mgr.run("control", "--vmindex", vmindex, "launch", timeout=60)
    started = time.monotonic()
    deadline = started + max(10, int(timeout))
    last = ""
    while time.monotonic() < deadline:
        try:
            last = mgr.sh(vmindex, f"getprop {C.BOOT_PROP}", timeout=10).strip()
        except MuMuError as exc:
            last = f"(shell: {exc.code})"
        if last == "1":
            return {"ready": True, "vmindex": vmindex,
                    "waited_s": round(time.monotonic() - started, 1)}
        time.sleep(2)
    raise Timeout(
        f"instance {vmindex} did not finish booting in {timeout}s",
        detail={"last_boot_completed": last},
    )


@mcp.tool()
@_wrap
def mumu_window(vmindex: int = 0, action: str = "show") -> dict:
    """Show, hide or lay out the instance window. action: show|hide."""
    if action not in {"show", "hide"}:
        raise BadArgument("action must be show or hide")
    sub = "show_window" if action == "show" else "hide_window"
    return {"result": manager().run("control", "--vmindex", vmindex, sub, timeout=30).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_sort() -> dict:
    """Auto-arrange all instance windows on screen."""
    return {"result": manager().run("sort", timeout=30).json_or_empty()}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_settings_get(vmindex: int | None = None, writable_only: bool = False) -> dict:
    """Dump instance settings (all, or only the writable keys)."""
    settings = manager().setting_keys(writable_only=writable_only, vmindex=vmindex)
    return {"vmindex": vmindex, "count": len(settings), "settings": settings}


@mcp.tool()
@_wrap
def mumu_setting_set(vmindex: int | None = None, key: str = "", value: str = "") -> dict:
    """Write one setting key. Use mumu_settings_get(writable_only=True) to find keys.

    Raises `key_not_writable` for build-managed keys such as `vm_cpu` on MuMu 6.x —
    use the semantic setters (mumu_set_cpu ...) which know the right key.
    """
    if not key:
        raise BadArgument("key is required")
    return {"result": manager().setting_set(vmindex, {key: value})}


@mcp.tool()
@_wrap
def mumu_set_resolution(vmindex: int = 0, width: int = 0, height: int = 0, dpi: int = 0,
                        preset: str = "") -> dict:
    """Set the screen. Either a preset name or explicit width/height/dpi. Needs a reboot to apply.

    Presets: phone, phone-hd, phone-hd+, tablet, tablet-hd, widescreen,
    widescreen-2k, widescreen-4k, square.
    """
    if preset:
        if preset not in C.RESOLUTION_PRESETS:
            raise BadArgument(f"unknown preset {preset!r}",
                              detail={"presets": sorted(C.RESOLUTION_PRESETS)})
        width, height, dpi = C.RESOLUTION_PRESETS[preset]
    if not (width and height):
        raise BadArgument("give width+height (+dpi) or a preset")
    dpi = dpi or C.RESOLUTION_PRESETS["phone"][2]
    for name, val in (("width", width), ("height", height), ("dpi", dpi)):
        lo, hi = C.RESOLUTION_LIMITS[name]
        if not lo <= val <= hi:
            raise BadArgument(f"{name} must be in [{lo}, {hi}], got {val}")
    return {
        "applied": {"width": width, "height": height, "dpi": dpi},
        "result": manager().setting_set(vmindex, {
            "resolution_width.custom": f"{float(width):.6f}",
            "resolution_height.custom": f"{float(height):.6f}",
            "resolution_dpi.custom": f"{float(dpi):.6f}",
            "resolution_mode": "custom",
        }),
        "note": "restart the instance for the new resolution to take effect",
    }


@mcp.tool()
@_wrap
def mumu_set_cpu(vmindex: int = 0, cores: int = 0) -> dict:
    """Set vCPU count. Writes `performance_cpu.custom` (works on 6.x, where `vm_cpu` is RO)."""
    lo, hi = C.CPU_RANGE
    if not lo <= cores <= hi:
        raise BadArgument(f"cores must be in [{lo}, {hi}], got {cores}")
    return {"result": manager().setting_set(vmindex, {"performance_cpu.custom": str(cores)})}


@mcp.tool()
@_wrap
def mumu_set_ram(vmindex: int = 0, gb: float = 0.0) -> dict:
    """Set guest RAM in GB. Writes `performance_mem.custom`."""
    lo, hi = C.RAM_RANGE
    if not lo <= gb <= hi:
        raise BadArgument(f"gb must be in [{lo}, {hi}], got {gb}")
    return {"result": manager().setting_set(vmindex, {"performance_mem.custom": f"{gb:.6f}"})}


@mcp.tool()
@_wrap
def mumu_set_fps(vmindex: int = 0, fps: int = 60) -> dict:
    """Set the frame cap (15-480)."""
    if not 15 <= fps <= 480:
        raise BadArgument("fps must be in [15, 480]")
    return {"result": manager().setting_set(vmindex, {"max_frame_rate": str(fps)})}


@mcp.tool()
@_wrap
def mumu_set_device(vmindex: int = 0, brand: str = "", model: str = "", miit: str = "") -> dict:
    """Change the phone identity reported to apps (brand / model / MIIT code)."""
    pairs = {}
    if brand:
        pairs["phone_brand"] = brand
    if model:
        pairs["phone_model"] = model
    if miit:
        pairs["phone_miit"] = miit
    if not pairs:
        raise BadArgument("give at least one of brand, model, miit")
    return {"applied": pairs, "result": manager().setting_set(vmindex, pairs)}


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_root_status(vmindex: int = 0) -> dict:
    """Report what root looks like on this instance, verified rather than assumed.

    Covers: whether adbd is uid 0, SELinux mode, whether `/system` is writable and
    through what mechanism, whether that mechanism survives a reboot, whether a
    `su` binary exists at all, and the two MuMu settings behind it.

    Read this before calling any other `mumu_root_*` tool — on MuMu the answers are
    not what a generic Android assumption predicts.
    """
    return {"root": root().state(vmindex).to_dict()}


@mcp.tool()
@_wrap
def mumu_root_adb(vmindex: int = 0, enabled: bool = True) -> dict:
    """`adb root` / `adb unroot`: run adbd as uid 0 (or drop it back to `shell`).

    Fast, and the instance keeps running — no reboot. This is the cheapest way to
    get a root shell, because there is no `su` binary on MuMu to invoke.
    """
    action = root()
    return action.enable_adb_root(vmindex) if enabled else action.disable_adb_root(vmindex)


@mcp.tool()
@_wrap
def mumu_root_permission(vmindex: int = 0, enabled: bool = True) -> dict:
    """Set MuMu's `root_permission` and restart, so adbd comes up as root at boot.

    Use this when you want root to *stay* across reboots; `mumu_root_adb` only
    affects the running instance.
    """
    return root().set_permission(vmindex, enabled)


@mcp.tool()
@_wrap
def mumu_root_system(vmindex: int = 0, writable: bool = True) -> dict:
    """Make `/system` writable (or read-only again) and restart.

    On MuMu the block device stays read-only; what this toggles is
    `system_disk_readonly`, which makes MuMu stack an overlayfs over `/system`
    backed by a host-side diff image. Writes therefore persist across reboots while
    the stock image stays untouched — `mumu_root_restore_system` is the clean undo.

    `writable=False` goes back to the stock read-only image.
    """
    return root().set_system_writable(vmindex, writable)


@mcp.tool()
@_wrap
def mumu_root_restore_system(vmindex: int = 0, confirm: bool = False) -> dict:
    """Reset `/system` to stock by deleting the overlay diff image.

    The stock system image was never modified, so removing `<vm>/system-diff.vdi`
    on the host is a complete restore and reclaims the space the overlay used.

    Stop the instance first — the image is mapped while it runs. Requires
    `confirm=True`; the file is deleted, not archived.
    """
    return root().restore_system_diff(vmindex, confirm=confirm)


@mcp.tool()
@_wrap
def mumu_root_remount(vmindex: int = 0, target: str = "/system") -> dict:
    """Try `mount -o rw,remount <target>` and explain the result.

    On stock MuMu 6.x this cannot succeed — the block device itself is read-only —
    so the payload says to use `mumu_root_system(True)` instead of just reporting a
    mount failure.
    """
    return root().remount(vmindex, target)


@mcp.tool()
@_wrap
def mumu_root_exec(vmindex: int = 0, command: str = "", timeout_s: int = 60) -> dict:
    """Run a shell command with an explicit root check, refusing silently-broken runs.

    `mumu_shell` already runs as root when adbd is root, but it will happily run as
    `shell` and let a command fail halfway. This asserts the uid first and tells you
    exactly what to fix if it is not root.
    """
    if not command:
        raise BadArgument("command is required")
    state = root().state(vmindex)
    if not state.is_root:
        raise BadArgument(
            "adbd is not root, so this command would run as `shell`",
            detail={
                "uid": state.uid,
                "fix": "call mumu_root_adb(enabled=True), or mumu_root_permission(True)",
            },
        )
    return {
        "uid": state.uid,
        "context": state.context,
        "command": command,
        "output": adb().shell(vmindex, command, timeout=max(5, timeout_s)),
    }


@mcp.tool()
@_wrap
def mumu_root_push_system(vmindex: int = 0, local_path: str = "",
                          remote_path: str = "/system/etc/") -> dict:
    """Copy a host file into `/system`, creating the overlay first if needed.

    Verifies the destination is actually writable before copying, so a failure is a
    clear "system is read-only, call mumu_root_system(True)" rather than a partial
    push followed by a confusing `adb` error.
    """
    if not local_path:
        raise BadArgument("local_path is required")
    if not Path(local_path).is_file():
        raise BadArgument(f"file not found: {local_path}")
    if not remote_path.startswith("/system/"):
        raise BadArgument("remote_path must live under /system")
    state = root().state(vmindex)
    if not state.is_root:
        raise BadArgument("adbd is not root; call mumu_root_adb(True) first",
                          detail={"uid": state.uid})
    if not state.system_writable:
        raise BadArgument(
            "/system is read-only; call mumu_root_system(True) to enable the overlay first",
            detail={"system_overlay": state.overlay},
        )
    target = remote_path.rstrip("/") + "/" + Path(local_path).name
    adb().push(vmindex, local_path, target)
    out = adb().shell(vmindex, f"ls -l {target}; chmod 644 {target} 2>&1")
    return {
        "pushed": target,
        "listed": out.strip(),
        "persists_reboot": True,
        "note": "written through the overlay; mumu_root_restore_system undoes it",
    }


@mcp.tool()
@_wrap
def mumu_root_pull_system(vmindex: int = 0, remote_path: str = "", local_path: str = "") -> dict:
    """Copy any guest file out to the host — including `/data/data` of other apps.

    The plain `mumu_pull` refuses paths the `shell` user cannot read; with adbd as
    root there is no such restriction, which is what makes this its own tool.

    `adb pull` on a directory succeeds and reports 0 bytes unless `-a` is used, so
    directories are detected up front and copied with `-a` instead of returning a
    silently empty file.
    """
    if not (remote_path and local_path):
        raise BadArgument("remote_path and local_path are required")
    state = root().state(vmindex)
    if not state.is_root:
        raise BadArgument("adbd is not root, so protected paths are unreadable",
                          detail={"uid": state.uid, "fix": "mumu_root_adb(True)"})

    exists = adb().shell(vmindex, f"[ -e {remote_path} ] && echo yes || echo no").strip()
    if exists != "yes":
        raise BadArgument(
            f"no such path in the guest: {remote_path}",
            detail={"hint": "adb pull reports success for missing paths, so this is checked first"},
        )

    kind = adb().shell(vmindex, f"[ -d {remote_path} ] && echo dir || echo file").strip()
    if kind == "dir":
        adb().run(vmindex, "pull", "-a", remote_path, local_path, timeout=300)
        dest = Path(local_path)
        if not dest.exists():
            raise AdbUnavailable("pull reported success but nothing landed on disk",
                                 detail={"remote": remote_path, "local": local_path})
        files = [p for p in dest.rglob("*") if p.is_file()]
        return {
            "pulled": str(dest),
            "kind": "directory",
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
        }

    adb().pull(vmindex, remote_path, local_path, timeout=300)
    dest = Path(local_path)
    if not dest.is_file():
        raise AdbUnavailable("pull reported success but no file was written",
                             detail={"remote": remote_path, "local": local_path})
    size = dest.stat().st_size
    if size == 0:
        # A real empty file is possible, but far more often this is a path the
        # guest could not produce — say so instead of handing back a 0-byte file.
        check = adb().shell(vmindex, f"ls -l {remote_path} 2>&1").strip()
        return {"pulled": str(dest), "bytes": 0, "kind": "file",
                "warning": "empty file copied; verify the source",
                "source_listing": check}
    return {"pulled": str(dest), "bytes": size, "kind": "file"}


@mcp.tool()
@_wrap
def mumu_spoof_identity(vmindex: int = 0, imei: str = "", mac: str = "",
                        android_id: str = "") -> dict:
    """Overwrite IMEI / MAC / Android ID. Pass `random` to generate a fresh one."""
    keys = {"imei": imei, "mac_address": mac, "android_id": android_id}
    applied: dict[str, Any] = {}
    for name, want in keys.items():
        if not want:
            continue
        value = _random_for(name) if want.lower() in {"random", "rand", "new"} else want
        applied[name] = value
        manager().run("simulation", "--vmindex", vmindex,
                      "--simu_key", name, "--simu_value", value, timeout=30)
    if not applied:
        raise BadArgument("give at least one of imei, mac, android_id (or 'random')")
    return {"applied": applied, "note": "restart the instance so apps re-read the identity"}


def _random_for(kind: str) -> str:
    import random
    import string

    if kind == "mac_address":
        tail = ":".join(f"{random.randrange(256):02x}" for _ in range(5))
        return f"02:{tail}"
    if kind == "imei":
        # Luhn-valid 15-digit IMEI, because apps check the check digit.
        body = [random.randrange(10) for _ in range(14)]
        total = 0
        for i, d in enumerate(body):
            if i % 2:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return "".join(map(str, body)) + str((10 - total % 10) % 10)
    return "".join(random.choice(string.hexdigits.lower()) for _ in range(16))


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_app_list(vmindex: int = 0) -> dict:
    """List installed packages with app name, version and version code."""
    payload = manager().call("control", "--vmindex", vmindex, "app", "info", "--installed", timeout=60)
    return {"apps": payload if isinstance(payload, dict) else {}, "vmindex": vmindex}


@mcp.tool()
@_wrap
def mumu_app_info(vmindex: int = 0, package: str = "") -> dict:
    """Info for one package (version, version code, install path)."""
    if not package:
        raise BadArgument("package is required")
    payload = manager().call("control", "--vmindex", vmindex, "app", "info",
                             "--package", package, timeout=30)
    return {"package": package, "info": payload}


@mcp.tool()
@_wrap
def mumu_app_install(vmindex: int = 0, apk_path: str = "") -> dict:
    """Install an .apk / .apks / .xapk from the host into the instance."""
    if not apk_path:
        raise BadArgument("apk_path is required")
    p = Path(apk_path)
    if not p.is_file():
        raise BadArgument(f"file not found: {apk_path}")
    return {"installed": str(p),
            "result": manager().run("control", "--vmindex", vmindex, "app", "install",
                                    "--apk", str(p), timeout=600).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_app_batch_install(vmindex: int = 0, apk_paths: Any = None) -> dict:
    """Install several packages in order; reports per-file outcome."""
    paths = as_list(apk_paths)
    if not paths:
        raise BadArgument("apk_paths is required")
    results = []
    for path in paths:
        try:
            results.append({"apk": path, "ok": True,
                            **mumu_app_install.__wrapped__(vmindex, path)})
        except MuMuError as exc:
            results.append({"apk": path, **exc.to_dict()})
    return {"results": results}


@mcp.tool()
@_wrap
def mumu_app_uninstall(vmindex: int = 0, package: str = "") -> dict:
    """Uninstall a package."""
    if not package:
        raise BadArgument("package is required")
    return {"result": manager().run("control", "--vmindex", vmindex, "app", "uninstall",
                                    "--package", package, timeout=120).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_app_launch(vmindex: int = 0, package: str = "") -> dict:
    """Launch a package by name."""
    if not package:
        raise BadArgument("package is required")
    return {"result": manager().run("control", "--vmindex", vmindex, "app", "launch",
                                    "--package", package, timeout=60).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_app_close(vmindex: int = 0, package: str = "") -> dict:
    """Force stop a package."""
    if not package:
        raise BadArgument("package is required")
    return {"result": manager().run("control", "--vmindex", vmindex, "app", "close",
                                    "--package", package, timeout=60).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_app_clear_data(vmindex: int = 0, package: str = "") -> dict:
    """Wipe a package's data (pm clear). Returns the app to a fresh-install state."""
    if not package:
        raise BadArgument("package is required")
    return {"package": package, "output": adb().shell(vmindex, f"pm clear {package}", timeout=60)}


@mcp.tool()
@_wrap
def mumu_app_fresh_start(vmindex: int = 0, package: str = "") -> dict:
    """Force stop, wipe data, then launch — the reproducible "first run" state."""
    if not package:
        raise BadArgument("package is required")
    steps: dict[str, Any] = {}
    for name, call in (
        ("close", lambda: mumu_app_close.__wrapped__(vmindex, package)),
        ("clear", lambda: mumu_app_clear_data.__wrapped__(vmindex, package)),
        ("launch", lambda: mumu_app_launch.__wrapped__(vmindex, package)),
    ):
        try:
            steps[name] = call()
        except MuMuError as exc:
            steps[name] = exc.to_dict()
    return {"package": package, "steps": steps}


@mcp.tool()
@_wrap
def mumu_app_monkey(vmindex: int = 0, package: str = "", events: int = 200,
                    throttle_ms: int = 100, seed: int = 0) -> dict:
    """Run a monkey stress pass on one package and report the crash summary."""
    if not package:
        raise BadArgument("package is required")
    cmd = f"monkey -p {package} --throttle {throttle_ms} -v {events}"
    if seed:
        cmd += f" -s {seed}"
    out = adb().shell(vmindex, cmd, timeout=max(60.0, events * throttle_ms / 1000 + 60))
    crashes = [ln for ln in out.splitlines()
               if "CRASH" in ln or "ANR" in ln or "monkey aborted" in ln.lower()]
    return {"package": package, "events": events, "crashes": crashes,
            "clean": not crashes, "tail": out[-1200:]}


# ---------------------------------------------------------------------------
# input — manager channel (no adb required)
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_tap_sync(vmindex: int = 0, x: int = 0, y: int = 0) -> dict:
    """Tap through MuMu's own toolbar channel. Works when adb is unavailable."""
    return {"result": manager().run("control", "--vmindex", vmindex, "tool", "cmd",
                                    "--cmd", f"input tap {int(x)} {int(y)}", timeout=30).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_swipe_sync(vmindex: int = 0, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                    duration_ms: int = 300) -> dict:
    """Swipe through MuMu's toolbar channel (no adb)."""
    cmd = f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}"
    return {"result": manager().run("control", "--vmindex", vmindex, "tool", "cmd",
                                    "--cmd", cmd, timeout=30).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_text_sync(vmindex: int = 0, text: str = "") -> dict:
    """Type text through MuMu's toolbar channel (handles CJK; no adb)."""
    if not text:
        raise BadArgument("text is required")
    return {"result": manager().run("control", "--vmindex", vmindex, "tool", "cmd",
                                    "--text", text, "--cmd", "input_text",
                                    timeout=30).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_toolbar(vmindex: int = 0, name: str = "") -> dict:
    """Trigger a MuMu toolbar button: rotate, go_home, go_back, top_most, fullscreen,
    shake, screenshot, volume_up, volume_down, volume_mute. No adb required."""
    if name not in C.TOOLBAR_FUNCS:
        raise BadArgument(f"unknown toolbar function {name!r}",
                          detail={"allowed": list(C.TOOLBAR_FUNCS)})
    return {"result": manager().run("control", "--vmindex", vmindex, "tool", "func",
                                    "--name", name, timeout=30).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_gps_set(vmindex: int = 0, latitude: float = 0.0, longitude: float = 0.0) -> dict:
    """Move the emulated GPS position. Native, no mock-location app needed."""
    if not -90 <= latitude <= 90:
        raise BadArgument("latitude must be in [-90, 90]")
    if not -180 <= longitude <= 180:
        raise BadArgument("longitude must be in [-180, 180]")
    return {"result": manager().run("control", "--vmindex", vmindex, "tool", "location",
                                    "--latitude", latitude, "--longitude", longitude,
                                    timeout=30).json_or_empty(),
            "location": {"lat": latitude, "lon": longitude}}


# ---------------------------------------------------------------------------
# input — adb channel (finer control)
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_tap(vmindex: int = 0, x: int = 0, y: int = 0) -> dict:
    """Tap at guest pixel coordinates via adb."""
    return {"output": adb().shell(vmindex, f"input tap {int(x)} {int(y)}")}


@mcp.tool()
@_wrap
def mumu_swipe(vmindex: int = 0, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
               duration_ms: int = 300) -> dict:
    """Swipe/drag via adb."""
    return {"output": adb().shell(
        vmindex, f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")}


@mcp.tool()
@_wrap
def mumu_long_press(vmindex: int = 0, x: int = 0, y: int = 0, duration_ms: int = 800) -> dict:
    """Press and hold a point."""
    return {"output": adb().shell(
        vmindex, f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}")}


@mcp.tool()
@_wrap
def mumu_text(vmindex: int = 0, text: str = "") -> dict:
    """Type ASCII into the focused field via adb (spaces are escaped correctly)."""
    if not text:
        raise BadArgument("text is required")
    return {"typed": text, "output": adb().input_text(vmindex, text)}


@mcp.tool()
@_wrap
def mumu_key(vmindex: int = 0, key: str = "") -> dict:
    """Send a key by name (home, back, enter, volume_up, power, ...) or numeric keycode."""
    if key.isdigit():
        code = int(key)
    else:
        code = C.KEYCODES.get(key.lower(), -1)
        if code < 0:
            raise BadArgument(f"unknown key {key!r}", detail={"known": sorted(C.KEYCODES)})
    return {"key": key, "keycode": code,
            "output": adb().shell(vmindex, f"input keyevent {code}")}


@mcp.tool()
@_wrap
def mumu_press_home(vmindex: int = 0) -> dict:
    """Go to the launcher."""
    return {"output": adb().shell(vmindex, "input keyevent 3")}


@mcp.tool()
@_wrap
def mumu_press_back(vmindex: int = 0) -> dict:
    """Send BACK."""
    return {"output": adb().shell(vmindex, "input keyevent 4")}


@mcp.tool()
@_wrap
def mumu_press_recent(vmindex: int = 0) -> dict:
    """Open the recents / overview switcher."""
    return {"output": adb().shell(vmindex, "input keyevent 187")}


@mcp.tool()
@_wrap
def mumu_scroll(vmindex: int = 0, direction: str = "down", amount: int = 1) -> dict:
    """Scroll the screen. direction: up|down|left|right."""
    if direction not in {"up", "down", "left", "right"}:
        raise BadArgument("direction must be up, down, left or right")
    width, height = _screen_size(vmindex)
    cx, cy = width // 2, height // 2
    span = int(height * 0.25) if direction in {"up", "down"} else int(width * 0.25)
    outputs = []
    for _ in range(max(1, int(amount))):
        if direction == "down":
            cmd = f"input swipe {cx} {cy + span} {cx} {cy - span} 250"
        elif direction == "up":
            cmd = f"input swipe {cx} {cy - span} {cx} {cy + span} 250"
        elif direction == "right":
            cmd = f"input swipe {cx - span} {cy} {cx + span} {cy} 250"
        else:
            cmd = f"input swipe {cx + span} {cy} {cx - span} {cy} 250"
        outputs.append(adb().shell(vmindex, cmd))
    return {"direction": direction, "amount": amount, "size": [width, height], "outputs": outputs}


@mcp.tool()
@_wrap
def mumu_rotate(vmindex: int = 0, orientation: str = "landscape") -> dict:
    """Force a screen orientation: portrait, landscape, reverse_portrait,
    reverse_landscape, or auto."""
    if orientation == "auto":
        return {"output": adb().shell(vmindex, "settings put system accelerometer_rotation 1")}
    mapping = {"portrait": 0, "landscape": 1, "reverse_portrait": 2, "reverse_landscape": 3}
    if orientation not in mapping:
        raise BadArgument(f"invalid orientation {orientation!r}", detail={"allowed": list(mapping) + ["auto"]})
    adb().shell(vmindex, "settings put system accelerometer_rotation 0")
    return {"orientation": orientation,
            "output": adb().shell(vmindex, f"settings put system user_rotation {mapping[orientation]}")}


@mcp.tool()
@_wrap
def mumu_volume(vmindex: int = 0, action: str = "up") -> dict:
    """Volume control: up | down | mute."""
    codes = {"up": 24, "down": 25, "mute": 164}
    if action not in codes:
        raise BadArgument("action must be up, down or mute")
    return {"output": adb().shell(vmindex, f"input keyevent {codes[action]}")}


@mcp.tool()
@_wrap
def mumu_shake(vmindex: int = 0) -> dict:
    """Real accelerometer shake (the manager toolbar function, not an airplane-mode broadcast)."""
    return {"result": manager().run("control", "--vmindex", vmindex, "tool", "func",
                                    "--name", "shake", timeout=30).json_or_empty()}


# ---------------------------------------------------------------------------
# screen reading
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_ui_dump(vmindex: int = 0, simplify: bool = True) -> dict:
    """Dump the current view hierarchy with clickable bounds.

    This is what makes blind tapping unnecessary: each node carries a
    `center` you can feed straight into mumu_tap. `simplify=True` keeps only
    nodes that have text, a content description, or are clickable.
    """
    remote = "/sdcard/.mumu_mcp_ui.xml"
    adb().shell(vmindex, f"uiautomator dump --compressed {remote}", timeout=60)
    local = str(Path(os.environ.get("TEMP", ".")) / f".mumu_mcp_ui_{vmindex}.xml")
    adb().pull(vmindex, remote, local, timeout=60)
    adb().shell(vmindex, f"rm -f {remote}", timeout=15)
    raw = Path(local).read_text(encoding="utf-8", errors="replace")
    nodes = _parse_ui(raw)
    if simplify:
        nodes = [n for n in nodes if n["text"] or n["desc"] or n["clickable"]]
    return {"count": len(nodes), "nodes": nodes[:400], "truncated": len(nodes) > 400}


def _parse_ui(xml: str) -> list[dict]:
    import re as _re

    bounds_re = _re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
    attr_re = _re.compile(r'([\w:-]+)="([^"]*)"')
    out: list[dict] = []
    for tag in _re.findall(r"<node [^>]*>", xml):
        attrs = dict(attr_re.findall(tag))
        node: dict[str, Any] = {
            "text": attrs.get("text", ""),
            "desc": attrs.get("content-desc", ""),
            "id": attrs.get("resource-id", "").split("/")[-1],
            "class": attrs.get("class", "").split(".")[-1],
            "clickable": attrs.get("clickable") == "true",
            "enabled": attrs.get("enabled") == "true",
            "bounds": attrs.get("bounds", ""),
        }
        m = bounds_re.search(node["bounds"])
        if m:
            x1, y1, x2, y2 = (int(v) for v in m.groups())
            node["center"] = [(x1 + x2) // 2, (y1 + y2) // 2]
        out.append(node)
    return out


@mcp.tool()
@_wrap
def mumu_wait_for(vmindex: int = 0, text: str = "", timeout_s: int = 30,
                  poll_s: float = 1.0) -> dict:
    """Block until `text` appears in the view hierarchy (or a content description).

    Returns the matching node so the caller can tap it directly. Cheaper and far
    more reliable than sleep-then-screenshot loops.
    """
    if not text:
        raise BadArgument("text is required")
    started = time.monotonic()
    deadline = started + max(1, int(timeout_s))
    target = text.lower()
    while time.monotonic() < deadline:
        try:
            dump = mumu_ui_dump.__wrapped__(vmindex, simplify=True)
            for node in dump["nodes"]:
                if target in (node["text"] or "").lower() or target in (node["desc"] or "").lower():
                    return {"found": True, "node": node,
                            "waited_s": round(time.monotonic() - started, 1)}
        except MuMuError:
            pass
        time.sleep(poll_s)
    raise Timeout(f"text {text!r} did not appear in {timeout_s}s",
                  detail={"vmindex": vmindex})


@mcp.tool()
@_wrap
def mumu_screenshot(vmindex: int = 0, path: str = "") -> dict:
    """Capture the guest screen to a PNG on the host and return its path.

    Prefer this over the base64 variant: an agent can read the file directly and
    the payload does not blow up the context.
    """
    dest = Path(path) if path else Path(os.environ.get("TEMP", ".")) / f"mumu_{vmindex}_{int(time.time())}.png"
    adb().screencap(vmindex, str(dest))
    return {"path": str(dest), "bytes": dest.stat().st_size,
            "size": list(_screen_size(vmindex))}


@mcp.tool()
@_wrap
def mumu_screenshot_base64(vmindex: int = 0) -> dict:
    """Capture the screen and inline it as base64 (large: ~2.6 MB PNG). Prefer mumu_screenshot."""
    dest = Path(os.environ.get("TEMP", ".")) / f"mumu_{vmindex}_{int(time.time())}.png"
    adb().screencap(vmindex, str(dest))
    data = base64.b64encode(dest.read_bytes()).decode("ascii")
    dest.unlink(missing_ok=True)
    return {"mime": "image/png", "bytes": len(data), "base64": data}


@mcp.tool()
@_wrap
def mumu_screen_size(vmindex: int = 0) -> dict:
    """Guest resolution and density (from `wm size` / `wm density`)."""
    width, height = _screen_size(vmindex)
    density = adb().shell(vmindex, "wm density").split(":")[-1].strip()
    return {"width": width, "height": height, "density": density}


def _screen_size(vmindex: int) -> tuple[int, int]:
    out = adb().shell(vmindex, "wm size")
    line = out.strip().splitlines()[-1] if out.strip() else ""
    raw = line.split(":")[-1].strip()
    if "x" not in raw:
        raise AdbUnavailable("could not read screen size", detail={"output": out})
    w, h = raw.split("x")
    return int(w), int(h)


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_network_info(vmindex: int = 0) -> dict:
    """Guest interfaces, addresses and routing table."""
    return {"vmindex": vmindex,
            "addresses": adb().shell(vmindex, "ip addr show"),
            "routes": adb().shell(vmindex, "ip route"),
            "endpoint": adb().endpoint(vmindex)}


@mcp.tool()
@_wrap
def mumu_public_ip(vmindex: int = 0) -> dict:
    """Public egress IP as seen from inside the guest."""
    for probe in ("curl -s --max-time 6 ifconfig.me",
                  "curl -s --max-time 6 https://api.ipify.org",
                  "ping -c 1 -W 2 8.8.8.8 > /dev/null && echo ping-ok || echo ping-fail"):
        out = adb().shell(vmindex, probe, timeout=20).strip()
        if out and "fail" not in out:
            return {"public_ip": out, "probe": probe.split()[0]}
    raise AdbUnavailable("guest could not reach the internet", detail={"vmindex": vmindex})


@mcp.tool()
@_wrap
def mumu_airplane_mode(vmindex: int = 0, enabled: bool = True) -> dict:
    """Toggle airplane mode."""
    flag = "1" if enabled else "0"
    adb().shell(vmindex, f"settings put global airplane_mode_on {flag}")
    return {"enabled": enabled,
            "output": adb().shell(
                vmindex,
                "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state "
                + ("true" if enabled else "false"))}


@mcp.tool()
@_wrap
def mumu_wifi(vmindex: int = 0, enabled: bool = True) -> dict:
    """Toggle Wi-Fi."""
    return {"enabled": enabled,
            "output": adb().shell(vmindex, f"svc wifi {'enable' if enabled else 'disable'}")}


# ---------------------------------------------------------------------------
# shell / files / props
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_shell(vmindex: int = 0, command: str = "", timeout_s: int = 60) -> dict:
    """Run a shell command inside the guest via adb."""
    if not command:
        raise BadArgument("command is required")
    return {"command": command, "output": adb().shell(vmindex, command, timeout=max(5, timeout_s))}


@mcp.tool()
@_wrap
def mumu_push(vmindex: int = 0, local_path: str = "", remote_path: str = "") -> dict:
    """Copy a host file into the guest."""
    if not (local_path and remote_path):
        raise BadArgument("local_path and remote_path are required")
    return {"output": adb().push(vmindex, local_path, remote_path)}


@mcp.tool()
@_wrap
def mumu_pull(vmindex: int = 0, remote_path: str = "", local_path: str = "") -> dict:
    """Copy a guest file out to the host."""
    if not (remote_path and local_path):
        raise BadArgument("remote_path and local_path are required")
    adb().pull(vmindex, remote_path, local_path)
    return {"saved": local_path, "bytes": Path(local_path).stat().st_size}


@mcp.tool()
@_wrap
def mumu_logcat(vmindex: int = 0, lines: int = 200, package: str = "", level: str = "") -> dict:
    """Read the last N logcat lines, optionally filtered by package or level."""
    cmd = f"logcat -d -t {max(1, int(lines))}"
    if package:
        cmd += f" --pid=$(pidof {package})"
    if level:
        cmd += f" *:{level.upper()}"
    return {"lines": lines, "output": adb().shell(vmindex, cmd, timeout=60)}


@mcp.tool()
@_wrap
def mumu_prop_get(vmindex: int = 0, key: str = "") -> dict:
    """Read one Android system property."""
    if not key:
        raise BadArgument("key is required")
    return {"key": key, "value": adb().shell(vmindex, f"getprop {key}").strip()}


@mcp.tool()
@_wrap
def mumu_prop_set(vmindex: int = 0, key: str = "", value: str = "") -> dict:
    """Write one Android system property (needs root for most keys)."""
    if not key:
        raise BadArgument("key is required")
    return {"key": key, "value": value,
            "output": adb().shell(vmindex, f"setprop {key} {value}")}


# ---------------------------------------------------------------------------
# images / transfer
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_export(vmindex: int = 0, directory: str = "", name: str = "", zip_it: bool = True) -> dict:
    """Export an instance to a .mumudata file."""
    if not directory:
        raise BadArgument("directory is required")
    args: list[Any] = ["export", "--vmindex", vmindex, "--dir", directory]
    if name:
        args += ["--name", name]
    if zip_it:
        args.append("--zip")
    return {"result": manager().run(*args, timeout=900).json_or_empty()}


@mcp.tool()
@_wrap
def mumu_import(path: str = "", count: int = 1) -> dict:
    """Import .mumudata file(s) as new instance(s)."""
    if not path:
        raise BadArgument("path is required")
    return {"result": manager().run("import", "--path", path, "--number", max(1, int(count)),
                                    timeout=900).json_or_empty()}


# ---------------------------------------------------------------------------
# device state and app launching
# ---------------------------------------------------------------------------
def device() -> Device:
    inst = _state.get("device")
    if inst is None:
        inst = Device(adb=adb(), manager=manager())
        _state["device"] = inst
    return inst


def flow() -> Flow:
    inst = _state.get("flow")
    if inst is None:
        inst = Flow(
            adb=adb(),
            device=device(),
            ui_dump=lambda idx, simplify=True: mumu_ui_dump.__wrapped__(idx, simplify),
            screenshot=lambda idx, path="": mumu_screenshot.__wrapped__(idx, path)["path"],
        )
        _state["flow"] = inst
    return inst


@mcp.tool()
@_wrap
def mumu_foreground(vmindex: int = 0) -> dict:
    """Which app/activity is in front right now.

    Reads `dumpsys activity activities` and falls back to the focused window;
    the two disagree during transitions, so both are consulted.
    """
    return {"foreground": device().foreground(vmindex).to_dict()}


@mcp.tool()
@_wrap
def mumu_wait_activity(vmindex: int = 0, target: str = "", timeout_s: int = 30) -> dict:
    """Block until the foreground package (or component, or regex) matches `target`.

    This is the reliable "did my tap actually navigate" check — far better than
    sleeping and hoping, because it keys on what Android itself reports.
    """
    fg = device().wait_for_activity(vmindex, target, timeout=max(1, timeout_s))
    return {"foreground": fg.to_dict(), "matched": target}


@mcp.tool()
@_wrap
def mumu_launch(vmindex: int = 0, package: str = "", activity: str = "",
                component: str = "", action: str = "", data: str = "",
                extras: Any = None, wait: bool = True, force_stop: bool = False) -> dict:
    """Start an activity through `am start`, and report how long it took.

    Give `component` (`pkg/act`), or `package` (+ `activity`), or an `action`.
    `wait=True` adds `-W` so the result carries `total_ms` and whether the app was
    already running — the difference between a cold and a warm launch.
    """
    parsed = extras if isinstance(extras, dict) else None
    if isinstance(extras, str) and extras.strip():
        try:
            parsed = json.loads(extras)
        except json.JSONDecodeError as exc:
            raise BadArgument(f"extras is not valid JSON: {exc}") from exc
    return device().launch(vmindex, component=component, package=package, activity=activity,
                           action=action, data=data, extras=parsed,
                           wait=wait, force_stop=force_stop)


@mcp.tool()
@_wrap
def mumu_battery(vmindex: int = 0) -> dict:
    """Current battery state (level, status, health, temperature, voltage)."""
    return {"battery": device().battery(vmindex)}


@mcp.tool()
@_wrap
def mumu_set_battery(vmindex: int = 0, level: int | None = None, status: int | None = None,
                     temperature: int | None = None, reset: bool = False) -> dict:
    """Override the reported battery, or `reset=True` to hand control back to the guest.

    Useful for exercising low-battery paths. `status`: 2 charging, 3 discharging,
    4 not charging, 5 full.
    """
    return device().set_battery(vmindex, level=level, status=status,
                                temperature=temperature, reset=reset)


@mcp.tool()
@_wrap
def mumu_proxy(vmindex: int = 0) -> dict:
    """Read the global HTTP proxy apps see (`settings global http_proxy`)."""
    return device().proxy(vmindex)


@mcp.tool()
@_wrap
def mumu_set_proxy(vmindex: int = 0, host: str = "", port: int = 0) -> dict:
    """Point every app at an HTTP proxy — the usual way to watch an app's traffic.

    `host=""` clears it. This writes the global setting, so it takes effect for
    apps immediately and survives until cleared or the data is wiped.
    """
    return device().set_proxy(vmindex, host=host, port=port)


@mcp.tool()
@_wrap
def mumu_hosts(vmindex: int = 0) -> dict:
    """Current `/system/etc/hosts` mappings, parsed."""
    return device().hosts(vmindex)


@mcp.tool()
@_wrap
def mumu_add_hosts(vmindex: int = 0, entries: Any = None, replace: bool = False) -> dict:
    """Pin DNS: add `{"ip": "hostname"}` mappings to `/system/etc/hosts`.

    Needs the root overlay (`mumu_root_system(True)`) because the file is on the
    read-only system partition. `replace=True` rewrites the file instead of
    appending. Mappings persist across reboots via the overlay diff image.
    """
    parsed = entries
    if isinstance(entries, str):
        try:
            parsed = json.loads(entries)
        except json.JSONDecodeError as exc:
            raise BadArgument(f"entries is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BadArgument("entries must be an object of {ip: hostname}")
    return device().add_hosts(vmindex, parsed, replace=replace)


@mcp.tool()
@_wrap
def mumu_storage(vmindex: int = 0) -> dict:
    """Free and used space on the guest filesystems."""
    return device().storage(vmindex)


@mcp.tool()
@_wrap
def mumu_processes(vmindex: int = 0, limit: int = 15) -> dict:
    """Top processes by CPU, plus load average and core count."""
    return device().processes(vmindex, limit=max(1, limit))


@mcp.tool()
@_wrap
def mumu_snapshot(vmindex: int = 0) -> dict:
    """Everything you need before acting, in one call: foreground app, screen,
    battery, adbd uid, proxy, storage.

    Cheaper than five tools and internally consistent — the foreground app and the
    screen size come from the same moment. A failed probe lands in `errors`
    instead of sinking the whole call.
    """
    return device().summary(vmindex)


# ---------------------------------------------------------------------------
# scripted flows
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def mumu_flow(vmindex: int = 0, steps: Any = None, stop_on_error: bool = True) -> dict:
    """Run a list of UI steps in one call: the tool that replaces a dozen round trips.

    Each step is `{"action": ...}` with these actions:

    | action | fields |
    |---|---|
    | `tap` | `x`/`y`, or a locator (`text`/`desc`/`id` + optional `index`, `exact`) |
    | `swipe` | `x1 y1 x2 y2`, optional `duration_ms` |
    | `long_press` | locator or `x`/`y`, optional `duration_ms` |
    | `text` | `text` |
    | `key` | `key` (name or numeric keycode) |
    | `wait_for` | locator, optional `timeout_s` |
    | `wait_gone` | locator, optional `timeout_s` |
    | `wait_activity` | `target` (package or regex), optional `timeout_s` |
    | `assert_text` | locator — fails the flow if absent |
    | `launch` | `package` (+ `activity`), or `component`, optional `force_stop` |
    | `sleep` | `ms` (capped at 30 s) |
    | `shell` | `command` |
    | `screenshot` | optional `path` |

    A locator polls the parsed view hierarchy, so it is immune to animation and
    OCR noise. Stops at the first failure and reports `failed_step` plus what it
    saw, which is what makes a broken flow diagnosable without re-running it.
    """
    return flow().run(vmindex, parse_steps(steps), stop_on_error=stop_on_error)


# ---------------------------------------------------------------------------
# gestures — the kernel input channel
# ---------------------------------------------------------------------------
def gestures() -> Gestures:
    inst = _state.get("gestures")
    if inst is None:
        inst = Gestures(adb=adb())
        _state["gestures"] = inst
    return inst


@mcp.tool()
@_wrap
def mumu_touch_device(vmindex: int = 0, refresh: bool = False) -> dict:
    """Report the multitouch input node, found by capability rather than by index.

    Device numbering differs between images and boots, so this looks for
    `ABS_MT_POSITION_X` and reports the node name, axis range and slot count. The
    gesture tools use the same discovery, which is why they do not need a device
    path.
    """
    dev = gestures().touch_device(vmindex, refresh=refresh)
    return {"device": dev.path, "name": dev.name,
            "max_x": dev.max_x, "max_y": dev.max_y, "slots": dev.slots}


@mcp.tool()
@_wrap
def mumu_pinch(vmindex: int = 0, cx: int = 0, cy: int = 0, start: int = 120,
               end: int = 360, steps: int = 8, duration_ms: int = 300,
               axis: str = "x") -> dict:
    """Two-finger pinch or zoom around (cx, cy), written as MT-protocol-B events.

    Spans `start` to `end` half-distance: `end > start` zooms in, `end < start`
    zooms out. `axis` is `x` or `y`.

    **Platform limit, measured:** on MuMu 6.8.x the host input bridge forwards
    single-pointer events and drops multi-slot ones, so this sequence reaches the
    kernel but not the app. The helper emits a correct gesture and will work
    unchanged on a build whose bridge forwards multi-touch; on this build, zoom or
    pan through the UI's own controls instead. Verify with `mumu_touch_device`
    and a before/after `mumu_ui_dump` before relying on it.
    """
    if axis not in {"x", "y"}:
        raise BadArgument("axis must be x or y")
    return gestures().pinch(vmindex, cx, cy, start=start, end=end,
                            steps=steps, duration_ms=duration_ms, axis=axis)


@mcp.tool()
@_wrap
def mumu_two_finger_drag(vmindex: int = 0, x1: int = 0, y1: int = 0, x2: int = 0,
                         y2: int = 0, gap: int = 120, steps: int = 8,
                         duration_ms: int = 300) -> dict:
    """Drag with two fingers held `gap` apart — the "move the map" gesture.

    Subject to the same platform limit as `mumu_pinch`: single-finger `mumu_swipe`
    scrolls and works; this multi-slot gesture does not reach the app on this
    MuMu build.
    """
    return gestures().two_finger_drag(vmindex, x1, y1, x2, y2, gap=gap,
                                      steps=steps, duration_ms=duration_ms)


@mcp.tool()
@_wrap
def mumu_two_finger_hold(vmindex: int = 0, x: int = 0, y: int = 0, gap: int = 120,
                         hold_ms: int = 900) -> dict:
    """Press and hold with two fingers. Same platform limit as `mumu_pinch`."""
    return gestures().long_press_two_finger(vmindex, x, y, gap=gap, hold_ms=hold_ms)


@mcp.tool()
@_wrap
def mumu_raw_event(vmindex: int = 0, event_type: int = 0, code: int = 0, value: int = 0,
                   device: str = "") -> dict:
    """Emit one raw input event (`type code value`) to the touchscreen node.

    For anything the gesture helpers do not cover. Linux input codes: EV_SYN=0,
    EV_KEY=1, EV_ABS=3; ABS_MT_POSITION_X=0x35, ABS_MT_POSITION_Y=0x36,
    ABS_MT_TRACKING_ID=0x39, BTN_TOUCH=0x14A.

    Each call is its own `adb shell`, so successive events are ~250 ms apart. For
    a tap or a gesture that is too slow — use `mumu_raw_sequence` to send the whole
    sequence in one round trip.
    """
    return gestures().raw_event(vmindex, event_type, code, value, device=device or None)


@mcp.tool()
@_wrap
def mumu_raw_sequence(vmindex: int = 0, events: Any = None, device: str = "") -> dict:
    """Send a batch of raw input events in one adb round trip.

    `events` is a list of `[type, code, value]` triples. A tap is
    `[[3,47,0],[3,57,300],[3,53,x],[3,54,y],[1,330,1],[0,0,0],[3,57,-1],[1,330,0],[0,0,0]]`.

    Batching is not an optimization, it is what makes the gesture work: splitting
    a down/up across separate shell calls stretches it into a slow drag, and apps
    ignore that. Verified by tapping a launcher icon this way and watching the
    launcher hand over.
    """
    parsed = events
    if isinstance(events, str):
        try:
            parsed = json.loads(events)
        except json.JSONDecodeError as exc:
            raise BadArgument(f"events is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise BadArgument("events must be a non-empty list of [type, code, value]")
    triples: list[tuple[int, int, int]] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise BadArgument(f"event {i} must be [type, code, value]",
                              detail={"got": item})
        triples.append((int(item[0]), int(item[1]), int(item[2])))
    return gestures().raw_sequence(vmindex, triples, device=device or None)


# ---------------------------------------------------------------------------
# environment surface: scan and hide
# ---------------------------------------------------------------------------
def stealth() -> Stealth:
    inst = _state.get("stealth")
    if inst is None:
        inst = Stealth(adb=adb(), root=root(), manager=manager())
        _state["stealth"] = inst
    return inst


@mcp.tool()
@_wrap
def mumu_stealth_scan(vmindex: int = 0) -> dict:
    """Enumerate what this instance leaks — emulator traces and root traces — and grade each.

    Read-only. Every finding is `clean`, `leak` (reversible) or `unfixable`, and the
    headline `verdict` keys on the unfixable ones: a single structural tell is
    enough for an integrity check to fail, whatever else is cleaned up.

    Needs root, because half the surface is unreadable as `shell`.
    """
    return stealth().scan(vmindex).to_dict()


@mcp.tool()
@_wrap
def mumu_stealth_hide(vmindex: int = 0, properties: bool = True, root_hide: bool = True,
                      restore: bool = False) -> dict:
    """Close the reversible part of the surface, then re-scan and report what is left.

    What it actually does:

    * rewrites `ro.*` properties through KernelSU's `resetprop`, which writes the
      property area directly — `setprop` refuses `ro.` keys after init has read
      them, so this is the only mechanism that works
    * blanks any property value carrying a vendor marker (`nemu`, `mumu`, `qemu`,
      `goldfish`, …)
    * drops adbd from root to `shell`, and sets SELinux to `Enforcing`

    What it deliberately does **not** fake, and says so in `remaining`: sensor
    count, the uname/cpuinfo/kallsyms disagreement, loaded modules, and the
    KernelSU directories. Those need a kernel hook or a faked HAL, and a half-done
    version of either is a *new* signal rather than a fix.

    `restore=True` puts back the values captured at the first call, so nothing here
    is a one-way door. A reboot also restores them, since `resetprop` does not
    persist.
    """
    return stealth().hide(vmindex, properties=properties, root=root_hide, restore=restore)


@mcp.tool()
@_wrap
def mumu_stealth_deep(vmindex: int = 0, cpuinfo: bool = True, dmi: bool = True,
                      clocksource: bool = True, osrelease: bool = True) -> dict:
    """Cover the files a professional integrity check opens, with bind mounts.

    This is the layer above `mumu_stealth_hide`. Properties are visible to a
    `getprop`; these files are what a check reads directly when it wants to know
    whether it is on real hardware:

    | file | real value here | after |
    |---|---|---|
    | `/proc/cpuinfo` | `vendor_id: AuthenticAMD` under `uname -m = aarch64` | 8-core ARM profile |
    | `/sys/class/dmi/id/*` | `innotek GmbH` / `NiohBox` (**VirtualBox**) | Samsung SM-S9210 |
    | `clocksource` | `kvm-clock` | `arch_sys_counter` |
    | `/proc/sys/kernel/osrelease` | `6.1.90-perf+` | a plausible Samsung 6.1 build |

    `mount --bind` is what makes this work: those paths are read-only, but a bind
    mount changes what a read returns, and unlike a kernel hook it leaves no code
    patch to find.

    What it cannot do: `uname(2)` is a syscall, not a file. `uname -m` still
    reports the truth, and closing that needs an LD_PRELOAD shim (per-process,
    useless against a game's own loader) or a kernel hook (a stronger signal than
    the leak). The result says so under `uname`.

    Runtime-only: a reboot removes the mounts. `mumu_stealth_deep_restore` does it
    without a reboot.
    """
    return {"applied": stealth().deep_hide(vmindex, cpuinfo=cpuinfo, dmi=dmi,
                                           clocksource=clocksource, osrelease=osrelease),
            "verified": stealth()._verify_fakes(vmindex)}


@mcp.tool()
@_wrap
def mumu_stealth_deep_restore(vmindex: int = 0) -> dict:
    """Unmount every override installed by `mumu_stealth_deep` and delete the staged files."""
    return stealth().deep_restore(vmindex)


@mcp.tool()
@_wrap
def mumu_stealth_features(vmindex: int = 0) -> dict:
    """List KernelSU's kernel-side switches — where `selinux_hide` and others live.

    These are neither properties nor files: they are kernel features, so nothing
    else in this server would surface them. `selinux_hide` is the important one —
    it makes a Permissive build read as Enforcing to app UIDs, which is the only
    way to close that tell without taking the instance down.
    """
    return stealth().features(vmindex)


@mcp.tool()
@_wrap
def mumu_stealth_set_feature(vmindex: int = 0, name: str = "", enabled: bool = True) -> dict:
    """Toggle one KernelSU feature and read it back.

    `selinux_hide` sanitises `/sys/fs/selinux` for app UIDs. Note the scope: root
    and shell still read the real value, so a scan running as root will report
    Permissive either way — that is why `mumu_stealth_scan` checks the feature flag
    rather than trusting its own `getenforce`.
    """
    return stealth().set_feature(vmindex, name, enabled)


# ---------------------------------------------------------------------------
# syscall-level uname patch
# ---------------------------------------------------------------------------
def uname_patcher() -> UnamePatcher:
    inst = _state.get("uname")
    if inst is None:
        inst = UnamePatcher(adb=adb(), root=root())
        _state["uname"] = inst
    return inst


@mcp.tool()
@_wrap
def mumu_uname_status(vmindex: int = 0) -> dict:
    """What `uname` reports right now, and whether a tracer is holding a pid.

    `uname(2)` is the last tell that properties and bind mounts cannot reach — it is
    a syscall, not a file.
    """
    return uname_patcher().status(vmindex)


@mcp.tool()
@_wrap
def mumu_uname_selftest(vmindex: int = 0) -> dict:
    """Prove the syscall rewrite works, with a built-in target that calls uname itself.

    Worth running before trusting the patch on a real app. Testing with `uname` from
    a shell gives a false negative: the shell forks, so the syscall happens in a
    child the tracer never attached to. The bundled target calls it in its own
    thread, and this reports the observable before/after strings.
    """
    return uname_patcher().self_test(vmindex)


@mcp.tool()
@_wrap
def mumu_uname_patch(vmindex: int = 0, pid: int = 0, settle_s: int = 3) -> dict:
    """Attach the ptrace tracer to `pid` so its `uname` reports a handset kernel.

    This is the only mechanism that covers a target which calls the syscall
    directly, ships its own loader, or clears `LD_PRELOAD` — it sits below libc.

    Two costs, stated rather than discovered later:

    * every traced thread pays a ptrace stop per syscall, so the target runs slower;
    * `PTRACE_O_EXITKILL` is set, so killing the tracer kills the tracee too. That
      keeps a crashed tracer from leaving a game permanently stopped.

    Find the pid with `mumu_processes`. Needs root.
    """
    return uname_patcher().attach(vmindex, pid, settle_s=settle_s)


@mcp.tool()
@_wrap
def mumu_uname_detach(vmindex: int = 0, pid: int = 0) -> dict:
    """Stop the tracer. The traced process exits too — restart it afterwards."""
    return uname_patcher().detach(vmindex, pid)


@mcp.tool()
@_wrap
def mumu_uname_verify(vmindex: int = 0, pid: int = 0, addr: str = "0x204200") -> dict:
    """Read the utsname bytes out of the tracee's memory to confirm the write landed.

    A `PTRACE_POKEDATA` that returns zero only means the kernel accepted the call;
    reading it back through `/proc/<pid>/mem` is what proves the target will see the
    value. This is the check that caught an argument-order bug in the tracer.
    """
    if pid <= 0:
        raise BadArgument("pid is required")
    return uname_patcher().verify_struct(vmindex, pid, addr)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="mumu-mcp", description="MCP server for MuMu Player")
    parser.add_argument("--mumu-home", "--mumu-path", dest="mumu_home",
                        help="MuMu install dir (contains MuMuManager.exe) or the binary itself")
    parser.add_argument("--adb", dest="adb", help="force an adb endpoint, host:port")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--list-tools", action="store_true", help="print the tool table and exit")
    parser.add_argument("--diagnose", action="store_true", help="print a health report and exit")
    args = parser.parse_args(argv)

    if args.mumu_home:
        os.environ["MUMU_HOME"] = args.mumu_home
    if args.adb:
        os.environ["MUMU_ADB"] = args.adb

    if args.list_tools:
        tools = mcp._tool_manager._tools
        print(f"mumu-mcp — {len(tools)} tools\n")
        for name in sorted(tools):
            print(f"  {name:26s} {(tools[name].description or '').splitlines()[0][:78]}")
        return 0

    if args.diagnose:
        print(json.dumps(mumu_diagnose.__wrapped__(), indent=2, ensure_ascii=False))
        return 0

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
