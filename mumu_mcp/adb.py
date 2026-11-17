"""ADB transport, with automatic endpoint discovery.

There is no single correct adb address for MuMu. MuMu 12 maps the guest onto a
NAT loopback port, `127.0.0.1:(16384 + index*32)`. MuMu 6.x (NX engine, Android
15) does **not** open that port: adbd listens on `:5555` inside the guest and
the emulator sits on a bridged LAN interface, so the only reachable address is
the guest's own wlan0 IP. Resolving this at call time is what makes the
difference between "12 tools work, 40 are empty" and everything working.

Resolution order per instance, cached:
  1. explicit override (MUMU_ADB_<index> env or --adb)
  2. loopback 16384 + index*32  (MuMu 12)
  3. guest wlan0 IP read through MuMuManager's own shell channel, port 5555
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AdbUnavailable, BadArgument, Timeout
from .manager import Manager, decode

log = logging.getLogger("mumu_mcp.adb")

IPV4 = re.compile(r"inet (\d{1,3}(?:\.\d{1,3}){3})/")
GUEST_ADB_PORT = 5555


def _port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0


# Text adb emits when the *connection* is gone, as opposed to a remote command
# that merely failed. Anything else with a non-zero exit is returned as output.
_TRANSPORT_ERRORS = (
    "device not found",
    "device offline",
    "no devices",
    "cannot connect",
    "connection refused",
    "unauthorized",
    "error: closed",
    "protocol fault",
)


def _transport_broken(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}".lower()
    return any(marker in blob for marker in _TRANSPORT_ERRORS)


@dataclass
class Adb:
    """ADB client bound to one MuMu install, with per-instance endpoints."""

    binary: Path
    manager: Manager
    explicit: str | None = None
    _endpoint: dict[int, str] = field(default_factory=dict, init=False, repr=False)
    _checked: dict[int, float] = field(default_factory=dict, init=False, repr=False)

    # -- endpoint ----------------------------------------------------------
    def endpoint(self, vmindex: int, *, refresh: bool = False) -> str:
        if self.explicit:
            return self.explicit
        env = os.environ.get(f"MUMU_ADB_{vmindex}")
        if env:
            return env
        if not refresh and vmindex in self._endpoint:
            return self._endpoint[vmindex]

        legacy_port = 16384 + vmindex * 32
        if _port_open("127.0.0.1", legacy_port):
            chosen = f"127.0.0.1:{legacy_port}"
            self._remember(vmindex, chosen, "NAT loopback")
            return chosen

        ip = self._guest_ip(vmindex)
        if not ip:
            raise AdbUnavailable(
                f"index {vmindex} has no reachable adb: the guest is not up yet",
                detail={
                    "tried": [f"127.0.0.1:{legacy_port}", "guest wlan0"],
                    "hint": "if the instance is starting, retry or call mumu_boot_ready first",
                },
            )
        chosen = f"{ip}:{GUEST_ADB_PORT}"
        self._remember(vmindex, chosen, "bridged LAN")
        return chosen

    def _remember(self, vmindex: int, endpoint: str, how: str) -> None:
        """Cache an endpoint, logging only when it actually changes.

        `state()`-style tools call `shell` a dozen times; logging the endpoint on
        every call turns the log into noise and hides the real messages.
        """
        if self._endpoint.get(vmindex) != endpoint:
            log.info("index %s: %s endpoint %s", vmindex, how, endpoint)
        self._endpoint[vmindex] = endpoint

    def _guest_ip(self, vmindex: int) -> str | None:
        try:
            out = self.manager.sh(vmindex, "ip addr show wlan0", timeout=15)
        except Exception:  # instance not up yet, shell unavailable, ...
            return None
        m = IPV4.search(out)
        return m.group(1) if m else None

    # -- transport ---------------------------------------------------------
    def run(self, vmindex: int, *args: Any, timeout: float = 30.0, connect: bool = True,
            retries: int = 1) -> str:
        last_error = ""
        for attempt in range(retries + 1):
            try:
                return self._run_once(vmindex, *args, timeout=timeout, connect=connect)
            except AdbUnavailable as exc:
                last_error = str(exc)
                transport = getattr(exc, "detail", {}).get("transport", False)
                if not transport or attempt >= retries:
                    raise
                # A restarted instance leaves a stale TCP socket in adb's table
                # ("device offline"). Dropping the cached endpoint and
                # reconnecting is what actually clears it.
                log.info("index %s: retrying after %s", vmindex, last_error.strip()[:60])
                self._endpoint.pop(vmindex, None)
                self._checked.clear()
                time.sleep(1.5)
        raise AdbUnavailable(last_error)  # pragma: no cover - loop always returns or raises

    def adb_endpoint_hint(self, vmindex: int) -> str:
        """Last known endpoint, for cache bookkeeping without a fresh probe."""
        return self._endpoint.get(vmindex, "")

    def invalidate(self, vmindex: int | None = None) -> None:
        """Forget a cached endpoint after the instance restarts.

        A restarted guest comes back with a dead socket, and adb reports
        `device offline` until it reconnects — so anything that restarts an
        instance must call this.
        """
        if vmindex is None:
            self._endpoint.clear()
        else:
            self._endpoint.pop(vmindex, None)
        self._checked.clear()

    def _run_once(self, vmindex: int, *args: Any, timeout: float = 30.0, connect: bool = True) -> str:
        target = self.endpoint(vmindex, refresh=connect)
        if connect:
            self.connect(target)
        argv = [str(self.binary), "-s", target, *[str(a) for a in args]]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                creationflags=_no_window(),
            )
        except subprocess.TimeoutExpired as exc:
            raise Timeout(f"adb did not answer in {timeout}s", detail={"argv": argv}) from exc
        out, err = decode(proc.stdout), decode(proc.stderr)
        if proc.returncode != 0 and _transport_broken(err, out):
            # Only a transport failure invalidates the cached endpoint. A remote
            # command that simply exits non-zero (uiautomator dump exits 1 after
            # printing its path, `pm clear` on a missing package, ...) must NOT
            # tear down a perfectly healthy connection.
            raise AdbUnavailable(
                err or out or f"adb exited {proc.returncode}",
                detail={"target": target, "argv": argv, "transport": True},
            )
        return out or err

    def connect(self, target: str) -> None:
        now = time.monotonic()
        last = self._checked.get(hash(target))
        if last and now - last < 5.0:
            return
        subprocess.run(
            [str(self.binary), "connect", target],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            creationflags=_no_window(),
        )
        self._checked[hash(target)] = now

    def shell(self, vmindex: int, command: str, *, timeout: float = 30.0) -> str:
        return self.run(vmindex, "shell", command, timeout=timeout)

    def shell_ok(self, vmindex: int, command: str, *, timeout: float = 30.0) -> bool:
        """Run a shell command and report success instead of text."""
        try:
            self.shell(vmindex, command, timeout=timeout)
            return True
        except (AdbUnavailable, Timeout):
            return False

    def push(self, vmindex: int, local: str, remote: str, *, timeout: float = 120.0) -> str:
        if not Path(local).exists():
            raise BadArgument(f"local path does not exist: {local}")
        return self.run(vmindex, "push", local, remote, timeout=timeout)

    def pull(self, vmindex: int, remote: str, local: str, *, timeout: float = 120.0) -> str:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        return self.run(vmindex, "pull", remote, local, timeout=timeout)

    def devices(self) -> list[str]:
        proc = subprocess.run(
            [str(self.binary), "devices"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            creationflags=_no_window(),
        )
        lines = decode(proc.stdout).splitlines()[1:]
        return [ln.split("\t")[0].strip() for ln in lines if "\tdevice" in ln]

    # -- high level --------------------------------------------------------
    def input_text(self, vmindex: int, text: str) -> str:
        """Type text. `input text` mangles spaces, so route through %s escaping."""
        escaped = (
            text.replace("\\", "\\\\")
            .replace(" ", "%s")
            .replace("'", "\\'")
            .replace('"', '\\"')
            .replace("&", "\\&")
            .replace("<", "\\<")
            .replace(">", "\\>")
            .replace("(", "\\(")
            .replace(")", "\\)")
            .replace("|", "\\|")
            .replace(";", "\\;")
        )
        return self.shell(vmindex, f"input text '{escaped}'")

    def screencap(self, vmindex: int, local: str, *, timeout: float = 60.0) -> str:
        remote = "/sdcard/.mumu_mcp_capture.png"
        self.shell(vmindex, f"screencap -p {remote}", timeout=timeout)
        self.pull(vmindex, remote, local, timeout=timeout)
        self.shell(vmindex, f"rm -f {remote}", timeout=15)
        return local
