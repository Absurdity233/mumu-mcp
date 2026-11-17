"""Locating a MuMu install and talking to MuMuManager.

Three things shape this module:

* `MuMuManager.exe` and `mumu-cli.exe` are byte-identical in MuMu 6.x; both are
  accepted so the server works on 12.x and 6.x without a flag.
* MuMu writes UTF-8 JSON. Decoding with the host codepage (the default of
  `text=True` on a zh-CN Windows) mangles every `app_name`. We decode bytes
  ourselves, UTF-8 first.
* Every call returns parsed JSON or raises a typed `MuMuError`. Callers never
  see `returncode`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    BadArgument,
    InstallNotFound,
    ManagerFailed,
    Timeout,
    from_errcode,
)

log = logging.getLogger("mumu_mcp.manager")

CLI_NAMES = ("mumu-cli.exe", "MuMuManager.exe", "mumu-cli", "MuMuManager")

# Ordered: most specific first. %s is expanded with common program roots.
_SEARCH_TEMPLATES = (
    r"{root}\Netease\MuMu\nx_main",              # MuMu 6.x / "MuMu模拟器" (NX)
    r"{root}\Netease\MuMuPlayer-12.0\nx_main",   # MuMu 12
    r"{root}\Netease\MuMuPlayer\nx_main",
    r"{root}\Netease\MuMuPlayer-15.0\nx_main",
    r"{root}\MuMu\nx_main",
    r"{root}\MuMuPlayer\nx_main",
)


def _candidate_dirs() -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMW6432", r"C:\Program Files"),
    ]
    # Any drive letter that has a Netease/MuMu tree; D: is the common custom target.
    for drive in "CDEFGH":
        roots.append(f"{drive}:\\Program Files")
        roots.append(f"{drive}:\\")
    seen: list[Path] = []
    for root in roots:
        if not root:
            continue
        for tpl in _SEARCH_TEMPLATES:
            p = Path(tpl.format(root=root.rstrip("\\") + "\\"))
            if p not in seen:
                seen.append(p)
    return seen


def find_cli(explicit: str | None = None) -> Path:
    """Return the MuMuManager binary, or raise InstallNotFound.

    `explicit` may point at the binary itself or at the nx_main directory.
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        for name in CLI_NAMES:
            cand = p / name
            if cand.is_file():
                return cand
        raise InstallNotFound(
            f"no MuMuManager under {p}",
            detail={"looked_for": list(CLI_NAMES)},
        )

    env = os.environ.get("MUMU_HOME")
    if env:
        p = Path(env)
        for name in CLI_NAMES:
            cand = p / name if p.is_dir() else p
            if cand.is_file():
                return cand

    for d in _candidate_dirs():
        for name in CLI_NAMES:
            cand = d / name
            if cand.is_file():
                return cand

    raise InstallNotFound(
        "MuMuManager not found; pass --mumu-home or set MUMU_HOME",
        detail={"searched": [str(d) for d in _candidate_dirs()[:8]]},
    )


def decode(raw: bytes) -> str:
    """UTF-8 first, then GB18030 for guest text, then lossy UTF-8."""
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


