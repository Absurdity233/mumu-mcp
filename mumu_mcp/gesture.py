"""The kernel input channel: raw events, and what multi-touch can actually do.

`adb shell input` on this image does single-pointer taps, swipes and key events
only. The touchscreen node does expose the full MT protocol B — `/dev/input/event4`
("Xiaomi Touchscreen") has `ABS_MT_SLOT` 0..31, `ABS_MT_POSITION_X` 0..1440,
`ABS_MT_POSITION_Y` 0..2560, `ABS_MT_TRACKING_ID` and `ABS_MT_TOUCH_MAJOR` — so
writing to it is a real, lower-level channel.

**What was measured, not assumed:**

* A batched single-finger tap written to the node *does* reach the app: sending
  down/SYN/up/SYN in one `adb shell` navigated the launcher into
  `com.mumu.store/.MainActivity`. Sending the same events as separate calls did
  not — 250 ms between down and up is a slow drag, not a tap. Hence `raw_sequence`
  exists and `raw_event` carries a warning.
* A single-finger drag written to the node scrolls a list.
* **Two-finger gestures do not reach the app on this build.** Pinch and two-finger
  drag were driven with a correct MT-B sequence (per-slot X/Y/TRACKING_ID, one
  SYN_REPORT per frame, 20 frames paced inside the guest) at the exact coordinates
  where a single-finger swipe scrolls; nothing moved. MuMu's host-side input
  bridge forwards single-pointer events and drops the multi-slot ones, and there
  is no MuMu setting that changes this.

So the `pinch`/`two_finger_drag` helpers are kept but documented as
platform-limited rather than presented as working: they emit a well-formed
gesture, and on a MuMu build whose bridge forwards multi-touch they will work
unchanged. Claiming otherwise would be the kind of half-tool this project avoids.

The consequence for callers: to zoom or pan content on the current build, drive
the UI's own controls (buttons, `seekBar`) or a per-app shortcut rather than a
pinch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .adb import Adb
from .errors import BadArgument

log = logging.getLogger("mumu_mcp.gesture")

# Linux input-event codes (linux/input-event-codes.h)
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3

ABS_MT_SLOT = 0x2F
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
BTN_TOUCH = 0x14A

SYN_REPORT = 0

DEFAULT_DEVICE = "/dev/input/event4"


@dataclass
class TouchDevice:
    path: str
    name: str
    max_x: int
    max_y: int
    slots: int


class Gestures:
    """Two-finger gestures, and a raw event channel for anything else."""

    def __init__(self, adb: Adb):
        self.adb = adb
        self._devices: dict[int, TouchDevice] = {}

    # -- discovery ---------------------------------------------------------
    def touch_device(self, vmindex: int = 0, *, refresh: bool = False) -> TouchDevice:
        """Find the multitouch node, rather than assuming the number.

        Device numbering differs between images and between boots, so the node is
        identified by capability (`ABS_MT_POSITION_X`) instead of by index.
        """
        if not refresh and vmindex in self._devices:
            return self._devices[vmindex]

        script = (
            "for d in /dev/input/event*; do "
            "if getevent -pl $d 2>/dev/null | grep -q ABS_MT_POSITION_X; then echo \"DEV:$d\"; "
            "getevent -pl $d 2>/dev/null | grep -E 'name:|ABS_MT_POSITION_X|ABS_MT_POSITION_Y|ABS_MT_SLOT'; "
            "break; fi; done"
        )
        out = self.adb.shell(vmindex, script, timeout=25)
        if "DEV:" not in out:
            raise BadArgument(
                "no multitouch input device found",
                detail={"output": out[:300],
                        "hint": "the image may not expose a touchscreen to the kernel"},
            )
        path = out.split("DEV:")[1].split()[0].strip()
        name = ""
        m = __import__("re").search(r'name:\s*"([^"]+)"', out)
        if m:
            name = m.group(1)

        def limit(code: str, default: int) -> int:
            mm = __import__("re").search(rf"{code}\s*:.*?max (\d+)", out)
            return int(mm.group(1)) if mm else default

        device = TouchDevice(
            path=path,
            name=name or path,
            max_x=limit("ABS_MT_POSITION_X", 1440),
            max_y=limit("ABS_MT_POSITION_Y", 2560),
            slots=limit("ABS_MT_SLOT", 10) + 1,
        )
        self._devices[vmindex] = device
        log.info("index %s: touch device %s (%dx%d, %d slots)",
                 vmindex, device.path, device.max_x, device.max_y, device.slots)
        return device

    # -- raw channel -------------------------------------------------------
    def _events(self, vmindex: int, events: list[tuple[int, int, int]],
                device: str | None = None) -> None:
        dev = device or self.touch_device(vmindex).path
        if not events:
            return
        # One `sh -c` with `;` separators: a single adb round trip per frame.
        # 15-event frames at 60 fps over adb would not keep up otherwise, which is
        # why gestures are built as few, larger frames.
        cmd = "; ".join(f"sendevent {dev} {t} {c} {v}" for t, c, v in events)
        self.adb.shell(vmindex, cmd, timeout=60)

    def _script(self, vmindex: int, frames: list[list[tuple[int, int, int]]],
                *, delay_ms: int = 0, device: str | None = None) -> None:
        """Send every frame of a gesture in ONE adb call, paced inside the guest.

        A pinch sent as ~10 separate `adb shell` calls has ~250 ms between frames,
        which most gesture detectors read as a long slow drag (or drop entirely).
        Pacing the frames with `sleep 0.0N` inside a single shell script keeps them
        milliseconds apart, which is what an app expects from a real gesture.
        """
        dev = device or self.touch_device(vmindex).path
        parts: list[str] = []
        pause = f"sleep {max(0.0, delay_ms / 1000):.3f}; " if delay_ms > 0 else ""
        for index, frame in enumerate(frames):
            if index and pause:
                parts.append(pause.rstrip())
            parts.append("; ".join(f"sendevent {dev} {t} {c} {v}" for t, c, v in frame))
        self.adb.shell(vmindex, "; ".join(parts), timeout=90)

    def raw_event(self, vmindex: int, type_: int, code: int, value: int,
                  device: str | None = None) -> dict[str, Any]:
        """Emit one input event. For anything the gesture helpers do not cover.

        Note the latency: each call is its own `adb shell`, so events land ~250 ms
        apart. A down/up pair sent as two calls reads as a slow drag, not a tap —
        use `raw_sequence` whenever the timing between events matters.
        """
        dev = device or self.touch_device(vmindex).path
        self._events(vmindex, [(type_, code, value)], device=dev)
        return {"device": dev, "type": type_, "code": code, "value": value}

    def raw_sequence(self, vmindex: int, events: list[tuple[int, int, int]],
                     device: str | None = None) -> dict[str, Any]:
        """Emit a batch of events in a single adb round trip.

        This is what makes a synthetic tap work: down -> SYN -> up -> SYN must
        happen within microseconds, and splitting it across separate `adb shell`
        calls stretches it into a drag that most apps ignore.
        """
        dev = device or self.touch_device(vmindex).path
        if not events:
            raise BadArgument("events is empty")
        self._events(vmindex, events, device=dev)
        return {"device": dev, "count": len(events), "events": events[:12]}

    # -- gestures ----------------------------------------------------------
    def pinch(self, vmindex: int, cx: int, cy: int, *, start: int, end: int,
              steps: int = 8, duration_ms: int = 300, axis: str = "x") -> dict[str, Any]:
        """Two-finger pinch or zoom around (cx, cy).

        `start` is the half-distance between the fingers at the beginning and
        `end` at the end: `end > start` zooms in, `end < start` zooms out.

        All frames go out in one adb call, paced inside the guest, because a
        gesture spread across separate round trips is not recognised as a gesture.
        """
        if start <= 0 or end <= 0:
            raise BadArgument("start and end must be positive half-distances")
        frames = _interpolate(start, end, max(2, steps))
        delay = max(4, int(duration_ms / max(1, len(frames) - 1)))

        def at(half: int) -> list[list[int]]:
            if axis == "y":
                return [[cx, cy - half], [cx, cy + half]]
            return [[cx - half, cy], [cx + half, cy]]

        script = (
            [self._begin_frame(at(start))]
            + [self._move_frame(at(half)) for half in frames[1:]]
            + [self._move_frame(at(end)), self._end_frame(at(end))]
        )
        self._script(vmindex, script, delay_ms=delay)
        return {"gesture": f"pinch_{axis}", "center": [cx, cy], "from": start, "to": end,
                "zoom": "in" if end > start else "out", "frames": len(frames),
                "duration_ms": duration_ms, "frame_delay_ms": delay}

    def two_finger_drag(self, vmindex: int, x1: int, y1: int, x2: int, y2: int, *,
                        gap: int = 120, steps: int = 8,
                        duration_ms: int = 300) -> dict[str, Any]:
        """Drag with two fingers held `gap` apart — the "move the map" gesture."""
        if gap <= 0:
            raise BadArgument("gap must be positive")
        frames = _interpolate_points(x1, y1, x2, y2, max(2, steps))
        delay = max(4, int(duration_ms / max(1, len(frames) - 1)))

        def at(px: int, py: int) -> list[list[int]]:
            return [[px, py - gap], [px, py + gap]]

        script = (
            [self._begin_frame(at(*frames[0]))]
            + [self._move_frame(at(*pt)) for pt in frames[1:]]
            + [self._move_frame(at(x2, y2)), self._end_frame(at(x2, y2))]
        )
        self._script(vmindex, script, delay_ms=delay)
        return {"gesture": "two_finger_drag", "from": [x1, y1], "to": [x2, y2],
                "gap": gap, "frames": len(frames), "frame_delay_ms": delay}

    def long_press_two_finger(self, vmindex: int, x: int, y: int, *,
                              gap: int = 120, hold_ms: int = 900) -> dict[str, Any]:
        """Press and hold with two fingers (some games gate actions behind this)."""
        pts = [[x, y - gap], [x, y + gap]]
        # hold_ms is spent inside the guest too, so the press is continuous.
        self._script(vmindex, [self._begin_frame(pts), self._end_frame(pts)],
                     delay_ms=max(50, hold_ms))
        return {"gesture": "two_finger_hold", "at": [x, y], "gap": gap, "hold_ms": hold_ms}
        return {"gesture": "two_finger_hold", "at": [x, y], "gap": gap, "hold_ms": hold_ms}

    # -- frame builders ----------------------------------------------------
    # These return event lists instead of sending them, so a whole gesture can be
    # concatenated into one shell script.
    @staticmethod
    def _begin_frame(points: list[list[int]]) -> list[tuple[int, int, int]]:
        """Introduce each finger with a tracking id, then SYN."""
        events: list[tuple[int, int, int]] = []
        for slot, (px, py) in enumerate(points):
            events.append((EV_ABS, ABS_MT_SLOT, slot))
            events.append((EV_ABS, ABS_MT_TRACKING_ID, 100 + slot))
            events.append((EV_ABS, ABS_MT_POSITION_X, int(px)))
            events.append((EV_ABS, ABS_MT_POSITION_Y, int(py)))
            events.append((EV_ABS, ABS_MT_TOUCH_MAJOR, 6))
        events.append((EV_KEY, BTN_TOUCH, 1))
        events.append((EV_SYN, SYN_REPORT, 0))
        return events

    @staticmethod
    def _move_frame(points: list[list[int]]) -> list[tuple[int, int, int]]:
        events: list[tuple[int, int, int]] = []
        for slot, (px, py) in enumerate(points):
            events.append((EV_ABS, ABS_MT_SLOT, slot))
            events.append((EV_ABS, ABS_MT_POSITION_X, int(px)))
            events.append((EV_ABS, ABS_MT_POSITION_Y, int(py)))
        events.append((EV_SYN, SYN_REPORT, 0))
        return events

    @staticmethod
    def _end_frame(points: list[list[int]]) -> list[tuple[int, int, int]]:
        """Lift every finger: TRACKING_ID -1 per slot, BTN_TOUCH up, SYN."""
        events: list[tuple[int, int, int]] = []
        for slot in range(len(points)):
            events.append((EV_ABS, ABS_MT_SLOT, slot))
            events.append((EV_ABS, ABS_MT_TRACKING_ID, -1))
        events.append((EV_KEY, BTN_TOUCH, 0))
        events.append((EV_SYN, SYN_REPORT, 0))
        return events


def _interpolate(start: int, end: int, steps: int) -> list[int]:
    if steps <= 1:
        return [start, end]
    span = end - start
    return [int(round(start + span * i / (steps - 1))) for i in range(steps)]


def _interpolate_points(x1: int, y1: int, x2: int, y2: int, steps: int) -> list[tuple[int, int]]:
    if steps <= 1:
        return [(x1, y1), (x2, y2)]
    return [
        (int(round(x1 + (x2 - x1) * i / (steps - 1))),
         int(round(y1 + (y2 - y1) * i / (steps - 1))))
        for i in range(steps)
    ]
