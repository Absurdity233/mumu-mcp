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
from .errors import (
    AdbUnavailable,
    BadArgument,
    MuMuError,
    Timeout,
)
from .manager import Manager, as_list

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

_state: dict[str, Any] = {"manager": None, "adb": None}


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


@mcp.tool()
@_wrap
def mumu_set_root(vmindex: int = 0, enabled: bool = True) -> dict:
    """Toggle the root permission flag for the instance."""
    return {"root": enabled,
            "result": manager().setting_set(vmindex, {"root_permission": "true" if enabled else "false"})}


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