@dataclass
class Run:
    """A finished MuMuManager invocation."""

    argv: list[str]
    stdout: str
    stderr: str
    returncode: int
    elapsed_ms: int = 0

    def json(self) -> Any:
        """Parse stdout as JSON; raise the typed error the payload names."""
        text = self.stdout.strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "errcode" in payload and payload["errcode"] not in (0, None):
                raise from_errcode(int(payload["errcode"]), payload.get("errmsg", ""), payload)
            if payload is not None:
                return payload
        if self.returncode != 0 and not text:
            # Manager writes some failures to stderr only.
            raise ManagerFailed(
                self.stderr.strip() or f"MuMuManager exited {self.returncode}",
                detail={"argv": self.argv},
            )
        return text

    def json_or_empty(self) -> Any:
        try:
            return self.json()
        except (ManagerFailed, BadArgument):
            return {}

    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class Manager:
    """Thin, typed wrapper over MuMuManager.exe."""

    cli: Path
    default_timeout: float = 30.0
    _version: str | None = field(default=None, init=False, repr=False)

    @classmethod
    def autodetect(cls, explicit: str | None = None, **kw: Any) -> Manager:
        cli = find_cli(explicit)
        log.info("using %s", cli)
        return cls(cli=cli, **kw)

    @property
    def home(self) -> Path:
        return self.cli.parent

    # -- raw ---------------------------------------------------------------
    def run(self, *args: Any, timeout: float | None = None) -> Run:
        argv = [str(self.cli), *[str(a) for a in args if a is not None]]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout or self.default_timeout,
                creationflags=_no_window(),
            )
        except subprocess.TimeoutExpired as exc:
            raise Timeout(f"MuMuManager did not answer in {timeout or self.default_timeout}s",
                          detail={"argv": argv}) from exc
        except FileNotFoundError as exc:
            raise InstallNotFound(str(exc)) from exc
        return Run(
            argv=argv,
            stdout=decode(proc.stdout),
            stderr=decode(proc.stderr),
            returncode=proc.returncode,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def call(self, *args: Any, timeout: float | None = None) -> Any:
        return self.run(*args, timeout=timeout).json()

    # -- convenience -------------------------------------------------------
    def version(self) -> str:
        if self._version is None:
            try:
                payload = self.call("version", timeout=15)
                self._version = str(payload.get("version", "")) if isinstance(payload, dict) else ""
            except (Timeout, ManagerFailed):
                self._version = ""
        return self._version or "unknown"

    def instances(self) -> dict[str, dict[str, Any]]:
        payload = self.call("info", "--vmindex", "all", timeout=20)
        if not isinstance(payload, dict):
            return {}
        if "errcode" in payload:
            # No instances at all is reported as an errcode payload.
            return {}
        return {k: v for k, v in payload.items() if isinstance(v, dict)}

    def instance(self, vmindex: int) -> dict[str, Any]:
        payload = self.call("info", "--vmindex", str(vmindex), timeout=20)
        if not isinstance(payload, dict) or "player_state" not in payload:
            raise _instance_error(vmindex, payload)
        return payload

    def setting_keys(self, *, writable_only: bool, vmindex: int | None = None) -> dict[str, str]:
        args: list[Any] = ["setting"]
        if vmindex is not None:
            args += ["--vmindex", str(vmindex)]
        args.append("--all_writable" if writable_only else "--all")
        payload = self.call(*args, timeout=20)
        return payload if isinstance(payload, dict) else {}

    def setting_set(self, vmindex: int | None, pairs: dict[str, str]) -> Any:
        if not pairs:
            raise BadArgument("no key/value pairs given")
        args: list[Any] = ["setting"]
        if vmindex is not None:
            args += ["--vmindex", str(vmindex)]
        for k, v in pairs.items():
            args += ["--key", k, "--value", v]
        return self.call(*args, timeout=30)

    def sh(self, vmindex: int, command: str, *, timeout: float = 30.0) -> str:
        payload = self.call("sh", "--vmindex", str(vmindex), "--cmd", command, timeout=timeout)
        if isinstance(payload, dict):
            # sh mirrors unrecognised output as an errcode envelope.
            raise from_errcode(int(payload.get("errcode", -1)), payload.get("errmsg", ""), payload)
        return str(payload)


def _instance_error(vmindex: int, payload: Any):
    if isinstance(payload, dict) and "errcode" in payload:
        return from_errcode(int(payload["errcode"]), payload.get("errmsg", ""), payload)
    return ManagerFailed(f"unexpected info payload for index {vmindex}", detail=payload)


def _no_window() -> int:
    """CREATE_NO_WINDOW: MuMuManager is a console binary; don't flash a console."""
    return 0x08000000 if os.name == "nt" else 0


def as_list(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)
