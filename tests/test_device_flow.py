"""Device-state and flow-runner tests. No emulator needed.

The parsers here are the part most likely to break when MuMu changes its dumpsys
or ps output, so they are tested against captured real output rather than
invented strings.
"""

from __future__ import annotations

import os

import pytest

from mumu_mcp.device import (
    ACTIVITY_RE,
    FOCUS_RE,
    Foreground,
    _battery_fields,
    _parse_ps,
)
from mumu_mcp.errors import BadArgument
from mumu_mcp.flow import _match, parse_steps

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


# ---------------------------------------------------------------------------
# foreground parsing — captured from a live dumpsys
# ---------------------------------------------------------------------------
DUMPSYS_ACTIVITY = (
    "  ResumedActivity: ActivityRecord{1 u0 app.lawnchair/.LawnchairLauncher t2}\n"
    "      topResumedActivity=ActivityRecord{16720749 u0 app.lawnchair/.LawnchairLauncher t2}\n"
)

DUMPSYS_WINDOW = (
    "  mCurrentFocus=Window{61e2e95 u0 app.lawnchair/app.lawnchair.LawnchairLauncher}\n"
    "  mFocusedApp=ActivityRecord{16720749 u0 app.lawnchair/.LawnchairLauncher t2}\n"
)


def test_activity_regex_full_class():
    """The full-class form appears in focus dumps; ACTIVITY_RE must read it there."""
    m = FOCUS_RE.search(DUMPSYS_WINDOW)
    assert m and m.group(1) == "app.lawnchair"
    assert m.group(2) == "app.lawnchair.LawnchairLauncher"


def test_activity_regex_matches_resumed_field():
    m = ACTIVITY_RE.search(
        "  topResumedActivity=ActivityRecord{16720749 u0 com.x/.Main t5}"
    )
    assert m and m.group(1) == "com.x" and m.group(2) == ".Main"


def test_activity_regex_relative_class():
    m = ACTIVITY_RE.search(DUMPSYS_ACTIVITY)
    assert m and m.group(2) == ".LawnchairLauncher"


def test_focus_regex():
    m = FOCUS_RE.search(DUMPSYS_WINDOW)
    assert m and m.group(1) == "app.lawnchair"


def test_foreground_component_joins():
    fg = Foreground("com.x", ".Main")
    assert fg.component == "com.x/.Main"


def test_foreground_component_without_activity():
    assert Foreground("com.x", "").component == "com.x"


# ---------------------------------------------------------------------------
# battery parsing
# ---------------------------------------------------------------------------
BATTERY_DUMP = """Current Battery Service state:
  AC powered: true
  USB powered: false
  status: 3
  health: 2
  present: true
  level: 86
  scale: 100
  voltage: 3
  temperature: 360
  technology: Li-ion
"""


def test_battery_fields_parsed():
    fields = _battery_fields(BATTERY_DUMP)
    assert fields["level"] == 86
    assert fields["status"] == 3
    assert fields["temperature"] == 360
    assert fields["ac_powered"] == "true"


def test_battery_missing_field_absent():
    assert "nonsense" not in _battery_fields(BATTERY_DUMP)


def test_battery_handles_negative():
    fields = _battery_fields("  temperature: -50\n")
    assert fields["temperature"] == -50


# ---------------------------------------------------------------------------
# ps parsing — captured from the guest
# ---------------------------------------------------------------------------
PS_OUT = """  PID USER         NAME                        %CPU %MEM
    1 root         init                         0.5  0.0
 1114 u0_a17       app.lawnchair                5.9  2.5
  726 system       surfaceflinger               2.7  0.2
 3010 root         [kworker/u12:2-events_unbound] 0.0  0.0
"""


def test_ps_rows_sorted_by_cpu():
    rows = _parse_ps(PS_OUT)
    assert rows[0]["name"] == "app.lawnchair"
    assert rows[0]["cpu_pct"] == 5.9
    assert rows[0]["pid"] == 1114


def test_ps_keeps_names_with_spaces():
    rows = _parse_ps(" 5 root some process name 1.0 0.0\n")
    assert rows[0]["name"] == "some process name"


def test_ps_skips_header_and_garbage():
    rows = _parse_ps("PID USER NAME %CPU %MEM\nnonsense line\n")
    assert rows == []


def test_ps_tolerates_missing_mem():
    rows = _parse_ps(" 9 root thing 1.0 notanumber\n")
    assert rows[0]["mem_pct"] is None


