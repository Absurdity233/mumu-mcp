"""Kernel-level `uname` spoofing, via a real out-of-tree module.

The module rewrites `init_uts_ns.name` — the single struct every `uname(2)` copies from. It
changes no code and needs no address patching, because `init_uts_ns` is one of the symbols
this kernel exports and lives in writable data.

Why the previous approaches could not work, all settled from the 6.1.90 source:

* `sys_call_table` is no longer used for dispatch on this kernel.
  arch/x86/entry/syscall_64.c says so in a comment, and `x64_sys_call()` is a `switch`
  that common.c calls directly. Patching a table entry changes a table nothing reads.
* `set_memory_rw` takes the `cpa_lock` spinlock and does a global TLB flush; calling it
  from module init wedged the guest.
* Neither of those matters here, because the data the syscall reads is writable already.

The module is built out-of-tree against the exact source that matches `uname -r`
(`android14-6.1-2024-08`, 6.1.90). `native/km/build.sh` records how — it is not a
one-liner on Windows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .adb import Adb
from .errors import BadArgument
from .root import Root

log = logging.getLogger("mumu_mcp.kmod")

KO_LOCAL = Path(__file__).resolve().parent.parent / "native" / "km" / "khide_hook.ko"
KO_DEVICE = "/data/local/tmp/khide_hook.ko"
MODULE_NAME = "khide_hook"

# What the module installs; must agree with fakeprofile.py and the module's own source.
EXPECT_RELEASE = "6.1.75-android14-11-g4d9e0d1b2c3f"
EXPECT_MACHINE = "aarch64"


class KernelUname:
    """Load, verify and unload the kernel-side uname rewrite."""

    def __init__(self, adb: Adb, root: Root):
        self.adb = adb
        self.root = root

    def _require_root(self, vmindex: int) -> None:
        if not self.root.state(vmindex).is_root:
            raise BadArgument("loading a kernel module needs root",
                              detail={"fix": "mumu_root_adb(True)"})

    def status(self, vmindex: int = 0) -> dict[str, Any]:
        """Whether the module is loaded, and what uname reports right now."""
        loaded = self.adb.shell(
            vmindex, f"lsmod 2>/dev/null | grep -c '^{MODULE_NAME} '").strip()
        release = self.adb.shell(vmindex, "uname -r").strip()
        machine = self.adb.shell(vmindex, "uname -m").strip()
        return {
            "loaded": loaded == "1",
            "module": MODULE_NAME,
            "release": release,
            "machine": machine,
            "spoofed": release == EXPECT_RELEASE and machine == EXPECT_MACHINE,
            "expected": {"release": EXPECT_RELEASE, "machine": EXPECT_MACHINE},
        }

    def load(self, vmindex: int = 0, *, rebuild: bool = False) -> dict[str, Any]:
        """Push the module and load it.

        `insmod` is tried first; if the kernel rejects it for an unexported symbol, the
        KernelSU loader is used instead, since it resolves kallsyms on the module's behalf.
        """
        self._require_root(vmindex)

        if not KO_LOCAL.is_file():
            raise BadArgument(
                "the kernel module is not built",
                detail={"expected": str(KO_LOCAL),
                        "hint": "see native/km/build.sh — it needs the 6.1.90 source tree"},
            )

        present = self.adb.shell(vmindex, f"ls {KO_DEVICE} 2>/dev/null").strip()
        if rebuild or not present:
            self.adb.push(vmindex, str(KO_LOCAL), KO_DEVICE)

        before = self.status(vmindex)

        # Unload a previous instance so a rebuild takes effect.
        self.adb.shell(vmindex, f"rmmod {MODULE_NAME} 2>/dev/null")

        out = self.adb.shell(vmindex, f"insmod {KO_DEVICE} 2>&1").strip()
        used = "insmod"
        if "Unknown symbol" in out:
            out = self.adb.shell(
                vmindex,
                f"timeout 30 /data/adb/ksu/bin/ksud insmod {KO_DEVICE} 2>&1").strip()
            used = "ksud insmod"

        after = self.status(vmindex)
        return {
            "loaded": after["loaded"],
            "via": used,
            "output": out,
            "before": {"release": before["release"], "machine": before["machine"]},
            "after": {"release": after["release"], "machine": after["machine"]},
            "verified": after["spoofed"],
            "note": "init_uts_ns.name is rewritten; no code patched, nothing bind-mounted",
        }

    def unload(self, vmindex: int = 0) -> dict[str, Any]:
        """Unload the module; `uname` returns to the real kernel identity."""
        out = self.adb.shell(vmindex, f"rmmod {MODULE_NAME} 2>&1").strip()
        after = self.status(vmindex)
        return {
            "unloaded": not after["loaded"],
            "output": out,
            "release": after["release"],
            "restored": after["release"] != EXPECT_RELEASE,
        }
