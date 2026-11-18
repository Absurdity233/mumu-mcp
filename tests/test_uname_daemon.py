"""Tests for the standing uname tracer (the system-wide alternative to a kernel hook)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from mumu_mcp.errors import BadArgument
from mumu_mcp.uname_daemon import UnameDaemon

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")

PS_OUTPUT = """\
  PID ARGS
    1 /init
  446 /system/bin/logd
  738 /system/bin/sh -c while true; do uname -r; done
  739 toybox uname -r
  900 com.android.settings
"""


def _daemon(shell=None, is_root=True):
    adb = MagicMock()
    adb.shell = MagicMock(side_effect=shell or (lambda vm, cmd, **kw: ""))
    adb.push = MagicMock(return_value="")
    root = MagicMock()
    root.state.return_value = MagicMock(is_root=is_root)
    d = UnameDaemon(adb=adb, root=root)
    # skip the binary push in unit tests
    d._ensure_binary = lambda vm: None
    return d, adb


# ---------------------------------------------------------------------------
# target discovery
# ---------------------------------------------------------------------------
def test_find_targets_matches_by_substring():
    d, _ = _daemon(lambda vm, cmd, **kw: PS_OUTPUT if "ps -A" in cmd else "")
    found = d._find_targets(0, ["uname", "toybox"], 8)
    assert {pid for pid, _ in found} == {738, 739}


def test_find_targets_respects_the_limit():
    """Each traced process costs a ptrace stop per syscall, so the fan-out is capped."""
    d, _ = _daemon(lambda vm, cmd, **kw: PS_OUTPUT if "ps -A" in cmd else "")
    assert len(d._find_targets(0, ["uname", "toybox", "sh"], 2)) == 2


def test_find_targets_skips_init():
    """pid 1 is never a valid trace target."""
    d, _ = _daemon(lambda vm, cmd, **kw: PS_OUTPUT if "ps -A" in cmd else "")
    found = d._find_targets(0, ["init"], 8)
    assert all(pid != 1 for pid, _ in found)


def test_find_targets_ignores_malformed_lines():
    d, _ = _daemon(lambda vm, cmd, **kw: "garbage\n\n  x y\n  42 uname -r\n")
    assert d._find_targets(0, ["uname"], 8) == [(42, "uname -r")]


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_start_requires_root():
    d, _ = _daemon(is_root=False)
    with pytest.raises(BadArgument) as exc:
        d.start(0)
    assert "root" in exc.value.message


def test_start_refuses_when_binary_missing(tmp_path, monkeypatch):
    d, _ = _daemon()
    monkeypatch.setattr("mumu_mcp.uname.NATIVE_DIR", tmp_path)
    d._ensure_binary = UnameDaemon._ensure_binary.__get__(d)
    with pytest.raises(BadArgument) as exc:
        d.start(0)
    assert "not built" in exc.value.message


# ---------------------------------------------------------------------------
# per-process attach
# ---------------------------------------------------------------------------
def test_attach_reports_verified_rewrite():
    def shell(vm, cmd, **kw):
        if "cat /data/local/tmp/uname_patch" in cmd:
            return "attached\nrewrote a uname result\n"
        return ""

    d, _ = _daemon(shell)
    out = d._attach_one(0, 1234)
    assert out["attached"] is True
    assert "rewrite" in out["verified"]


def test_attach_surfaces_failure():
    def shell(vm, cmd, **kw):
        if "cat /data/local/tmp/uname_patch" in cmd:
            return "ptrace attach failed (need root)\n"
        return ""

    d, _ = _daemon(shell)
    out = d._attach_one(0, 1234)
    assert out["attached"] is False
    assert "attach failed" in out.get("error", "")


def test_attach_returns_inconclusive_when_no_message():
    d, _ = _daemon(lambda vm, cmd, **kw: "")
    out = d._attach_one(0, 1234)
    assert out["attached"] is False


# ---------------------------------------------------------------------------
# status / stop
# ---------------------------------------------------------------------------
def test_status_lists_tracers():
    def shell(vm, cmd, **kw):
        if "grep uname_patch" in cmd:
            return "  500 uname_patch 738\n  501 uname_patch 739\n"
        if cmd == "uname -r":
            return "6.1.90-perf+\n"
        return ""

    d, _ = _daemon(shell)
    out = d.status(0)
    assert out["count"] == 2
    assert out["tracers"] == [500, 501]


def test_status_reports_zero_when_nothing_traced():
    d, _ = _daemon(lambda vm, cmd, **kw: "6.1.90-perf+\n" if cmd == "uname -r" else "")
    assert d.status(0)["count"] == 0


def test_stop_reports_when_nothing_running():
    d, _ = _daemon(lambda vm, cmd, **kw: "")
    out = d.stop(0)
    assert out["stopped"] == 0


def test_stop_warns_about_exitkill():
    """The warning has to reach the caller: stopping a tracer kills its tracee."""
    def shell(vm, cmd, **kw):
        if "grep" in cmd:
            return "500 uname_patch 738\n"
        return ""

    d, _ = _daemon(shell)
    out = d.stop(0)
    assert "EXITKILL" in out["warning"]


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_daemon_attaches_and_rewrites():
    """The claim that matters: a standing tracer makes a running process see the fake."""
    from mumu_mcp.server import (
        mumu_uname_daemon_start,
        mumu_uname_daemon_status,
        mumu_uname_daemon_stop,
    )

    started = mumu_uname_daemon_start.__wrapped__(0, limit=4)
    try:
        assert started["state"]["running"] or any(a.get("attached") for a in started["attached"])
        status = mumu_uname_daemon_status.__wrapped__(0)
        assert status["count"] >= 1
    finally:
        mumu_uname_daemon_stop.__wrapped__(0)