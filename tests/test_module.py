"""Tests for the KernelSU module package and the feature-switch layer."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mumu_mcp.errors import BadArgument
from mumu_mcp.stealth import Stealth

MODULE = Path(__file__).resolve().parent.parent / "module"
LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


# ---------------------------------------------------------------------------
# module package shape
# ---------------------------------------------------------------------------
def test_module_prop_has_required_keys():
    text = (MODULE / "module.prop").read_text(encoding="utf-8")
    for key in ("id=", "name=", "version=", "versionCode=", "author=", "description="):
        assert key in text, f"module.prop missing {key}"


def test_module_id_matches_the_install_path():
    text = (MODULE / "module.prop").read_text(encoding="utf-8")
    ident = next(ln for ln in text.splitlines() if ln.startswith("id=")).split("=", 1)[1]
    assert ident == "mumu-stealth"


def test_service_script_exists_and_is_a_shell_script():
    script = MODULE / "service.sh"
    assert script.is_file()
    assert script.read_text(encoding="utf-8").startswith("#!/system/bin/sh")


def test_post_fs_data_script_exists():
    assert (MODULE / "post-fs-data.sh").is_file()


def test_scripts_are_idempotent_per_boot():
    """KernelSU may run a script once per stage; without a guard the log doubles."""
    for name in ("service.sh", "post-fs-data.sh"):
        text = (MODULE / name).read_text(encoding="utf-8")
        assert "boot_id" in text, f"{name} has no per-boot guard"
        assert "exit 0" in text


def test_service_loads_the_kernel_module():
    """The hiding lives in khide_hook.ko now, not in mounts."""
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    assert "khide_hook.ko" in text
    assert "insmod" in text


def test_module_ships_the_kernel_module():
    ko = MODULE / "khide_hook.ko"
    assert ko.is_file() and ko.stat().st_size > 1000
    assert ko.read_bytes()[:4] == b"\x7fELF"


def test_service_no_longer_bind_mounts():
    """A bind mount shows up in /proc/self/mounts, which any process can read.

    On real hardware /proc/cpuinfo comes from procfs and /sys/class/dmi/id/* from sysfs,
    so an ext4 partition as the mount source is a bigger tell than the value it hides.
    The kernel module replaced all of it; a mount left dormant here would reintroduce the
    tell on some future boot.
    """
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    # Ignore comments — the rationale above is spelled out there.
    code = "\n".join(
        ln for ln in text.splitlines() if not ln.strip().startswith("#")
    )
    assert "mount --bind" not in code
    assert "/proc/cpuinfo" not in code
    assert "/sys/class/dmi/id/" not in code
    assert "clocksource0" not in code


def test_staged_data_files_are_gone():
    """The files those mounts needed must not linger."""
    for name in ("cpuinfo", "kallsyms", "osrelease", "dmi.prop"):
        assert not (MODULE / name).exists(), (
            f"{name} is left over from the bind-mount layer; the kernel module "
            "sets these values directly"
        )


def test_service_deletes_vendor_properties():
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    assert "resetprop" in text and " -d " in text
    assert "vbox" in text, "ro.build.hv.platform=vbox must be covered"


def test_service_enables_selinux_hide():
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    assert "selinux_hide" in text


def test_scripts_have_no_crlf():
    """Android's sh cannot parse `\\r`; a CRLF script fails at boot with a syntax error.

    This is not hypothetical: service.sh was written with Windows line endings and the
    module silently did nothing on reboot, while `sh -n` on the device reported
    "syntax error: unexpected 'do'".
    """
    for name in ("service.sh", "post-fs-data.sh"):
        data = (MODULE / name).read_bytes()
        assert b"\r\n" not in data, f"{name} has CRLF line endings"


@pytest.mark.skipif(shutil.which("sh") is None and os.name == "nt", reason="no POSIX shell")
def test_scripts_parse():
    """`sh -n` catches the quoting mistakes that would only show up at boot."""
    for name in ("service.sh", "post-fs-data.sh"):
        result = subprocess.run(["sh", "-n", str(MODULE / name)],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


# ---------------------------------------------------------------------------
# kernel feature switches
# ---------------------------------------------------------------------------
FEATURE_LIST = """\
Available Features:
================================================================================
[ENABLED (1)] su_compat (ID=0)
    SU Compatibility Mode

[DISABLED] selinux_hide (ID=4)
    SELinux Hide - sanitize /sys/fs/selinux access results for app UIDs
"""


def _stealth(shell=None):
    from unittest.mock import MagicMock

    adb = MagicMock()
    adb.shell = MagicMock(side_effect=shell or (lambda vm, cmd, **kw: FEATURE_LIST))
    root = MagicMock()
    root.state.return_value = MagicMock(is_root=True)
    return Stealth(adb=adb, root=root, manager=MagicMock()), adb


def test_features_parses_the_list():
    s, _ = _stealth()
    out = s.features(0)
    assert out["available"] is True
    names = {f["name"]: f["enabled"] for f in out["features"]}
    assert names == {"su_compat": True, "selinux_hide": False}


def test_features_captures_description():
    s, _ = _stealth()
    entry = next(f for f in s.features(0)["features"] if f["name"] == "selinux_hide")
    assert "sanitize" in entry["description"]


def test_set_feature_rejects_unknown_name():
    s, _ = _stealth()
    with pytest.raises(BadArgument) as exc:
        s.set_feature(0, "no_such_feature", True)
    assert "unknown" in exc.value.message


def test_set_feature_reports_the_readback_state():
    calls = {"n": 0}

    def shell(vm, cmd, **kw):
        if "feature set" in cmd:
            return ""
        # after the set, report selinux_hide as enabled
        calls["n"] += 1
        return FEATURE_LIST.replace("[DISABLED] selinux_hide", "[ENABLED (1)] selinux_hide")

    s, _ = _stealth(shell)
    out = s.set_feature(0, "selinux_hide", True)
    assert out["requested"] is True
    assert out["applied"] is True


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_features_include_selinux_hide():
    from mumu_mcp.server import mumu_stealth_features

    out = mumu_stealth_features.__wrapped__(0)
    names = {f["name"] for f in out["features"]}
    assert "selinux_hide" in names


@LIVE
def test_live_module_is_installed_and_applied():
    """The module must be installed and its effects present.

    The overrides live in a mount namespace that `adb root` can reset, so this
    asserts on the *installed module and its log* — the durable facts — and then
    re-runs the script if the mounts are missing rather than assuming they are
    still up from boot.
    """
    from mumu_mcp import server

    adb = server.adb()
    listing = adb.shell(0, "timeout 10 /data/adb/ksu/bin/ksud module list 2>&1")
    assert "mumu-stealth" in listing
    assert '"enabled": "true"' in listing

    # The module loads khide_hook.ko, which hides itself — `lsmod` deliberately does not
    # list it, so the observable proof is the values it changed.
    release = adb.shell(0, "uname -r").strip()
    if not release.startswith("6.1.75-"):
        adb.shell(0, "rm -f /data/adb/modules/mumu-stealth/.last_boot; "
                     "timeout 40 sh /data/adb/modules/mumu-stealth/service.sh 2>&1")

    release = adb.shell(0, "uname -r").strip()
    assert release.startswith("6.1.75-"), release

    # No bind mount may be involved: an override shows up in /proc/self/mounts, which any
    # process can read, and a real device serves these from procfs/sysfs.
    overrides = adb.shell(
        0, "grep -cE 'proc/cpuinfo|proc/kallsyms|clocksource|dmi/id' /proc/mounts").strip()
    assert overrides == "0", f"{overrides} bind-mount override(s) still present"

    assert "innotek" not in adb.shell(0, "cat /sys/class/dmi/id/sys_vendor")
    assert "hypervisor" not in adb.shell(0, "grep -m1 '^flags' /proc/cpuinfo")
