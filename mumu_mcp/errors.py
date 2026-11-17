"""Typed errors.

MuMuManager reports trouble three different ways: a JSON `errcode`, a non-zero
exit with text on stderr, and a successful exit whose payload is empty because
the instance was not ready. Collapsing those into an `Exception` the caller has
to string-match means a model cannot tell "retry in two seconds" from "this
argument is wrong". Every failure here carries a stable code, and `retryable`
answers that question directly.
"""

from __future__ import annotations

from typing import Any


class MuMuError(Exception):
    """Base error. Maps to a JSON envelope, never to a bare traceback."""

    code = "mumu_error"
    retryable = False

    def __init__(self, message: str, *, detail: Any | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class InstallNotFound(MuMuError):
    """MuMuManager.exe could not be located on this machine."""

    code = "install_not_found"


class InstanceNotFound(MuMuError):
    """The requested vmindex does not exist."""

    code = "instance_not_found"


class InstanceNotReady(MuMuError):
    """The instance exists but Android has not finished booting."""

    code = "instance_not_ready"
    retryable = True


class KeyNotWritable(MuMuError):
    """The setting exists but is read-only on this build."""

    code = "key_not_writable"


class AdbUnavailable(MuMuError):
    """A channel that needs adb could not reach the guest."""

    code = "adb_unavailable"
    retryable = True


class ManagerFailed(MuMuError):
    """MuMuManager returned a non-zero errcode."""

    code = "manager_failed"

    def __init__(self, message: str, *, errcode: int | None = None, detail: Any = None):
        super().__init__(message, detail=detail)
        self.errcode = errcode

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        if self.errcode is not None:
            out["errcode"] = self.errcode
        return out


class Timeout(MuMuError):
    code = "timeout"
    retryable = True


class BadArgument(MuMuError):
    code = "bad_argument"


# MuMuManager errcode -> exception class. Unlisted codes fall back to ManagerFailed.
_ERRCODE_MAP: dict[int, type[MuMuError]] = {
    -200: InstanceNotFound,
    -202: InstanceNotReady,
    -101: KeyNotWritable,
    -102: KeyNotWritable,
}


def from_errcode(errcode: int, errmsg: str, payload: Any = None) -> MuMuError:
    cls = _ERRCODE_MAP.get(errcode, ManagerFailed)
    if cls is ManagerFailed:
        return ManagerFailed(errmsg or f"MuMuManager errcode {errcode}", errcode=errcode, detail=payload)
    return cls(errmsg or f"MuMuManager errcode {errcode}", detail=payload)
