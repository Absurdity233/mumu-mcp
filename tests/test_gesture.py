"""Gesture-channel tests. The event sequences are pure data, so most is hermetic."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from mumu_mcp.errors import BadArgument
from mumu_mcp.gesture import (
    ABS_MT_POSITION_X,
    ABS_MT_SLOT,
    ABS_MT_TRACKING_ID,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    SYN_REPORT,
    Gestures,
    TouchDevice,
    _interpolate,
    _interpolate_points,
)

LIVE = pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1")


def _gestures() -> tuple[Gestures, MagicMock]:
    adb = MagicMock()
    adb.shell = MagicMock(return_value="")
    g = Gestures(adb=adb)
    g._devices[0] = TouchDevice(path="/dev/input/event4", name="t",
                                max_x=1440, max_y=2560, slots=32)
    return g, adb


# ---------------------------------------------------------------------------
# interpolation
# ---------------------------------------------------------------------------
def test_interpolate_reaches_endpoints():
    out = _interpolate(10, 20, 5)
    assert out[0] == 10 and out[-1] == 20
    assert len(out) == 5


def test_interpolate_is_monotonic():
    out = _interpolate(0, 100, 11)
    assert out == sorted(out)


def test_interpolate_single_step_gives_ends():
    assert _interpolate(5, 9, 1) == [5, 9]


def test_interpolate_points():
    pts = _interpolate_points(0, 0, 10, 20, 3)
    assert pts[0] == (0, 0)
    assert pts[-1] == (10, 20)
    assert len(pts) == 3


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------
def test_begin_frame_sets_tracking_ids():
    frame = Gestures._begin_frame([[100, 200], [300, 400]])
    assert (EV_ABS, ABS_MT_SLOT, 0) in frame
    assert (EV_ABS, ABS_MT_TRACKING_ID, 100) in frame
    assert (EV_ABS, ABS_MT_TRACKING_ID, 101) in frame
    assert (EV_ABS, ABS_MT_POSITION_X, 100) in frame
    assert (EV_KEY, BTN_TOUCH, 1) in frame
    assert frame[-1] == (EV_SYN, SYN_REPORT, 0)


def test_end_frame_lifts_every_finger():
    frame = Gestures._end_frame([[100, 200], [300, 400]])
    assert frame.count((EV_ABS, ABS_MT_TRACKING_ID, -1)) == 2
    assert (EV_KEY, BTN_TOUCH, 0) in frame
    assert frame[-1] == (EV_SYN, SYN_REPORT, 0)


def test_move_frame_has_no_tracking_id():
    """A move must not re-introduce the finger; that would restart the gesture."""
    frame = Gestures._move_frame([[150, 250], [350, 450]])
    assert not any(code == ABS_MT_TRACKING_ID for _, code, _ in frame)
    assert frame[-1] == (EV_SYN, SYN_REPORT, 0)


def test_every_frame_ends_with_syn():
    for builder in (Gestures._begin_frame, Gestures._move_frame, Gestures._end_frame):
        assert builder([[1, 2], [3, 4]])[-1] == (EV_SYN, SYN_REPORT, 0)


# ---------------------------------------------------------------------------
# one-call batching
# ---------------------------------------------------------------------------
def test_pinch_is_one_adb_call():
    """Frames spread across calls are not a gesture; they must ship together."""
    g, adb = _gestures()
    g.pinch(0, 720, 1280, start=100, end=300, steps=6)
    assert adb.shell.call_count == 1


def test_pinch_paces_frames_inside_the_guest():
    g, adb = _gestures()
    g.pinch(0, 720, 1280, start=100, end=300, steps=6, duration_ms=300)
    script = adb.shell.call_args[0][1]
    assert "sleep 0." in script


def test_two_finger_drag_is_one_call():
    g, adb = _gestures()
    g.two_finger_drag(0, 700, 1800, 700, 900, gap=100, steps=5)
    assert adb.shell.call_count == 1


def test_long_press_holds_inside_the_guest():
    g, adb = _gestures()
    g.long_press_two_finger(0, 500, 500, gap=80, hold_ms=800)
    assert adb.shell.call_count == 1
    assert "sleep 0.800" in adb.shell.call_args[0][1]


def test_raw_event_is_one_call():
    g, adb = _gestures()
    g.raw_event(0, EV_KEY, BTN_TOUCH, 1)
    assert adb.shell.call_count == 1


def test_raw_sequence_batches():
    g, adb = _gestures()
    out = g.raw_sequence(0, [(3, 47, 0), (3, 57, 1), (0, 0, 0)])
    assert adb.shell.call_count == 1
    assert out["count"] == 3


def test_raw_sequence_rejects_empty():
    g, _ = _gestures()
    with pytest.raises(BadArgument):
        g.raw_sequence(0, [])


def test_pinch_rejects_zero_distance():
    g, _ = _gestures()
    with pytest.raises(BadArgument):
        g.pinch(0, 100, 100, start=0, end=100)


def test_drag_rejects_zero_gap():
    g, _ = _gestures()
    with pytest.raises(BadArgument):
        g.two_finger_drag(0, 0, 0, 10, 10, gap=0)


def test_pinch_axis_y_moves_y():
    g, adb = _gestures()
    out = g.pinch(0, 500, 500, start=100, end=200, steps=3, axis="y")
    script = adb.shell.call_args[0][1]
    assert out["gesture"] == "pinch_y"
    # x stays at 500 for both fingers; y differs
    assert "0x35 500" in script.replace("3 53", "0x35")


# ---------------------------------------------------------------------------
# device discovery
# ---------------------------------------------------------------------------
def test_touch_device_parses_capabilities():
    adb = MagicMock()
    adb.shell = MagicMock(return_value=(
        'DEV:/dev/input/event4\n'
        '  name:     "Xiaomi Touchscreen"\n'
        '    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 31\n'
        '                ABS_MT_POSITION_X     : value 0, min 0, max 1440\n'
        '                ABS_MT_POSITION_Y     : value 0, min 0, max 2560\n'
    ))
    g = Gestures(adb=adb)
    dev = g.touch_device(0)
    assert dev.path == "/dev/input/event4"
    assert dev.name == "Xiaomi Touchscreen"
    assert dev.max_x == 1440
    assert dev.max_y == 2560
    assert dev.slots == 32


def test_touch_device_signals_when_absent():
    adb = MagicMock()
    adb.shell = MagicMock(return_value="")
    g = Gestures(adb=adb)
    with pytest.raises(BadArgument) as exc:
        g.touch_device(0)
    assert "no multitouch" in exc.value.message


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------
@LIVE
def test_live_touch_device():
    from mumu_mcp.server import mumu_touch_device

    dev = mumu_touch_device.__wrapped__(0)
    assert dev["device"].startswith("/dev/input/event")
    assert dev["max_x"] > 0 and dev["max_y"] > 0


@LIVE
def test_live_raw_sequence_tap_navigates():
    """The one claim that must hold: a batched single-finger tap reaches the app.

    Taps the launcher's game-centre icon through the raw channel and requires the
    foreground to actually change — a tolerance of "anything goes" would make this
    test unable to fail.
    """
    import time

    from mumu_mcp import server
    from mumu_mcp.server import mumu_foreground, mumu_key

    mumu_key.__wrapped__(0, "home")
    time.sleep(2)
    before = mumu_foreground.__wrapped__(0)["foreground"]["package"]
    if before != "app.lawnchair":
        pytest.skip(f"launcher not in front (got {before}); cannot run the tap check")

    seq = [(3, 0x2F, 0), (3, 0x39, 300), (3, 0x35, 195), (3, 0x36, 997),
           (1, 0x14A, 1), (0, 0, 0), (3, 0x39, -1), (1, 0x14A, 0), (0, 0, 0)]
    server.gestures().raw_sequence(0, seq)
    time.sleep(3)
    after = mumu_foreground.__wrapped__(0)["foreground"]["package"]

    mumu_key.__wrapped__(0, "home")
    assert after != "app.lawnchair", (
        "the raw tap did not leave the launcher; the kernel event channel is not reaching the app"
    )
