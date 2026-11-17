"""Root: making the emulator a root shell, and what that actually means here.

Verified behaviour on MuMu 6.8.x / Android 15 (NX engine), measured rather than
assumed — every claim below was checked on a live instance:

**There is no `su`.** `root_permission=true` does not install one. It starts
**adbd as root** (`uid=0`, SELinux context `u:r:su:s0`) and sets
`service.adb.root=1`, which survives reboots. Consequences:

* `mumu_shell` already runs as root — nothing needs to call `su -c`.
* Anything that assumes a `su` binary fails. Privilege *drop* has to go through
  `run-as <package>` or a second `pm`/`am` invocation, not `su <uid> -c`.

**`/system` is read-only at the block level and writable in practice.**
`/dev/block/sda6` is mounted `ro`, so `mount -o rw,remount /system` fails with
`'/dev/block/sda6' is read-only`. But when `system_disk_readonly=false`, MuMu
stacks an overlayfs on top:

    overlay on /system type overlay (rw,
        lowerdir=/system, upperdir=/mnt/scratch/upperdir, workdir=/mnt/scratch/workdir)

The upperdir is **not visible inside the guest** — `/mnt/scratch` is an empty
tmpfs there. It is backed on the host by `<vm>/system-diff.vdi`, which grows as
you write (9.4 MB → 10.5 MB after a single file) and whose mtime updates on
guest `sync`. So:

* writes are **persistent across guest reboots** (verified),
* they land in a **diff image**, leaving the stock `system` image untouched,
* `.vdi` is the documented MuMu/VirtualBox disk format, so an export/import round
  trip is the reliable way to reset it.

That distinction drives the API: `set_system_writable` is not "edit the system
image", and there is no honest "wipe the overlay from inside the guest" — the
reset path is `restore_system_diff`, which the caller uses with the instance
stopped.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adb import Adb
from .errors import AdbUnavailable, BadArgument, MuMuError
from .manager import Manager

log = logging.getLogger("mumu_mcp.root")

OVERLAY_RE = re.compile(r"overlay on /system type overlay \(([^)]*)\)")
SYSTEM_MOUNT_RE = re.compile(r"(\S+) on /system type (\S+) \(([^)]*)\)")
DIFF_IMAGE = "system-diff.vdi"


@dataclass
class RootState:
    """What root looks like on this instance right now."""

    vmindex: int
    uid: int
    user: str
    selinux: str
    context: str
    is_root: bool
    system_writable: bool
    overlay: bool
    upperdir: str
    upperdir_visible: bool
    diff_image: str | None
    diff_image_bytes: int | None
    adb_root_prop: int
    root_permission: str | None
    su_binary: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "vmindex": self.vmindex,
            "is_root": self.is_root,
            "uid": self.uid,
            "user": self.user,
            "selinux": self.selinux,
            "context": self.context,
            "system_writable": self.system_writable,
            "system_overlay": self.overlay,
            "overlay_upperdir": self.upperdir or None,
            "overlay_upperdir_visible": self.upperdir_visible,
            "system_diff_image": self.diff_image,
            "system_diff_bytes": self.diff_image_bytes,
            "service_adb_root": self.adb_root_prop,
            "root_permission": self.root_permission,
            "su_binary": self.su_binary,
            "notes": notes(self),
        }


def notes(state: RootState) -> list[str]:
    out: list[str] = []
    if state.is_root:
        out.append("adbd runs as uid=0: mumu_shell already has root, no `su` needed")
    else:
        out.append("adbd is NOT root: mumu_root_adb, or root_permission=true + restart")
    if not state.su_binary:
        out.append("this image ships no `su`; drop privileges with `run-as <package>`, not `su <uid>`")
    if state.system_writable and state.overlay:
        out.append("`/system` writes go to the overlay diff image and persist across reboots")
        if not state.upperdir_visible:
            out.append("the upperdir is host-side; it is not reachable from inside the guest")
    elif not state.system_writable:
        out.append("`/system` is read-only; mumu_root_system(True) enables the overlay")
    if state.selinux != "Enforcing":
        out.append(f"SELinux is {state.selinux}: policy denials will not block you")
    return out


def _field(blob: str, name: str) -> str:
    """Pull `NAME:value` out of the sentinel-tagged state dump."""
    for line in blob.splitlines():
        if line.startswith(f"{name}:"):
            return line[len(name) + 1:].strip()
    return ""


class Root:
    """Root inspection and manipulation for one MuMu install."""

    def __init__(self, adb: Adb, manager: Manager):
        self.adb = adb
        self.manager = manager

    # -- paths -------------------------------------------------------------
    def vm_dir(self, vmindex: int = 0) -> Path | None:
        """The host-side VM directory, derived from the install layout."""
        home = self.manager.home
        root = home.parent  # ...\Netease\MuMu
        vms = root / "vms"
        if not vms.is_dir():
            return None
        for cand in sorted(vms.glob(f"MuMuPlayer-*-{vmindex}")):
            if cand.is_dir():
                return cand
        return None

    def changed_settings(self, vmindex: int = 0) -> dict[str, str]:
        """MuMu settings that decide how root comes up at boot."""
        out: dict[str, str] = {}
        for key in ("root_permission", "system_disk_readonly"):
            try:
                payload = self.manager.call("setting", "--vmindex", vmindex,
                                            "--key", key, timeout=15)
                if isinstance(payload, dict) and key in payload:
                    out[key] = str(payload[key])
            except MuMuError:
                pass
        return out

    # -- inspection --------------------------------------------------------
    def state(self, vmindex: int = 0) -> RootState:
        """Read the whole picture in one adb round trip.

        Doing this as a dozen separate `shell` calls costs ~15 s and floods the
        log; the guest can answer all of it in a single `sh -c`. Fields are split
        on a sentinel so a value containing a newline cannot shift the parse.
        """
        script = (
            "echo ID:$(id); "
            "echo ENFORCE:$(getenforce 2>/dev/null); "
            "echo ADBROOT:$(getprop service.adb.root); "
            "echo SU:$(ls /system/bin/su /system/xbin/su 2>/dev/null | tr '\\n' ' '; which su 2>/dev/null); "
            "echo UPPER:$(test -d /mnt/scratch/upperdir && echo yes || echo no); "
            "mount"
        )
        blob = self.adb.shell(vmindex, script, timeout=30)

        ident = _field(blob, "ID")
        m = re.search(r"uid=(\d+)\(([^)]+)\)", ident)
        if not m:
            raise AdbUnavailable("could not read uid from the guest", detail={"output": blob[:400]})
        uid, user = int(m.group(1)), m.group(2)
        ctx_match = re.search(r"context=(\S+)", ident)

        mounts = blob
        overlay_match = OVERLAY_RE.search(mounts)
        overlay = overlay_match is not None
        upperdir = ""
        if overlay_match:
            for part in overlay_match.group(1).split(","):
                if part.startswith("upperdir="):
                    upperdir = part.split("=", 1)[1]
        writable = overlay
        if not overlay:
            plain = SYSTEM_MOUNT_RE.search(mounts)
            writable = bool(plain and "rw" in plain.group(3).split(","))

        selinux = _field(blob, "ENFORCE") or "Unknown"
        try:
            adb_root_prop = int(_field(blob, "ADBROOT") or 0)
        except ValueError:
            adb_root_prop = 0

        diff_image = None
        diff_bytes: int | None = None
        vm = self.vm_dir(vmindex)
        if vm:
            img = vm / DIFF_IMAGE
            if img.is_file():
                diff_image = str(img)
                diff_bytes = img.stat().st_size

        settings = self.changed_settings(vmindex)
        return RootState(
            vmindex=vmindex,
            uid=uid,
            user=user,
            selinux=selinux,
            context=ctx_match.group(1) if ctx_match else "",
            is_root=uid == 0,
            system_writable=writable,
            overlay=overlay,
            upperdir=upperdir,
            upperdir_visible=_field(blob, "UPPER").strip() == "yes",
            diff_image=diff_image,
            diff_image_bytes=diff_bytes,
            adb_root_prop=adb_root_prop,
            root_permission=settings.get("root_permission"),
            su_binary=bool(_field(blob, "SU").strip()),
        )

    # -- actions -----------------------------------------------------------
    def enable_adb_root(self, vmindex: int = 0) -> dict[str, Any]:
        """`adb root`: restart adbd as uid 0. The instance keeps running."""
        before = self.state(vmindex)
        if before.is_root:
            return {"changed": False, "state": before.to_dict(),
                    "message": "adbd is already running as root"}
        out = self.adb.run(vmindex, "root", timeout=30, connect=False)
        time.sleep(2.0)
        self.adb.invalidate(vmindex)
        after = self._settle(vmindex, lambda s: s.is_root)
        return {"changed": after.is_root, "output": out.strip(), "state": after.to_dict()}

    def disable_adb_root(self, vmindex: int = 0) -> dict[str, Any]:
        """`adb unroot`: drop adbd back to the shell user."""
        before = self.state(vmindex)
        if not before.is_root:
            return {"changed": False, "state": before.to_dict(),
                    "message": "adbd is already running as the shell user"}
        out = self.adb.run(vmindex, "unroot", timeout=30, connect=False)
        time.sleep(2.0)
        self.adb.invalidate(vmindex)
        after = self._settle(vmindex, lambda s: not s.is_root)
        return {"changed": not after.is_root, "output": out.strip(), "state": after.to_dict()}

    def set_permission(self, vmindex: int, enabled: bool) -> dict[str, Any]:
        """Toggle `root_permission`, which decides whether adbd starts as root at boot."""
        self.manager.setting_set(vmindex, {"root_permission": "true" if enabled else "false"})
        self.manager.run("control", "--vmindex", vmindex, "restart", timeout=90)
        state = self._wait_up(vmindex)
        return {
            "root_permission": enabled,
            "restarted": True,
            "state": state.to_dict(),
        }

    def set_system_writable(self, vmindex: int, writable: bool) -> dict[str, Any]:
        """Toggle `system_disk_readonly`, which is what stacks the /system overlay.

        `writable=False` turns the overlay off: the guest sees the stock image
        again, and the previously written files are still in the diff image until
        `restore_system_diff` removes it.
        """
        self.manager.setting_set(vmindex, {"system_disk_readonly": "false" if writable else "true"})
        self.manager.run("control", "--vmindex", vmindex, "restart", timeout=90)
        state = self._wait_up(vmindex)
        return {
            "system_writable": state.system_writable,
            "system_disk_readonly": not writable,
            "restarted": True,
            "state": state.to_dict(),
        }

    def restore_system_diff(self, vmindex: int = 0, *, confirm: bool = False) -> dict[str, Any]:
        """Reset `/system` to stock by removing the overlay diff image on the host.

        The stock image is never modified while the overlay is on, so deleting the
        diff is a complete and cheap restore — this is the undo for
        `set_system_writable(True)`. The instance must be stopped: the image is
        memory-mapped while it runs.

        `confirm` must be passed explicitly; the file is deleted, not archived.
        """
        if not confirm:
            raise BadArgument(
                "this deletes the /system overlay diff image; pass confirm=True",
                detail={"image": DIFF_IMAGE},
            )
        vm = self.vm_dir(vmindex)
        if vm is None:
            raise BadArgument(f"no VM directory found for index {vmindex}")
        img = vm / DIFF_IMAGE
        status = self._instance_state(vmindex)
        if status.get("is_process_started"):
            raise BadArgument(
                "stop the instance first (mumu_stop): the diff image is mapped while it runs",
                detail={"player_state": status.get("player_state")},
            )
        if not img.is_file():
            return {"removed": False, "path": str(img), "message": "no diff image; system is already stock"}
        size = img.stat().st_size
        img.unlink()
        return {
            "removed": True,
            "path": str(img),
            "bytes_reclaimed": size,
            "note": "the stock system image was never modified, so this is a complete restore",
        }

    def remount(self, vmindex: int, target: str = "/system") -> dict[str, Any]:
        """Best-effort `mount -o rw,remount`, with an honest explanation when it fails.

        On stock MuMu 6.x the block device is read-only and this cannot succeed;
        the working path is `set_system_writable(True)`.
        """
        if target not in {"/system", "/vendor", "/product", "/"}:
            raise BadArgument("target must be one of /system, /vendor, /product, /")
        out = self.adb.shell(vmindex, f"mount -o rw,remount {target} 2>&1")
        mounts = self.adb.shell(vmindex, f"mount | grep ' {target} '")
        writable = bool(re.search(r"\(rw", mounts)) or "type overlay" in mounts
        payload = {"target": target, "writable": writable,
                   "output": out.strip(), "mount": mounts.strip()}
        if not writable:
            payload["hint"] = (
                "the block device is read-only on this image; use mumu_root_system(True) "
                "to get MuMu's overlayfs, which does not require remounting"
            )
        return payload

    # -- helpers -----------------------------------------------------------
    def _instance_state(self, vmindex: int) -> dict[str, Any]:
        try:
            payload = self.manager.call("info", "--vmindex", vmindex, timeout=20)
            return payload if isinstance(payload, dict) else {}
        except MuMuError:
            return {}

    def _restart(self, vmindex: int) -> dict[str, Any]:
        self.manager.run("control", "--vmindex", vmindex, "restart", timeout=90)
        self.adb.invalidate(vmindex)
        return {"restarted": True}

    def _wait_up(self, vmindex: int, timeout: float = 150.0) -> RootState:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                booted = self.manager.sh(vmindex, "getprop sys.boot_completed", timeout=10).strip()
                if booted == "1":
                    self.adb.invalidate(vmindex)
                    return self.state(vmindex)
            except MuMuError:
                self.adb.invalidate(vmindex)
            time.sleep(3)
        raise AdbUnavailable(f"instance {vmindex} did not come back within {timeout}s")

    def _settle(self, vmindex: int, predicate, timeout: float = 20.0) -> RootState:
        """Poll until the guest reflects a change, or return the last reading."""
        deadline = time.monotonic() + timeout
        state = self.state(vmindex)
        while time.monotonic() < deadline:
            if predicate(state):
                return state
            time.sleep(1.0)
            try:
                state = self.state(vmindex)
            except MuMuError:
                time.sleep(1.0)
        return state
