"""Tests for the kernel-module uname spoof."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mumu_mcp.errors import BadArgument
from mumu_mcp.kmod import EXPECT_MACHINE, EXPECT_RELEASE, KO_LOCAL, MODULE_NAME, KernelUname

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


def _kmod(shell=None, is_root=True):
    adb = MagicMock()
    adb.shell = MagicMock(side_effect=shell or (lambda vm, cmd, **kw: ""))
    adb.push = MagicMock(return_value="")
    root = MagicMock()
    root.state.return_value = MagicMock(is_root=is_root)
    return KernelUname(adb=adb, root=root), adb


# ---------------------------------------------------------------------------
# the artifact exists and is the real thing
# ---------------------------------------------------------------------------
def test_module_binary_is_committed():
    """The MCP pushes this file, so a clone has to include it."""
    assert KO_LOCAL.is_file(), f"missing {KO_LOCAL}"
    assert KO_LOCAL.stat().st_size > 1000


def test_module_binary_is_an_elf_relocatable():
    head = KO_LOCAL.read_bytes()[:20]
    assert head[:4] == b"\x7fELF"
    assert head[4] == 2           # ELFCLASS64
    assert head[5] == 1           # little endian
    assert head[16] == 1          # ET_REL


def test_module_binary_carries_the_right_vermagic():
    """vermagic is compared byte for byte; a mismatch is a silent rejection."""
    blob = KO_LOCAL.read_bytes()
    assert b"vermagic=6.1.90-perf+ SMP preempt mod_unload modversions " in blob


def test_module_binary_exports_the_expected_entries():
    blob = KO_LOCAL.read_bytes()
    assert b"init_module" in blob
    assert b"cleanup_module" in blob
    assert b"name=khide_hook" in blob


def _code_only(path: Path) -> str:
    """Strip comments so assertions test behaviour, not prose about it."""
    import re

    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//.*", "", text)
    return text


def test_module_source_is_committed():
    src = KO_LOCAL.with_suffix(".c")
    assert src.is_file()
    code = _code_only(src)
    # The whole design hinges on writing this struct, not on patching code.
    assert "init_uts_ns" in code
    assert "sys_call_table" not in code


def test_module_does_not_touch_page_attributes():
    """set_memory_rw takes cpa_lock and flushed the TLB — it wedged the guest once."""
    code = _code_only(KO_LOCAL.with_suffix(".c"))
    assert "set_memory_rw" not in code
    assert "set_memory" not in code


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_reports_loaded_and_spoofed():
    def shell(vm, cmd, **kw):
        if "lsmod" in cmd:
            return "1\n"
        if cmd == "uname -r":
            return EXPECT_RELEASE + "\n"
        if cmd == "uname -m":
            return "aarch64\n"
        return ""

    k, _ = _kmod(shell)
    out = k.status(0)
    assert out["loaded"] is True
    assert out["spoofed"] is True


def test_status_reports_unspoofed_when_release_is_real():
    def shell(vm, cmd, **kw):
        if "lsmod" in cmd:
            return "0\n"
        if cmd == "uname -r":
            return "6.1.90-perf+\n"
        if cmd == "uname -m":
            return "aarch64\n"
        return ""

    k, _ = _kmod(shell)
    out = k.status(0)
    assert out["loaded"] is False
    assert out["spoofed"] is False


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_load_requires_root():
    k, _ = _kmod(is_root=False)
    with pytest.raises(BadArgument) as exc:
        k.load(0)
    assert "root" in exc.value.message


def test_load_refuses_when_binary_missing(tmp_path, monkeypatch):
    k, _ = _kmod()
    monkeypatch.setattr("mumu_mcp.kmod.KO_LOCAL", tmp_path / "nope.ko")
    with pytest.raises(BadArgument) as exc:
        k.load(0)
    assert "not built" in exc.value.message


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def test_load_falls_back_to_ksud_on_unknown_symbol():
    """An unexported symbol means insmod cannot resolve it; KernelSU's loader can."""
    state = {"loaded": False}

    def shell(vm, cmd, **kw):
        if cmd.startswith("insmod"):
            return "insmod: failed: Unknown symbol foo\n"
        if "ksud insmod" in cmd:
            state["loaded"] = True
            return "Loaded kernel module\n"
        if "lsmod" in cmd:
            return "1\n" if state["loaded"] else "0\n"
        if cmd == "uname -r":
            return (EXPECT_RELEASE if state["loaded"] else "6.1.90-perf+") + "\n"
        if cmd == "uname -m":
            return "aarch64\n"
        if cmd.startswith("ls /data"):
            return "/data/local/tmp/khide_hook.ko\n"
        return ""

    k, _ = _kmod(shell)
    out = k.load(0)
    assert out["via"] == "ksud insmod"
    assert out["verified"] is True


def test_load_reports_before_and_after():
    def shell(vm, cmd, **kw):
        if cmd == "uname -r":
            return EXPECT_RELEASE + "\n"
        if cmd == "uname -m":
            return "aarch64\n"
        if "lsmod" in cmd:
            return "1\n"
        if cmd.startswith("ls /data"):
            return "/data/x\n"
        return ""

    k, _ = _kmod(shell)
    out = k.load(0)
    assert out["after"]["release"] == EXPECT_RELEASE
    assert set(out["before"]) == {"release", "machine"}


# ---------------------------------------------------------------------------
# unload
# ---------------------------------------------------------------------------
def test_unload_reports_restoration():
    state = {"loaded": True}

    def shell(vm, cmd, **kw):
        if cmd.startswith("rmmod"):
            state["loaded"] = False
            return ""
        if "lsmod" in cmd:
            return "1\n" if state["loaded"] else "0\n"
        if cmd == "uname -r":
            return (EXPECT_RELEASE if state["loaded"] else "6.1.90-perf+") + "\n"
        if cmd == "uname -m":
            return "aarch64\n"
        return ""

    k, _ = _kmod(shell)
    out = k.unload(0)
    assert out["unloaded"] is True
    assert out["restored"] is True


def test_module_name_matches_the_binary():
    assert MODULE_NAME == "khide_hook"


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_kernel_module_changes_uname_systemwide():
    """The claim that matters: after loading, a plain `uname` reports the fake kernel."""
    from mumu_mcp.server import mumu_kmod_load, mumu_kmod_status, mumu_kmod_unload

    try:
        loaded = mumu_kmod_load.__wrapped__(0, rebuild=True)
        assert loaded["verified"] is True, loaded

        status = mumu_kmod_status.__wrapped__(0)
        assert status["spoofed"] is True
        assert status["release"] == EXPECT_RELEASE
        assert status["machine"] == EXPECT_MACHINE
    finally:
        mumu_kmod_unload.__wrapped__(0)

    after = mumu_kmod_status.__wrapped__(0)
    assert after["spoofed"] is False, "unload must restore the real identity"