# ---------------------------------------------------------------------------
# flow: locator matching
# ---------------------------------------------------------------------------
NODES = [
    {"text": "登录", "desc": "", "id": "btn_login", "center": [100, 200]},
    {"text": "登录/注册", "desc": "", "id": "btn_login2", "center": [100, 300]},
    {"text": "", "desc": "关闭", "id": "close", "center": [10, 10]},
    {"text": "", "desc": "", "id": "edit_user", "center": [50, 60]},
]


def test_match_by_text_substring():
    hits = _match(NODES, text="登录")
    assert len(hits) == 2


def test_match_exact_narrows():
    hits = _match(NODES, text="登录", exact=True)
    assert len(hits) == 1
    assert hits[0]["id"] == "btn_login"


def test_match_by_desc():
    hits = _match(NODES, desc="关闭")
    assert len(hits) == 1


def test_match_by_id():
    hits = _match(NODES, rid="edit_user")
    assert len(hits) == 1


def test_match_is_case_insensitive():
    assert _match(NODES, rid="EDIT_USER")


def test_match_empty_locator_returns_nothing():
    # An empty needle must never match everything.
    assert _match(NODES) == []


# ---------------------------------------------------------------------------
# flow: step parsing
# ---------------------------------------------------------------------------
def test_parse_steps_accepts_list():
    assert parse_steps([{"action": "tap"}]) == [{"action": "tap"}]


def test_parse_steps_accepts_json_string():
    assert parse_steps('[{"action": "tap", "x": 1, "y": 2}]')[0]["x"] == 1


def test_parse_steps_rejects_bad_json():
    with pytest.raises(BadArgument) as exc:
        parse_steps("{not json")
    assert "not valid JSON" in exc.value.message


def test_parse_steps_rejects_non_list():
    with pytest.raises(BadArgument):
        parse_steps({"action": "tap"})


# ---------------------------------------------------------------------------
# flow: execution with a fake transport
# ---------------------------------------------------------------------------
class _FakeAdb:
    def __init__(self):
        self.shells = []

    def shell(self, vmindex, command, **kw):
        self.shells.append(command)
        return ""

    def input_text(self, vmindex, text):
        self.shells.append(f"text:{text}")
        return ""


class _FakeDevice:
    def __init__(self):
        self.launched = []
        self.waited = []

    def wait_for_activity(self, vmindex, target, timeout=30):
        self.waited.append(target)
        return Foreground("app.lawnchair", ".LawnchairLauncher")

    def launch(self, vmindex, **kw):
        self.launched.append(kw)
        return {"component": kw.get("component"), "total_ms": 12}


def _flow(nodes=None):
    from mumu_mcp.flow import Flow

    nodes = nodes if nodes is not None else NODES
    return Flow(
        adb=_FakeAdb(),
        device=_FakeDevice(),
        ui_dump=lambda idx, simplify=True: {"count": len(nodes), "nodes": nodes},
        screenshot=lambda idx, path="": path or "/tmp/x.png",
    )


def test_flow_runs_steps_in_order():
    f = _flow()
    out = f.run(0, [{"action": "tap", "x": 5, "y": 6},
                    {"action": "key", "key": "back"},
                    {"action": "sleep", "ms": 1}])
    assert out["completed"] is True
    assert out["steps_run"] == 3
    assert "input tap 5 6" in f.adb.shells
    assert "input keyevent 4" in f.adb.shells


def test_flow_tap_by_locator_uses_centre():
    f = _flow()
    out = f.run(0, [{"action": "tap", "id": "btn_login"}])
    assert out["completed"] is True
    assert "input tap 100 200" in f.adb.shells


def test_flow_stops_at_first_failure():
    f = _flow()
    out = f.run(0, [{"action": "assert_text", "text": "absent"},
                    {"action": "tap", "x": 1, "y": 1}])
    assert out["completed"] is False
    assert out["failed_step"] == 0
    assert "assertion failed" in out["error"]["message"]
    assert not any("input tap" in s for s in f.adb.shells)


def test_flow_continue_on_error():
    f = _flow()
    out = f.run(0, [{"action": "assert_text", "text": "absent"},
                    {"action": "tap", "x": 1, "y": 1}], stop_on_error=False)
    assert out["completed"] is True
    assert out["results"][0]["ok"] is False
    assert out["results"][1]["ok"] is True


