"""Deep-hide tests: the bind-mount layer and the fake-file content."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from mumu_mcp import fakeprofile as fp
from mumu_mcp.errors import BadArgument
from mumu_mcp.stealth import Stealth

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


def _stealth(shell=None, is_root=True):
    adb = MagicMock()
    adb.shell = MagicMock(side_effect=shell or (lambda vm, cmd, **kw: ""))
    adb.run = MagicMock(return_value="")
    adb.invalidate = MagicMock()
    root = MagicMock()
    root.state.return_value = MagicMock(is_root=is_root, system_writable=True)
    return Stealth(adb=adb, root=root, manager=MagicMock()), adb


# ---------------------------------------------------------------------------
# fake profile content
# ---------------------------------------------------------------------------
def test_cpuinfo_is_arm_not_x86():
    text = fp.cpuinfo()
    assert "CPU implementer" in text
    assert "0x41" in text            # ARM
    assert "AuthenticAMD" not in text
    assert "vendor_id" not in text


def test_cpuinfo_core_count_matches_a_handset():
    """An e1q reports 8 cores; 6 would contradict ro.product.model."""
    assert fp.cpuinfo().count("processor\t:") == 8


def test_cpuinfo_has_no_hypervisor_flag():
    assert "hypervisor" not in fp.cpuinfo().lower()


def test_dmi_has_no_virtualbox_markers():
    blob = " ".join(fp.DMI.values()).lower()
    for marker in ("innotek", "vbox", "virtualbox", "qemu", "vmware"):
        assert marker not in blob


def test_dmi_matches_the_property_profile():
    """ro.product.model is SM-S9210, so the DMI must agree or it is a new tell."""
    assert fp.DMI["product_name"] == "SM-S9210"
    assert "samsung" in fp.DMI["sys_vendor"].lower()


def test_clocksource_is_not_kvm():
    assert "kvm" not in fp.CLOCKSOURCE_AVAILABLE.lower()
    assert "kvm" not in fp.CLOCKSOURCE_CURRENT.lower()
    assert "arch_sys_counter" in fp.CLOCKSOURCE_CURRENT


def test_osrelease_looks_like_a_samsung_build():
    assert "android" in fp.OSRELEASE
    assert "perf+" not in fp.OSRELEASE


def test_kallsyms_stub_has_no_x86_symbols():
    assert "x64_sys" not in fp.KALLSYMS
    assert "__arm64_sys_" in fp.KALLSYMS


def test_overrides_cover_the_files_that_shipped():
    assert fp.OVERRIDES["cpuinfo"] == "/proc/cpuinfo"
    assert fp.OVERRIDES["kallsyms"] == "/proc/kallsyms"
    assert fp.OVERRIDES["clocksource_current"].endswith("current_clocksource")


# ---------------------------------------------------------------------------
# bind mechanics
# ---------------------------------------------------------------------------
def test_bind_writes_base64_then_mounts():
    """Newlines must survive the shell, so the content goes through base64."""
    s, adb = _stealth(lambda vm, cmd, **kw: "processor\t: 0\n")
    result = s._bind(0, "/data/local/tmp/x", "/proc/cpuinfo", "processor\t: 0\n")
    cmds = [c[0][1] for c in adb.shell.call_args_list]
    assert any("base64 -d" in c for c in cmds)
    assert any("mount --bind" in c for c in cmds)
    assert result["target"] == "/proc/cpuinfo"


def test_bind_umounts_before_mounting_again():
    """Re-running deep_hide must not stack mounts."""
    s, adb = _stealth(lambda vm, cmd, **kw: "x")
    s._bind(0, "/data/local/tmp/x", "/proc/cpuinfo", "x")
    mount_cmd = [c[0][1] for c in adb.shell.call_args_list if "mount --bind" in c[0][1]][0]
    assert "umount" in mount_cmd


def test_deep_hide_requires_root():
    s, _ = _stealth(is_root=False)
    with pytest.raises(BadArgument) as exc:
        s.deep_hide(0)
    assert "root" in exc.value.message


def test_deep_hide_leaves_uname_alone_and_says_so():
    """uname(2) is a syscall; pretending to cover it would be a lie."""
    s, adb = _stealth(lambda vm, cmd, **kw: "aarch64\n")
    result = s.deep_hide(0, dmi=False)
    assert result["uname"]["covered"] is False
    assert "syscall" in result["uname"]["note"]


def test_deep_hide_skips_dmi_keys_that_do_not_exist():
    s, _ = _stealth(lambda vm, cmd, **kw: "No such file or directory\n")
    result = s.deep_hide(0, cpuinfo=False, clocksource=False, osrelease=False, kallsyms=False)
    assert result.get("dmi") == {}


def test_kallsyms_override_only_when_x86_symbols_present():
    """No point replacing the file when it already reads clean."""
    s, adb = _stealth(lambda vm, cmd, **kw: "0\n")
    result = s.deep_hide(0, cpuinfo=False, dmi=False, clocksource=False, osrelease=False)
    assert "kallsyms" not in result


def test_deep_restore_unmounts_and_cleans():
    s, adb = _stealth(lambda vm, cmd, **kw: "")
    out = s.deep_restore(0)
    assert out["staged_removed"] == fp.STAGE_DIR
    assert any("rm -rf" in c[0][1] for c in adb.shell.call_args_list)


def test_verify_fakes_reports_each_file():
    s, _ = _stealth(lambda vm, cmd, **kw: "CPU implementer : 0x41")
    out = s._verify_fakes(0)
    assert set(out) == {"cpuinfo", "osrelease", "clocksource", "dmi_sys_vendor", "kallsyms"}
    assert out["cpuinfo"]["covered"] is True


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_deep_hide_then_restore():
    """The bind-mount layer must fake on demand and undo its own mounts.

    What it must *not* be expected to undo any more is the kernel module's work.
    `khide_hook.ko` rewrites `cpuinfo_x86` and `dmi_ident` in kernel data, and it hides
    itself from the module list — so after `deep_restore()` the cpuinfo still reads as a
    handset, because a different mechanism is responsible for it. Only a reboot undoes
    that, and this test does not pretend otherwise.
    """
    from mumu_mcp import server
    from mumu_mcp.server import (
        mumu_root_adb,
        mumu_stealth_deep,
        mumu_stealth_deep_restore,
    )

    mumu_root_adb.__wrapped__(0, True)
    applied = mumu_stealth_deep.__wrapped__(0)
    verified = applied["verified"]
    assert verified["cpuinfo"]["covered"], "cpuinfo must read as a handset profile"
    assert verified["dmi_sys_vendor"]["covered"], "DMI must not say innotek"

    # Read it back the way a detector would: as the shell user, no root env.
    seen = server.adb().shell(0, "head -8 /proc/cpuinfo")
    assert "AuthenticAMD" not in seen

    mumu_stealth_deep_restore.__wrapped__(0)
    after = server.adb().shell(0, "head -8 /proc/cpuinfo")
    # No bind mount may survive the restore.
    mounts = server.adb().shell(
        0, "grep -cE 'proc/cpuinfo|clocksource0|dmi/id' /proc/mounts 2>/dev/null").strip()
    assert mounts == "0", f"{mounts} bind mount(s) survived deep_restore"
    # The kernel module still owns the values, so the host CPU is not expected back.
    assert "Ryzen" not in after, "the kernel module's rewrite should still be in effect"
