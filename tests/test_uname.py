"""Tests for the syscall-level uname patch layer."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from mumu_mcp.errors import BadArgument
from mumu_mcp.uname import (
    EXPECT_MACHINE,
    EXPECT_RELEASE,
    NATIVE_DIR,
    UnamePatcher,
)

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


def _patcher(shell=None, is_root=True):
    adb = MagicMock()
    adb.shell = MagicMock(side_effect=shell or (lambda vm, cmd, **kw: ""))
    adb.push = MagicMock(return_value="")
    root = MagicMock()
    root.state.return_value = MagicMock(is_root=is_root)
    return UnamePatcher(adb=adb, root=root), adb


# ---------------------------------------------------------------------------
# expected values must agree with the rest of the surface
# ---------------------------------------------------------------------------
def test_expected_release_matches_fakeprofile():
    from mumu_mcp import fakeprofile as fp
    assert fp.OSRELEASE.startswith("6.1.75-android14")


def test_expected_machine_is_aarch64():
    assert EXPECT_MACHINE == "aarch64"


def test_release_has_no_perf_suffix():
    """`-perf+` is a MuMu/VM kernel marker; the fake must not carry it."""
    assert "perf+" not in EXPECT_RELEASE


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_reports_unpatched():
    def shell(vm, cmd, **kw):
        if cmd == "uname -r":
            return "6.1.90-perf+\n"
        if cmd == "uname -m":
            return "aarch64\n"
        return ""

    p, _ = _patcher(shell)
    out = p.status(0)
    assert out["patched"] is False
    assert out["release"] == "6.1.90-perf+"


def test_status_reports_patched():
    def shell(vm, cmd, **kw):
        if cmd == "uname -r":
            return EXPECT_RELEASE + "\n"
        if cmd == "uname -m":
            return "aarch64\n"
        return ""

    p, _ = _patcher(shell)
    assert p.status(0)["patched"] is True


def test_status_finds_a_running_tracer():
    def shell(vm, cmd, **kw):
        if "grep uname_patch" in cmd:
            return "4242 uname_patch 999\n"
        return "x\n"

    p, _ = _patcher(shell)
    assert p.status(0)["tracer_pid"] == 4242


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_attach_requires_root():
    p, _ = _patcher(is_root=False)
    with pytest.raises(BadArgument) as exc:
        p.attach(0, 1)
    assert "root" in exc.value.message


def test_attach_rejects_bad_pid():
    p, _ = _patcher()
    with pytest.raises(BadArgument):
        p.attach(0, 0)


def test_attach_rejects_missing_process():
    def shell(vm, cmd, **kw):
        if "test -d /proc/" in cmd:
            return "no\n"
        return ""

    p, _ = _patcher(shell)
    with pytest.raises(BadArgument) as exc:
        p.attach(0, 99999)
    assert "does not exist" in exc.value.message


def test_ensure_binaries_refuses_when_not_built(tmp_path, monkeypatch):
    """A missing .S build must be a clear message, not a push of nothing."""
    monkeypatch.setattr("mumu_mcp.uname.NATIVE_DIR", tmp_path)
    p, _ = _patcher()
    with pytest.raises(BadArgument) as exc:
        p.ensure_binaries(0)
    assert "not built" in exc.value.message


# ---------------------------------------------------------------------------
# attach result interpretation
# ---------------------------------------------------------------------------
def test_attach_reports_success_on_rewrite():
    def shell(vm, cmd, **kw):
        if "test -d /proc/" in cmd:
            return "yes\n"
        if "cat /data/local/tmp/uname_patch" in cmd:
            return "attached\nrewrote a uname result\n"
        if cmd.startswith("ls -l"):
            return "-rwxr-xr-x 1 root root 1 x\n"
        return ""

    p, _ = _patcher(shell)
    out = p.attach(0, 1234, settle_s=1.5)
    assert out["attached"] is True
    assert "rewrite" in out["verified"]


def test_attach_surfaces_ptrace_failure():
    from mumu_mcp.errors import AdbUnavailable

    def shell(vm, cmd, **kw):
        if "test -d /proc/" in cmd:
            return "yes\n"
        if "cat /data/local/tmp/uname_patch" in cmd:
            return "ptrace attach failed (need root)\n"
        if cmd.startswith("ls -l"):
            return "-rwxr-xr-x\n"
        return ""

    p, _ = _patcher(shell)
    with pytest.raises(AdbUnavailable) as exc:
        p.attach(0, 1234, settle_s=1.0)
    assert "ptrace attach failed" in str(exc.value.detail.get("log", ""))


def test_detach_reports_when_nothing_running():
    p, _ = _patcher(lambda vm, cmd, **kw: "")
    assert p.detach(0)["detached"] is False


# ---------------------------------------------------------------------------
# struct verification
# ---------------------------------------------------------------------------
def test_verify_struct_detects_fake_values():
    def shell(vm, cmd, **kw):
        if "/proc/" in cmd and "mem" in cmd:
            return f"Linuxlocalhost{EXPECT_RELEASE}#1 SM...aarch64"
        return ""

    p, _ = _patcher(shell)
    out = p.verify_struct(0, 1234)
    assert out["patched"] is True
    assert out["has_fake_release"] is True


def test_verify_struct_detects_real_values():
    def shell(vm, cmd, **kw):
        if "/proc/" in cmd and "mem" in cmd:
            return "Linuxlocalhost6.1.90-perf+#3 SMP..."
        return ""

    p, _ = _patcher(shell)
    out = p.verify_struct(0, 1234)
    assert out["patched"] is False


def test_verify_struct_requires_pid():
    from mumu_mcp.server import mumu_uname_verify

    with pytest.raises(BadArgument):
        mumu_uname_verify.__wrapped__(0, 0)


# ---------------------------------------------------------------------------
# native sources exist and encode the right constants
# ---------------------------------------------------------------------------
def test_native_sources_present():
    for name in ("uname_patch.S", "uname_target.S", "uname_hook.h"):
        assert (NATIVE_DIR / name).is_file(), name


def test_built_binaries_are_committed():
    """The MCP pushes these to the device, so a clone must include them."""
    for name in ("uname_patch", "uname_target"):
        path = NATIVE_DIR / name
        assert path.is_file(), name
        assert path.stat().st_size > 0


def test_tracer_uses_correct_ptrace_argument_order():
    """The first version put addr in r10 and data in rdx, so every poke failed."""
    src = (NATIVE_DIR / "uname_patch.S").read_text(encoding="utf-8")
    # the POKEDATA block must load data into r10 and the address into rdx
    assert "movq    (%r14,%r15,8), %r10" in src
    assert "leaq    (%r13,%r15,8), %rdx" in src


def test_tracer_checks_poke_result():
    src = (NATIVE_DIR / "uname_patch.S").read_text(encoding="utf-8")
    assert "js      .trace" in src, "a failed poke must not look like success"


def test_fake_uts_fields_are_65_bytes_each():
    """A wrong pad length shifts every later field; that produced an empty machine."""
    src = (NATIVE_DIR / "uname_patch.S").read_text(encoding="utf-8")
    block = src.split("fake_uts:")[1]
    strings = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith('.asciz "'):
            strings.append(line[8:-1])
        elif line.startswith(".space "):
            strings.append(int(line.split()[1]))
    # pairs of (string, pad); each string + NUL + pad == 65
    for i in range(0, len(strings) - 1, 2):
        text, pad = strings[i], strings[i + 1]
        assert len(text) + 1 + pad == 65, f"{text!r}: {len(text)}+1+{pad} != 65"


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_status_shape():
    from mumu_mcp.server import mumu_uname_status

    out = mumu_uname_status.__wrapped__(0)
    assert out["release"]
    assert out["machine"]
    assert out["expected"]["release"] == EXPECT_RELEASE


@LIVE
def test_live_selftest_rewrites_uname():
    """The claim that matters: a self-calling target sees the fake kernel."""
    from mumu_mcp.server import mumu_uname_selftest

    out = mumu_uname_selftest.__wrapped__(0)
    assert out["passed"] is True, out
    assert EXPECT_RELEASE in out["after"]
    assert EXPECT_MACHINE in out["after"]
