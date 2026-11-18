"""System-wide uname spoofing without a kernel module.

The module route is blocked on this kernel by something I could not pin down without its
source: `do_init_module` is called with a correctly-wired `struct module`, `.init.text` is
freed afterwards (so init did run), yet `sys_call_table[63]` is unchanged and the module's
own `.data` is untouched. Every structural check passes and the effect never appears,
which is the signature of a kernel whose module loader does something the upstream source
does not describe.

What *does* work here, verified end to end, is `ptrace`: attach to a process, break on
syscall exit, rewrite the `utsname` in its address space. That covers a target which calls
`uname` directly, ships its own loader, or clears `LD_PRELOAD`, because it sits below libc.

This module turns the one-shot tracer into a standing watcher: it scans `/proc` for new
processes, attaches to the ones that matter, and keeps a record of what it is holding.

Honest costs, stated rather than discovered later:

* every traced thread pays a ptrace stop per syscall, so tracked processes run slower;
* a process is untraced for the brief window between `exec` and the attach, so a target
  that calls `uname` in its first instructions can still observe the truth once;
* `PTRACE_O_EXITKILL` means a tracer crash takes the tracee with it.

So this is a debugging/analysis facility, not something to leave running silently in the
background of a shipping app.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .adb import Adb
from .errors import BadArgument
from .root import Root

log = logging.getLogger("mumu_mcp.uname_daemon")

DEVICE_BIN = "/data/local/tmp/uname_patch"
DEVICE_PIDFILE = "/data/local/tmp/mumu_uname_daemon.pids"
DEVICE_LOGDIR = "/data/local/tmp"


@dataclass
class DaemonState:
    """What the watcher is currently holding."""

    running: bool = False
    pid: int = 0
    targets: list[int] = field(default_factory=list)
    started_at: float = 0.0
    log: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "daemon_pid": self.pid,
            "targets": list(self.targets),
            "uptime_s": round(time.monotonic() - self.started_at, 1) if self.started_at else 0,
            "log_tail": self.log[-800:] if self.log else "",
        }


class UnameDaemon:
    """Start, inspect and stop a standing set of uname tracers."""

    def __init__(self, adb: Adb, root: Root):
        self.adb = adb
        self.root = root
        self.state = DaemonState()

    # -- helpers ----------------------------------------------------------
    def _require_root(self, vmindex: int) -> None:
        if not self.root.state(vmindex).is_root:
            raise BadArgument("the uname watcher needs root", detail={"fix": "mumu_root_adb(True)"})

    def _ensure_binary(self, vmindex: int) -> None:
        from pathlib import Path

        from .uname import NATIVE_DIR, UnamePatcher

        patcher = UnamePatcher(self.adb, self.root)
        local = NATIVE_DIR / "uname_patch"
        if not local.is_file():
            raise BadArgument(
                "the tracer binary is not built",
                detail={"hint": "see native/README.md", "path": str(Path(local))},
            )
        patcher.ensure_binaries(vmindex)

    # -- start ------------------------------------------------------------
    def start(self, vmindex: int = 0, *, names: list[str] | None = None,
              limit: int = 8) -> dict[str, Any]:
        """Attach the tracer to matching processes and report what it holds.

        `names` is a list of process-name substrings. When omitted it falls back to a
        small set that matters for this use: the shell tools a check would call, plus any
        app package the caller inspects separately.

        `limit` caps how many processes are traced. Each one costs a ptrace stop per
        syscall, so an unbounded fan-out would make the whole instance crawl.
        """
        self._require_root(vmindex)
        self._ensure_binary(vmindex)

        wanted = names or ["uname", "toybox", "sh", "getprop"]
        found = self._find_targets(vmindex, wanted, limit)

        attached: list[dict[str, Any]] = []
        for pid, name in found:
            result = self._attach_one(vmindex, pid)
            attached.append({"pid": pid, "name": name, **result})

        self.state = DaemonState(
            running=any(a.get("attached") for a in attached),
            pid=self._first_tracer_pid(vmindex),
            targets=[a["pid"] for a in attached if a.get("attached")],
            started_at=time.monotonic(),
        )
        self.state.log = self._read_log(vmindex)

        return {
            "attached": attached,
            "targets": self.state.targets,
            "state": self.state.as_dict(),
            "note": (
                "each traced process pays a ptrace stop per syscall; a process can still "
                "observe the real uname in the window between exec and attach"
            ),
        }

    def _find_targets(self, vmindex: int, wanted: list[str], limit: int) -> list[tuple[int, str]]:
        listing = self.adb.shell(
            vmindex, "ps -A -o PID,ARGS 2>/dev/null || ps -A 2>/dev/null")
        out: list[tuple[int, str]] = []
        seen: set[int] = set()
        for line in listing.splitlines():
            parts = line.split(None, 1)
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            pid, args = int(parts[0]), parts[1]
            if pid in seen or pid <= 1:
                continue
            if any(w in args for w in wanted):
                seen.add(pid)
                out.append((pid, args[:60]))
                if len(out) >= limit:
                    break
        return out

    def _attach_one(self, vmindex: int, pid: int) -> dict[str, Any]:
        log_path = f"{DEVICE_LOGDIR}/uname_patch.{pid}.log"
        self.adb.shell(
            vmindex,
            f"rm -f {log_path}; nohup {DEVICE_BIN} {pid} > {log_path} 2>&1 & echo started",
        )
        deadline = time.monotonic() + 4.0
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.4)
            text = self.adb.shell(vmindex, f"cat {log_path} 2>/dev/null").strip()
            if "rewrote a uname result" in text:
                return {"attached": True, "verified": "a rewrite was reported", "log": text}
            if "attach failed" in text:
                return {"attached": False, "error": text.splitlines()[-1] if text else ""}
        return {"attached": "attached" in text, "log": text}

    def _first_tracer_pid(self, vmindex: int) -> int:
        out = self.adb.shell(
            vmindex, "ps -A -o PID,ARGS 2>/dev/null | grep uname_patch | grep -v grep | head -1")
        if out.strip():
            first = out.split()[0]
            if first.isdigit():
                return int(first)
        return 0

    def _read_log(self, vmindex: int) -> str:
        return self.adb.shell(
            vmindex, "for f in /data/local/tmp/uname_patch.*.log; do echo \"== $f\"; "
                      "tail -3 $f; done 2>/dev/null").strip()

    # -- inspect / stop ---------------------------------------------------
    def status(self, vmindex: int = 0) -> dict[str, Any]:
        """Report which processes are traced and what they report."""
        tracers = self.adb.shell(
            vmindex, "ps -A -o PID,ARGS 2>/dev/null | grep uname_patch | grep -v grep").strip()
        pids: list[int] = []
        for line in tracers.splitlines():
            parts = line.split()
            if parts and parts[0].isdigit():
                pids.append(int(parts[0]))

        return {
            "tracers": pids,
            "count": len(pids),
            "uname_now": self.adb.shell(vmindex, "uname -r").strip(),
            "log_tail": self._read_log(vmindex)[-600:] if pids else "",
            "cost": "one ptrace stop per syscall per traced thread",
        }

    def stop(self, vmindex: int = 0, pid: int = 0) -> dict[str, Any]:
        """Stop tracers. Note that this also ends the processes they hold."""
        pattern = f"uname_patch {pid}" if pid else "uname_patch"
        before = self.adb.shell(
            vmindex, f"ps -A -o PID,ARGS 2>/dev/null | grep '{pattern}' | grep -v grep").strip()
        if not before:
            self.state.running = False
            return {"stopped": 0, "message": "nothing traced"}
        self.adb.shell(vmindex, f"pkill -f '{pattern}' 2>/dev/null; sleep 1")
        after = self.adb.shell(
            vmindex, f"ps -A -o PID,ARGS 2>/dev/null | grep '{pattern}' | grep -v grep").strip()
        self.state = DaemonState()
        return {
            "stopped": len(before.splitlines()),
            "still_running": len(after.splitlines()),
            "warning": "PTRACE_O_EXITKILL is set, so the tracees exited too; restart them",
        }