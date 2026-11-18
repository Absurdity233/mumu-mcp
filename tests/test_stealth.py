"""Environment-surface tests. The parsers and the grading are pure logic."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from mumu_mcp.errors import BadArgument
from mumu_mcp.stealth import (
    EMULATOR_MARKERS,
    NEMU_SERVICES,
    PROP_PROFILE,
    Finding,
    Scan,
    Stealth,
    _score,
)

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


def _stealth(shell_output: dict[str, str] | None = None, is_root: bool = True) -> tuple[Stealth, MagicMock]:
    adb = MagicMock()
    adb.shell = MagicMock(side_effect=lambda vm, cmd, **kw: (shell_output or {}).get(cmd, ""))
    adb.run = MagicMock(return_value="")
    adb.invalidate = MagicMock()
    root = MagicMock()
    root.state.return_value = MagicMock(is_root=is_root)
    mgr = MagicMock()
    return Stealth(adb=adb, root=root, manager=mgr), adb


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------
def test_score_clean():
    findings = [Finding("a", "1", "clean")]
    assert _score(findings)["verdict"] == "clean"


def test_score_hides_needed_when_only_fixable():
    findings = [Finding("a", "1", "leak")]
    verdict = _score(findings)
    assert verdict["verdict"] == "hides_needed"
    assert verdict["fixable"] == 1


def test_score_compromised_beats_hides_needed():
    """One structural tell is enough; a clean fixable count must not mask it."""
    findings = [Finding("a", "1", "clean"), Finding("b", "1", "unfixable")]
    assert _score(findings)["verdict"] == "compromised"


def test_scan_dict_separates_fixable_from_unfixable():
    scan = Scan(findings=[Finding("a", "1", "leak"), Finding("b", "1", "unfixable"),
                          Finding("c", "1", "clean")])
    payload = scan.to_dict()
    assert payload["counts"] == {"total": 3, "clean": 1, "leak": 1, "unfixable": 1}
    assert [f["name"] for f in payload["fixable_leaks"]] == ["a"]
    assert [f["name"] for f in payload["unfixable"]] == ["b"]


# ---------------------------------------------------------------------------
# marker detection
# ---------------------------------------------------------------------------
def test_markers_only_count_live_values():
    """A deleted property lingers with an empty value; that is not a leak."""
    s, _ = _stealth()
    props = {"init.svc.nemuhotd": "running", "nemud.fps_limit": "", "ro.hardware": "qcom"}
    finding = s._markers_finding(props)
    assert finding.status == "leak"
    assert "nemuhotd" in finding.detail
    assert "fps_limit" not in finding.detail


def test_markers_clean_when_all_empty():
    s, _ = _stealth()
    finding = s._markers_finding({"nemud.fps_limit": "", "ro.hardware": "qcom"})
    assert finding.status == "clean"


def test_marker_list_covers_the_vendors_we_saw():
    for marker in ("nemu", "mumu", "qemu", "goldfish"):
        assert marker in EMULATOR_MARKERS


# ---------------------------------------------------------------------------
# property profile
# ---------------------------------------------------------------------------
def test_props_finding_flags_mismatch():
    s, _ = _stealth()
    props = {k: want for k, (want, _w) in PROP_PROFILE.items()}
    props["ro.hardware"] = "Samsung"      # wrong
    finding = s._props_finding(props)
    assert finding.status == "leak"
    assert "ro.hardware" in finding.detail


def test_props_finding_clean_when_all_match():
    s, _ = _stealth()
    props = {k: want for k, (want, _w) in PROP_PROFILE.items()}
    assert s._props_finding(props).status == "clean"


def test_empty_expected_means_must_be_empty():
    s, _ = _stealth()
    props = {k: want for k, (want, _w) in PROP_PROFILE.items()}
    props["ro.kernel.qemu"] = "1"
    assert s._props_finding(props).status == "leak"


# ---------------------------------------------------------------------------
# sensor / kernel grading
# ---------------------------------------------------------------------------
def test_sensor_count_is_unfixable_when_absent():
    s, _ = _stealth({"dumpsys sensorservice 2>/dev/null | sed -n '/Sensor List:/,/^$/p' "
                     "| grep -cE '^0x[0-9a-f]{8}\\)'": "1"})
    finding = s._sensors_finding(0)
    assert finding.status == "unfixable"
    assert "sensors HAL" in finding.fix


def test_sensor_count_clean_when_plausible():
    s, _ = _stealth({"dumpsys sensorservice 2>/dev/null | sed -n '/Sensor List:/,/^$/p' "
                     "| grep -cE '^0x[0-9a-f]{8}\\)'": "15"})
    assert s._sensors_finding(0).status == "clean"


def test_sensor_count_reads_the_hex_handle_form():
    """The first version grepped `^[0-9]{6,}` and reported 1 on an image with 15."""
    s, adb = _stealth({"dumpsys sensorservice 2>/dev/null | sed -n '/Sensor List:/,/^$/p' "
                       "| grep -cE '^0x[0-9a-f]{8}\\)'": "15"})
    s._sensors_finding(0)
    cmd = adb.shell.call_args[0][1]
    assert "0x[0-9a-f]{8}" in cmd, "must anchor on the hex handle, not on digits"


def test_kernel_inconsistency_lists_each_open_tell():
    """The finding must name *which* tell is open, not aggregate them into one word."""
    s, _ = _stealth({
        "uname -m": "aarch64",
        "uname -r": "6.1.90-perf+",
        "grep -m1 'vendor_id\\|CPU implementer' /proc/cpuinfo": "vendor_id : AuthenticAMD",
        "grep -m1 '^flags' /proc/cpuinfo": "flags : fpu hypervisor avx2",
        "grep -m1 init_module /proc/kallsyms 2>/dev/null": "ffffffff T __x64_sys_init_module",
    })
    finding = s._kernel_finding(0)
    assert finding.status == "unfixable"
    assert "hypervisor flag" in finding.detail
    assert "cpuinfo says x86" in finding.detail
    assert "uname -r" in finding.detail


def test_kernel_clean_when_cpuinfo_and_kallsyms_covered():
    """After the bind-mount layer, only uname -r should remain open."""
    s, _ = _stealth({
        "uname -m": "aarch64",
        "uname -r": "6.1.75-android14-11-g4d9e0d1b2c3f",
        "grep -m1 'vendor_id\\|CPU implementer' /proc/cpuinfo": "CPU implementer : 0x41",
        "grep -m1 '^flags' /proc/cpuinfo": "",
        "grep -m1 init_module /proc/kallsyms 2>/dev/null": "",
    })
    assert s._kernel_finding(0).status == "clean"


def test_selinux_permissive_without_hide_is_a_fixable_leak():
    """KernelSU ships `selinux_hide`, so this is fixable — not unfixable as first assumed."""
    s, _ = _stealth({"getenforce": "Permissive",
                     "timeout 10 /data/adb/ksu/bin/ksud feature get selinux_hide 2>/dev/null "
                     "| grep -i '^Value' | grep -oE '[01]'": "0"})
    finding = s._selinux_finding(0)
    assert finding.status == "leak"
    assert "selinux_hide" in finding.fix


def test_selinux_permissive_with_hide_enabled_is_clean():
    """selinux_hide only sanitises the value for app UIDs; root still reads Permissive."""
    s, _ = _stealth({"getenforce": "Permissive",
                     "timeout 10 /data/adb/ksu/bin/ksud feature get selinux_hide 2>/dev/null "
                     "| grep -i '^Value' | grep -oE '[01]'": "1"})
    finding = s._selinux_finding(0)
    assert finding.status == "clean"
    assert "hidden from apps" in finding.value


def test_selinux_enforcing_is_clean():
    s, _ = _stealth({"getenforce": "Enforcing"})
    assert s._selinux_finding(0).status == "clean"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_scan_requires_root():
    s, _ = _stealth(is_root=False)
    with pytest.raises(BadArgument) as exc:
        s.scan(0)
    assert "root" in exc.value.message


def test_hide_requires_root():
    s, _ = _stealth(is_root=False)
    with pytest.raises(BadArgument):
        s.hide(0)


def test_hide_requires_resetprop():
    s, _ = _stealth({"ls /data/adb/ksu/bin/resetprop 2>/dev/null": ""})
    with pytest.raises(BadArgument) as exc:
        s.hide(0)
    assert "resetprop" in exc.value.message


def test_nemu_services_are_the_ones_we_measured():
    assert "nemuhotd" in NEMU_SERVICES
    assert "nemuinput" in NEMU_SERVICES


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_scan_shape():
    from mumu_mcp.server import mumu_root_adb, mumu_stealth_scan

    mumu_root_adb.__wrapped__(0, True)
    r = mumu_stealth_scan.__wrapped__(0)
    assert r["score"]["verdict"] in {"clean", "hides_needed", "compromised"}
    assert r["counts"]["total"] >= 8
    names = {f["name"] for f in r["all"]}
    assert {"properties", "sensor_count", "kernel_consistency"} <= names


@LIVE
def test_live_hide_then_verify():
    """Round trip: hide, confirm the properties verify, then restore."""
    from mumu_mcp.server import mumu_root_adb, mumu_stealth_hide

    mumu_root_adb.__wrapped__(0, True)
    result = mumu_stealth_hide.__wrapped__(0, properties=True, root_hide=False)
    assert result["verified"]["clean"] is True, result["verified"]
    assert not result["verified"]["markers_left"]

    restored = mumu_stealth_hide.__wrapped__(0, restore=True)
    assert restored["restored"], "restore must return the values it put back"
