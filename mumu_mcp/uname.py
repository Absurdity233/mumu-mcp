"""Kernel-identity patching at the syscall layer.

`uname(2)` is the last tell that neither `resetprop` nor bind mounts can reach: it
is a syscall, not a file, and `/proc/sys/kernel/osrelease` is read-only. Verified on
this image — after covering `/proc/version`, `uname -r` still printed the real
kernel.

Two mechanisms were written; only the first ships as a binary.

* `uname_patch` (shipped) — a ptrace tracer. Attaches to a pid, breaks on syscall
  exit, and rewrites the utsname struct in the tracee. Works no matter how the
  target calls uname, because it sits below libc. Cost: one ptrace stop per
  syscall.
* `uname_hook` (source only, `native/uname_hook.h`) — an LD_PRELOAD shim. Cheaper
  (no ptrace), but useless against a process that ships its own loader, calls the
  syscall directly, or clears the environment. It needs a real `dlopen`, so it
  cannot be built freestanding; the header keeps the expected values in one place
  for whenever a sysroot is available.

The tracer is written freestanding: this host has clang but no Android sysroot, and
the device has no headers either, so a normal C build cannot link.

Measured end to end on the live instance: a target calling uname in a loop reported
`6.1.75-android14-11-g4d9e0d1b2c3f|aarch64` while the tracer was attached, and
`6.1.90-perf+|aarch64` after it detached. The struct bytes were also read back
through `/proc/<pid>/mem` to prove the write landed where the target reads from,
not merely that `PTRACE_POKEDATA` returned zero.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .adb import Adb
from .errors import AdbUnavailable, BadArgument, MuMuError
from .root import Root

log = logging.getLogger("mumu_mcp.uname")

DEVICE_BIN = "/data/local/tmp/uname_patch"
DEVICE_TARGET = "/data/local/tmp/uname_target"
NATIVE_DIR = Path(__file__).resolve().parent.parent / "native"

# Values that must match fakeprofile.py and the module's cpuinfo, or the surface
# contradicts itself in a way a check can compare across files.
EXPECT_RELEASE = "6.1.75-android14-11-g4d9e0d1b2c3f"
EXPECT_MACHINE = "aarch64"


class UnamePatcher:
    """Install and drive the syscall-level uname rewrite."""

    def __init__(self, adb: Adb, root: Root):
        self.adb = adb
        self.root = root

    # -- installation ------------------------------------------------------
    def ensure_binaries(self, vmindex: int = 0, *, rebuild: bool = False) -> dict[str, Any]:
        """Push the tracer (and the probe target) to the device if missing.

        `rebuild` re-pushes unconditionally, which is what you want after a host-side
        rebuild — the device copy is otherwise kept.
        """
        out: dict[str, Any] = {"pushed": [], "missing": []}
        for local_name, remote in (("uname_patch", DEVICE_BIN), ("uname_target", DEVICE_TARGET)):
            local = NATIVE_DIR / local_name
            if not local.is_file():
                out["missing"].append(str(local))
                continue
            present = self.adb.shell(vmindex, f"ls -l {remote} 2>/dev/null").strip()
            if present and not rebuild:
                continue
            self.adb.push(vmindex, str(local), remote)
            self.adb.shell(vmindex, f"chmod 755 {remote}")
            out["pushed"].append(remote)
        if out["missing"]:
            raise BadArgument(
                "native binaries are not built; run the build step first",
                detail={"missing": out["missing"],
                        "hint": "see native/README.md — clang --target=x86_64-linux-android26 -nostdlib"},
            )
        return out

    # -- status ------------------------------------------------------------
    def status(self, vmindex: int = 0) -> dict[str, Any]:
        """What `uname` reports now, and whether a tracer is holding a pid."""
        release = self.adb.shell(vmindex, "uname -r").strip()
        machine = self.adb.shell(vmindex, "uname -m").strip()
        tracer = self.adb.shell(
            vmindex, "ps -A -o PID,ARGS 2>/dev/null | grep uname_patch | grep -v grep"
        ).strip()
        traced_pid: int | None = None
        if tracer:
            first = tracer.splitlines()[0].split()
            if first and first[0].isdigit():
                traced_pid = int(first[0])
        return {
            "release": release,
            "machine": machine,
            "patched": release == EXPECT_RELEASE and machine == EXPECT_MACHINE,
            "tracer_pid": traced_pid,
            "expected": {"release": EXPECT_RELEASE, "machine": EXPECT_MACHINE},
        }

    # -- attach / detach ---------------------------------------------------
    def attach(self, vmindex: int, pid: int, *, settle_s: float = 3.0) -> dict[str, Any]:
        """Start the tracer against `pid` and confirm it reported a rewrite.

        The tracer stays attached for as long as it runs, and `PTRACE_O_EXITKILL`
        means killing it also kills the tracee — so `detach` is the only way out
        that leaves the target alive. That trade is deliberate: a crashed tracer
        must not leave a game half-instrumented.
        """
        if pid <= 0:
            raise BadArgument("pid must be positive")
        state = self.root.state(vmindex)
        if not state.is_root:
            raise BadArgument("ptrace needs root", detail={"fix": "mumu_root_adb(True)"})

        self.ensure_binaries(vmindex)
        alive = self.adb.shell(vmindex, f"test -d /proc/{pid} && echo yes || echo no").strip()
        if alive != "yes":
            raise BadArgument(f"pid {pid} does not exist")

        log_path = f"/data/local/tmp/uname_patch.{pid}.log"
        self.adb.shell(
            vmindex,
            f"rm -f {log_path}; nohup {DEVICE_BIN} {pid} > {log_path} 2>&1 & echo started",
        )
        deadline = time.monotonic() + max(1.0, settle_s)
        logtext = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            logtext = self.adb.shell(vmindex, f"cat {log_path} 2>/dev/null").strip()
            if "rewrote a uname result" in logtext:
                return {"attached": True, "pid": pid, "log": logtext,
                        "verified": "a rewrite was reported"}
            if "attach failed" in logtext:
                raise AdbUnavailable(
                    "ptrace attach failed inside the guest",
                    detail={"log": logtext,
                            "hint": "needs root; check /proc/sys/kernel/yama/ptrace_scope"},
                )
        return {
            "attached": "attached" in logtext,
            "pid": pid,
            "log": logtext,
            "verified": "attached but no rewrite seen yet — the target may not call uname",
        }

    def detach(self, vmindex: int, pid: int = 0) -> dict[str, Any]:
        """Stop the tracer.

        Note what this costs: `PTRACE_O_EXITKILL` is set, so killing the tracer takes
        the tracee with it. That is the intended contract — the alternative is a
        target left permanently stopped when the tracer dies unexpectedly. If the
        target must survive, restart it instead of detaching.
        """
        pattern = f"uname_patch {pid}" if pid else "uname_patch"
        before = self.adb.shell(
            vmindex, f"ps -A -o PID,ARGS 2>/dev/null | grep '{pattern}' | grep -v grep").strip()
        if not before:
            return {"detached": False, "message": "no tracer running"}
        self.adb.shell(vmindex, f"pkill -f '{pattern}' 2>/dev/null; sleep 1")
        after = self.adb.shell(
            vmindex, f"ps -A -o PID,ARGS 2>/dev/null | grep '{pattern}' | grep -v grep").strip()
        return {
            "detached": not after,
            "tracee_note": "the traced process exits too (PTRACE_O_EXITKILL); restart it",
            "status": self.status(vmindex),
        }

    # -- self-test ---------------------------------------------------------
    def self_test(self, vmindex: int = 0) -> dict[str, Any]:
        """Prove the rewrite works, using a built-in target that calls uname itself.

        This exists because `uname` from a shell always forks: the syscall happens in
        a child the tracer never attached to, so testing that way reports a false
        negative. `uname_target` calls the syscall in its own thread and writes the
        result out, which is what makes a rewrite observable.
        """
        out: dict[str, Any] = {}
        self.ensure_binaries(vmindex)
        self.adb.shell(vmindex, "pkill -f uname_target 2>/dev/null; sleep 1; "
                                "rm -f /data/local/tmp/uname_target.out")
        self.adb.shell(vmindex, f"nohup {DEVICE_TARGET} >/dev/null 2>&1 & echo started")
        time.sleep(2)

        pid_line = self.adb.shell(
            vmindex, "ps -A -o PID,ARGS 2>/dev/null | grep uname_target | grep -v grep").strip()
        if not pid_line:
            raise MuMuError("the test target did not start")
        pid = int(pid_line.splitlines()[0].split()[0])
        out["pid"] = pid
        out["before"] = self.adb.shell(
            vmindex, "tail -1 /data/local/tmp/uname_target.out 2>/dev/null").strip()

        attach = self.attach(vmindex, pid, settle_s=6.0)
        out["attach"] = attach

        # The target writes release, "|", machine and "\n" as four separate write
        # calls, so tail -1 can catch a half-written line. Poll until a complete
        # "<release>|<machine>" line appears, or give up.
        deadline = time.monotonic() + 8.0
        last = ""
        while time.monotonic() < deadline:
            time.sleep(0.6)
            last = self.adb.shell(
                vmindex,
                "grep -E '^[0-9].*\\|.*$' /data/local/tmp/uname_target.out 2>/dev/null | tail -1",
            ).strip()
            if EXPECT_RELEASE in last and EXPECT_MACHINE in last:
                break
        out["after"] = last

        self.adb.shell(vmindex, "pkill -f uname_patch 2>/dev/null; "
                                "pkill -f uname_target 2>/dev/null")
        out["passed"] = EXPECT_RELEASE in last and EXPECT_MACHINE in last
        out["expected_prefix"] = f"{EXPECT_RELEASE}|{EXPECT_MACHINE}"
        if not out["passed"]:
            out["diagnosis"] = (
                "the rewrite was not observed — check that the target was still running "
                "and that adbd is root (ptrace needs it)"
            )
        return out

    # -- syscall verification ---------------------------------------------
    def verify_struct(self, vmindex: int, pid: int, addr: str = "0x204200") -> dict[str, Any]:
        """Read the utsname bytes straight out of the tracee's memory.

        A `PTRACE_POKEDATA` that returns zero is not proof the target will see the
        value; reading it back through `/proc/<pid>/mem` is. This is the check that
        caught an argument-order bug in the tracer.
        """
        raw = self.adb.shell(
            vmindex,
            f"dd if=/proc/{pid}/mem bs=1 skip=$(({addr})) count=200 2>/dev/null "
            f"| tr -d '\\0' | head -c 200",
        ).strip()
        return {
            "pid": pid, "addr": addr, "raw": raw,
            "has_fake_release": EXPECT_RELEASE in raw,
            "has_fake_machine": EXPECT_MACHINE in raw,
            "patched": EXPECT_RELEASE in raw and EXPECT_MACHINE in raw,
        }
