"""Root-inspection tests. No emulator: the state parser and the guards are pure.

Live behaviour is covered by `test_live_root_*`, which needs MUMU_LIVE=1.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mumu_mcp.errors import BadArgument, MuMuError
from mumu_mcp.root import DIFF_IMAGE, Root, RootState, _field, notes


# ---------------------------------------------------------------------------
# sentinel parsing
# ---------------------------------------------------------------------------
def test_field_extracts_value():
    blob = "ID:uid=0(root)\nENFORCE:Permissive\nADBROOT:1"
    assert _field(blob, "ID") == "uid=0(root)"
    assert _field(blob, "ENFORCE") == "Permissive"
    assert _field(blob, "ADBROOT") == "1"


def test_field_missing_returns_empty():
    assert _field("ID:x", "ENFORCE") == ""


def test_field_ignores_prefix_collisions():
    # ADBROOT must not be matched by an ADB line.
    blob = "ADB:1\nADBROOT:0"
    assert _field(blob, "ADBROOT") == "0"


def test_field_value_with_colons_survives():
    blob = "ID:uid=0(root) context=u:r:su:s0"
    assert _field(blob, "ID").endswith("u:r:su:s0")


# ---------------------------------------------------------------------------
# notes: the part the model actually reads
# ---------------------------------------------------------------------------
def _state(**kw) -> RootState:
    base = dict(
        vmindex=0, uid=0, user="root", selinux="Permissive", context="u:r:su:s0",
        is_root=True, system_writable=True, overlay=True,
        upperdir="/mnt/scratch/upperdir", upperdir_visible=False,
        diff_image=None, diff_image_bytes=None, adb_root_prop=1,
        root_permission="true", su_binary=False,
    )
    base.update(kw)
    return RootState(**base)


def test_notes_flags_missing_su():
    text = " ".join(notes(_state()))
    assert "no `su`" in text


def test_notes_says_no_su_needed_when_root():
    text = " ".join(notes(_state(is_root=True)))
    assert "no `su` needed" in text


def test_notes_tells_you_to_enable_root_when_not_root():
    text = " ".join(notes(_state(is_root=False, uid=2000, user="shell")))
    assert "mumu_root_adb" in text


def test_notes_mentions_host_side_upperdir():
    text = " ".join(notes(_state(upperdir_visible=False)))
    assert "host-side" in text


def test_notes_warns_when_system_read_only():
    text = " ".join(notes(_state(system_writable=False, overlay=False)))
    assert "read-only" in text


def test_notes_reports_permissive_selinux():
    text = " ".join(notes(_state(selinux="Permissive")))
    assert "Permissive" in text


def test_notes_are_all_actionable():
    """Every note must say something the caller can act on — no filler."""
    out = notes(_state())
    assert out
    for line in out:
        assert len(line) > 20
        assert not line.endswith(".")  # terse, not prose


def test_state_dict_reports_reboot_persistence():
    payload = _state(overlay=True).to_dict()
    # overlay implies host-side diff image, which survives reboot
    assert payload["system_overlay"] is True
    assert payload["overlay_upperdir_visible"] is False


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
class _FakeAdb:
    def __init__(self):
        self.calls = []
        self.invalidated = []

    def shell(self, vmindex, command, **kw):
        self.calls.append(command)
        if command.strip().startswith("echo ID:"):
            return (
                "ID:uid=0(root) gid=0(root) context=u:r:su:s0\n"
                "ENFORCE:Permissive\n"
                "ADBROOT:1\n"
                "SU:\n"
                "UPPER:no\n"
                "overlay on /system type overlay (rw,upperdir=/mnt/scratch/upperdir,"
                "workdir=/mnt/scratch/workdir,lowerdir=/system)"
            )
        return ""

    def invalidate(self, vmindex=None):
        self.invalidated.append(vmindex)

    def run(self, *a, **kw):
        return ""


class _FakeManager:
    home = Path(r"D:\Program Files\Netease\MuMu\nx_main")

    def call(self, *a, **kw):
        return {"root_permission": "true", "system_disk_readonly": "false"}

    def run(self, *a, **kw):
        return ""

    def setting_set(self, *a, **kw):
        return {}

    def sh(self, *a, **kw):
        return "1"

    def setting(self, *a, **kw):  # pragma: no cover
        return {}


def test_state_parses_single_round_trip():
    adb = _FakeAdb()
    root = Root(adb=adb, manager=_FakeManager())
    state = root.state(0)
    assert state.is_root is True
    assert state.uid == 0
    assert state.selinux == "Permissive"
    assert state.overlay is True
    assert state.system_writable is True
    assert state.su_binary is False
    # one adb call, not a dozen
    assert len(adb.calls) == 1


def test_restore_requires_confirm():
    root = Root(adb=_FakeAdb(), manager=_FakeManager())
    with pytest.raises(BadArgument) as exc:
        root.restore_system_diff(0, confirm=False)
    assert "confirm=True" in exc.value.message


def test_restore_refuses_while_running(tmp_path: Path):
    root = Root(adb=_FakeAdb(), manager=_FakeManager())
    with patch.object(Root, "vm_dir", return_value=tmp_path), \
         patch.object(Root, "_instance_state", return_value={"is_process_started": True}), \
         pytest.raises(BadArgument) as exc:
        root.restore_system_diff(0, confirm=True)
    assert "stop the instance" in exc.value.message


def test_restore_reports_missing_image(tmp_path: Path):
    root = Root(adb=_FakeAdb(), manager=_FakeManager())
    with patch.object(Root, "vm_dir", return_value=tmp_path), \
         patch.object(Root, "_instance_state", return_value={"is_process_started": False}):
        out = root.restore_system_diff(0, confirm=True)
    assert out["removed"] is False


def test_restore_deletes_known_artifact(tmp_path: Path):
    img = tmp_path / DIFF_IMAGE
    img.write_bytes(b"x" * 2048)
    root = Root(adb=_FakeAdb(), manager=_FakeManager())
    with patch.object(Root, "vm_dir", return_value=tmp_path), \
         patch.object(Root, "_instance_state", return_value={"is_process_started": False}):
        out = root.restore_system_diff(0, confirm=True)
    assert out["removed"] is True
    assert out["bytes_reclaimed"] == 2048
    assert not img.exists()


def test_vm_dir_requires_vms_root(tmp_path: Path):
    root = Root(adb=_FakeAdb(), manager=_FakeManager())
    mgr = _FakeManager()
    mgr.home = tmp_path / "nx_main"
    mgr.home.mkdir()
    root.manager = mgr
    assert root.vm_dir(0) is None


def test_remount_rejects_outside_targets():
    root = Root(adb=_FakeAdb(), manager=_FakeManager())
    with pytest.raises(BadArgument):
        root.remount(0, "/data")


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


@LIVE
def test_live_root_state():
    from mumu_mcp.server import mumu_root_status

    payload = mumu_root_status.__wrapped__(0)["root"]
    assert payload["uid"] >= 0
    assert payload["selinux"] in {"Enforcing", "Permissive", "Disabled", "Unknown"}
    assert payload["notes"], "notes must always say something actionable"


@LIVE
def test_live_root_exec_requires_root():
    from mumu_mcp.server import mumu_root_exec, mumu_root_status

    state = mumu_root_status.__wrapped__(0)["root"]
    if state["is_root"]:
        out = mumu_root_exec.__wrapped__(0, "id")
        assert "uid=0" in out["output"]
    else:
        with pytest.raises(MuMuError):
            mumu_root_exec.__wrapped__(0, "id")
