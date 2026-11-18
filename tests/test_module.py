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


def test_service_covers_every_file_the_deep_layer_covers():
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    for target in ("/proc/cpuinfo", "/proc/kallsyms", "/proc/sys/kernel/osrelease",
                   "/sys/devices/system/clocksource/clocksource0/current_clocksource",
                   "/sys/devices/system/clocksource/clocksource0/available_clocksource"):
        assert target in text, f"service.sh does not cover {target}"


def test_service_covers_dmi_fields():
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    assert "/sys/class/dmi/id/" in text
    assert "sys_vendor" in text


def test_service_deletes_vendor_properties():
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    assert "resetprop" in text and " -d " in text
    assert "vbox" in text, "ro.build.hv.platform=vbox must be covered"


def test_service_enables_selinux_hide():
    text = (MODULE / "service.sh").read_text(encoding="utf-8")
    assert "selinux_hide" in text


def test_data_files_are_present_and_non_empty():
    for name in ("cpuinfo", "kallsyms", "osrelease", "dmi.prop"):
        path = MODULE / name
        assert path.is_file() and path.stat().st_size > 0, name


def test_cpuinfo_data_is_arm():
    text = (MODULE / "cpuinfo").read_text(encoding="utf-8")
    assert "CPU implementer" in text and "AuthenticAMD" not in text


def test_dmi_prop_has_no_virtualbox():
    text = (MODULE / "dmi.prop").read_text(encoding="utf-8").lower()
    for marker in ("innotek", "vbox", "virtualbox"):
        assert marker not in text


def test_kallsyms_data_has_no_x86_symbols():
    text = (MODULE / "kallsyms").read_text(encoding="utf-8")
    assert "x64_sys" not in text


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

    if "CPU implementer" not in adb.shell(0, "grep -m1 'CPU implementer' /proc/cpuinfo"):
        # mounts were reset; reapply so the rest of the assertions are meaningful
        adb.shell(0, "rm -f /data/adb/modules/mumu-stealth/.last_boot; "
                     "timeout 40 sh /data/adb/modules/mumu-stealth/service.sh 2>&1")

    assert "CPU implementer" in adb.shell(0, "grep -m1 'CPU implementer' /proc/cpuinfo")
    assert "innotek" not in adb.shell(0, "cat /sys/class/dmi/id/sys_vendor")
    assert adb.shell(0, "grep -c x64_sys /proc/kallsyms").strip() == "0"