def test_flow_rejects_unknown_action():
    with pytest.raises(BadArgument) as exc:
        _flow().run(0, [{"action": "teleport"}])
    assert "unknown action" in exc.value.message


def test_flow_rejects_empty_list():
    with pytest.raises(BadArgument):
        _flow().run(0, [])


def test_flow_rejects_step_limit():
    with pytest.raises(BadArgument):
        _flow().run(0, [{"action": "sleep", "ms": 1}] * 61)


def test_flow_wait_for_times_out():
    """A timeout is a step failure, not an exception: `run` reports it."""
    from mumu_mcp.flow import Flow

    f = Flow(adb=_FakeAdb(), device=_FakeDevice(),
             ui_dump=lambda idx, simplify=True: {"count": 0, "nodes": []},
             screenshot=lambda idx, path="": path)
    out = f.run(0, [{"action": "wait_for", "text": "never", "timeout_s": 0.05}])
    assert out["completed"] is False
    assert out["failed_step"] == 0
    assert out["error"]["error"] == "timeout"


def test_flow_wait_gone_succeeds_when_absent():
    from mumu_mcp.flow import Flow

    f = Flow(adb=_FakeAdb(), device=_FakeDevice(),
             ui_dump=lambda idx, simplify=True: {"count": 0, "nodes": []},
             screenshot=lambda idx, path="": path)
    out = f.run(0, [{"action": "wait_gone", "text": "spinner", "timeout_s": 1}])
    assert out["completed"] is True
    assert out["results"][0]["detail"]["gone"] is True


def test_flow_shell_step_captures_output():
    f = _flow()
    f.adb.shell = lambda vmindex, cmd, **kw: "prop-value"
    out = f.run(0, [{"action": "shell", "command": "getprop x"}])
    assert out["results"][0]["detail"]["output"] == "prop-value"


def test_flow_launch_delegates_to_device():
    f = _flow()
    f.run(0, [{"action": "launch", "package": "com.x", "activity": ".Main"}])
    assert f.device.launched[0]["package"] == "com.x"


# ---------------------------------------------------------------------------
# flow: hierarchy caching
# ---------------------------------------------------------------------------
def test_read_only_steps_reuse_one_dump():
    """A dump costs ~4 s; assert_text twice on one screen must not pay twice."""
    calls = {"n": 0}

    def counting_dump(idx, simplify=True):
        calls["n"] += 1
        return {"count": len(NODES), "nodes": NODES}

    from mumu_mcp.flow import Flow

    f = Flow(adb=_FakeAdb(), device=_FakeDevice(), ui_dump=counting_dump,
             screenshot=lambda idx, path="": path)
    out = f.run(0, [{"action": "assert_text", "text": "登录"},
                    {"action": "assert_text", "desc": "关闭"},
                    {"action": "wait_for", "desc": "关闭"}])
    assert out["completed"] is True
    assert calls["n"] == 1


def test_mutating_step_invalidates_cache():
    calls = {"n": 0}

    def counting_dump(idx, simplify=True):
        calls["n"] += 1
        return {"count": len(NODES), "nodes": NODES}

    from mumu_mcp.flow import Flow

    f = Flow(adb=_FakeAdb(), device=_FakeDevice(), ui_dump=counting_dump,
             screenshot=lambda idx, path="": path)
    f.run(0, [{"action": "assert_text", "text": "登录"},
              {"action": "tap", "x": 1, "y": 2},
              {"action": "assert_text", "text": "登录"}])
    # one dump for the first assert, another after the tap
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_foreground_is_resolvable():
    from mumu_mcp.server import mumu_foreground

    fg = mumu_foreground.__wrapped__(0)["foreground"]
    assert fg["package"], "must resolve a package"
    assert fg["component"]


@LIVE
def test_live_snapshot_has_core_fields():
    from mumu_mcp.server import mumu_snapshot

    snap = mumu_snapshot.__wrapped__(0)
    assert "foreground" in snap
    assert snap["screen"]["size"]
    assert snap["adbd_uid"] in (0, 2000)


@LIVE
def test_live_flow_reaches_launcher():
    from mumu_mcp.server import mumu_flow, mumu_foreground

    package = mumu_foreground.__wrapped__(0)["foreground"]["package"]
    out = mumu_flow.__wrapped__(0, [{"action": "wait_activity", "target": package,
                                     "timeout_s": 15}])
    assert out["completed"] is True
