"""Scripted UI flows: a step list instead of a dozen tool calls.

An agent driving an emulator by hand needs 5–15 round trips per screen: dump the
tree, find the node, tap its centre, wait for the next screen, dump again. That
is slow, and every intermediate step is a chance to lose the thread. This module
runs the whole list inside one tool call and stops at the first failure with the
step index that broke, so the caller gets a precise error instead of a shrug.

The steps deliberately mirror what the other tools do — `tap`, `swipe`, `text`,
`key`, `wait_for`, `wait_activity`, `assert_text`, `launch`, `sleep`, `shell` — so
a flow is just the tools it replaces, in a list.

Design notes that matter for reliability:

* `wait_for` polls the parsed hierarchy, not a screenshot, so it is immune to
  animation and to OCR noise.
* A node locator can be text, content-desc, resource-id, or a `(x, y)` pair, and
  `index` disambiguates the common case of several nodes with the same label.
* Every step records what it saw, so a failing flow is diagnosable without
  re-running it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .adb import Adb
from .constants import KEYCODES
from .device import Device
from .errors import BadArgument, MuMuError, Timeout

log = logging.getLogger("mumu_mcp.flow")

STEP_KINDS = {
    "tap", "swipe", "long_press", "text", "key",
    "wait_for", "wait_gone", "wait_activity", "assert_text",
    "launch", "sleep", "shell", "screenshot",
}


@dataclass
class StepResult:
    index: int
    kind: str
    ok: bool
    ms: int
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.index, "kind": self.kind,
                               "ok": self.ok, "ms": self.ms}
        if self.detail:
            out["detail"] = self.detail
        if self.error:
            out["error"] = self.error
        return out


class Flow:
    """Executes a step list against one instance."""

    def __init__(self, adb: Adb, device: Device, ui_dump, screenshot):
        # `ui_dump` and `screenshot` are injected so this module does not import
        # server.py (which owns the MCP decorators) and create a cycle.
        self.adb = adb
        self.device = device
        self._ui_dump = ui_dump
        self._screenshot = screenshot

    def run(self, vmindex: int, steps: Any, *, stop_on_error: bool = True) -> dict[str, Any]:
        if not isinstance(steps, list) or not steps:
            raise BadArgument("steps must be a non-empty list")
        if len(steps) > 60:
            raise BadArgument("at most 60 steps per flow; split it up")

        results: list[StepResult] = []
        started = time.monotonic()
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise BadArgument(f"step {i} is not an object", detail={"got": type(raw).__name__})
            kind = str(raw.get("action") or raw.get("kind") or "").strip()
            if kind not in STEP_KINDS:
                raise BadArgument(
                    f"step {i}: unknown action {kind!r}",
                    detail={"allowed": sorted(STEP_KINDS)},
                )
            t0 = time.monotonic()
            try:
                detail = self._dispatch(vmindex, kind, raw)
                results.append(StepResult(i, kind, True, int((time.monotonic() - t0) * 1000), detail))
            except MuMuError as exc:
                results.append(StepResult(i, kind, False, int((time.monotonic() - t0) * 1000),
                                          dict(exc.detail or {}), exc.message))
                if stop_on_error:
                    return {
                        "completed": False,
                        "failed_step": i,
                        "total_ms": int((time.monotonic() - started) * 1000),
                        "results": [r.to_dict() for r in results],
                        "error": exc.to_dict(),
                    }
        return {
            "completed": True,
            "steps_run": len(results),
            "total_ms": int((time.monotonic() - started) * 1000),
            "results": [r.to_dict() for r in results],
        }

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, vmindex: int, kind: str, step: dict[str, Any]) -> dict[str, Any]:
        if kind == "tap":
            x, y = self._point(vmindex, step)
            self.adb.shell(vmindex, f"input tap {x} {y}")
            return {"x": x, "y": y}
        if kind == "swipe":
            x1, y1, x2, y2 = (int(step.get(k, 0)) for k in ("x1", "y1", "x2", "y2"))
            duration = int(step.get("duration_ms", 300))
            self.adb.shell(vmindex, f"input swipe {x1} {y1} {x2} {y2} {duration}")
            return {"from": [x1, y1], "to": [x2, y2], "duration_ms": duration}
        if kind == "long_press":
            x, y = self._point(vmindex, step)
            duration = int(step.get("duration_ms", 800))
            self.adb.shell(vmindex, f"input swipe {x} {y} {x} {y} {duration}")
            return {"x": x, "y": y, "duration_ms": duration}
        if kind == "text":
            value = step.get("text", "")
            if not value:
                raise BadArgument("text step needs a `text` field")
            self.adb.input_text(vmindex, value)
            return {"typed": value}
        if kind == "key":
            key = str(step.get("key", "")).lower()
            code = int(key) if key.isdigit() else KEYCODES.get(key)
            if code is None:
                raise BadArgument(f"unknown key {key!r}", detail={"known": sorted(KEYCODES)})
            self.adb.shell(vmindex, f"input keyevent {code}")
            return {"key": key, "keycode": code}
        if kind in {"wait_for", "wait_gone"}:
            return self._wait(vmindex, step, gone=(kind == "wait_gone"))
        if kind == "wait_activity":
            target = step.get("target") or step.get("package") or step.get("text")
            if not target:
                raise BadArgument("wait_activity needs `target` (package or regex)")
            fg = self.device.wait_for_activity(
                vmindex, target, timeout=float(step.get("timeout_s", 30)))
            return {"foreground": fg.to_dict()}
        if kind == "assert_text":
            return self._assert(vmindex, step)
        if kind == "launch":
            return self.device.launch(
                vmindex,
                component=step.get("component", ""),
                package=step.get("package", ""),
                activity=step.get("activity", ""),
                wait=bool(step.get("wait", True)),
                force_stop=bool(step.get("force_stop", False)),
            )
        if kind == "sleep":
            ms = min(int(step.get("ms", 500)), 30_000)
            time.sleep(ms / 1000)
            return {"slept_ms": ms}
        if kind == "shell":
            cmd = step.get("command", "")
            if not cmd:
                raise BadArgument("shell step needs a `command`")
            return {"output": self.adb.shell(vmindex, cmd, timeout=float(step.get("timeout_s", 30)))[:2000]}
        if kind == "screenshot":
            path = self._screenshot(vmindex, step.get("path", ""))
            return {"path": path}
        raise BadArgument(f"unhandled action {kind!r}")  # pragma: no cover

    # -- helpers -----------------------------------------------------------
    def _point(self, vmindex: int, step: dict[str, Any]) -> tuple[int, int]:
        """Resolve a tap target: explicit coordinates, or a node locator."""
        if "x" in step and "y" in step:
            return int(step["x"]), int(step["y"])
        node = self._find(vmindex, step, timeout=float(step.get("timeout_s", 10)))
        centre = node.get("center")
        if not centre:
            raise BadArgument("the located node has no bounds, so it cannot be tapped",
                              detail={"node": node})
        return int(centre[0]), int(centre[1])

    def _find(self, vmindex: int, step: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Poll the hierarchy until a node matches the step's locator."""
        text = step.get("text", "")
        desc = step.get("desc", "")
        rid = step.get("id", "")
        index = int(step.get("index", 0))
        exact = bool(step.get("exact", False))
        if not (text or desc or rid):
            raise BadArgument("a locator needs one of text, desc, id")

        deadline = time.monotonic() + max(0.5, timeout)
        last_count = -1
        while time.monotonic() < deadline:
            dump = self._ui_dump(vmindex, simplify=True)
            hits = _match(dump["nodes"], text=text, desc=desc, rid=rid, exact=exact)
            last_count = len(hits)
            if len(hits) > index:
                return hits[index]
            time.sleep(0.5)
        raise Timeout(
            f"no node matched text={text!r} desc={desc!r} id={rid!r} (index {index}) in {timeout}s",
            detail={"matches_seen": max(0, last_count)},
        )

    def _wait(self, vmindex: int, step: dict[str, Any], *, gone: bool) -> dict[str, Any]:
        timeout = float(step.get("timeout_s", 20))
        if gone:
            deadline = time.monotonic() + max(0.5, timeout)
            while time.monotonic() < deadline:
                dump = self._ui_dump(vmindex, simplify=True)
                if not _match(dump["nodes"], text=step.get("text", ""), desc=step.get("desc", ""),
                              rid=step.get("id", ""), exact=bool(step.get("exact", False))):
                    return {"gone": True, "waited_s": round(timeout - (deadline - time.monotonic()), 1)}
                time.sleep(0.5)
            raise Timeout(f"node still present after {timeout}s",
                          detail={"text": step.get("text", "")})
        node = self._find(vmindex, step, timeout=timeout)
        return {"found": True, "node": node}

    def _assert(self, vmindex: int, step: dict[str, Any]) -> dict[str, Any]:
        want = step.get("text") or step.get("desc") or step.get("id")
        if not want:
            raise BadArgument("assert_text needs `text` (or desc/id)")
        dump = self._ui_dump(vmindex, simplify=True)
        if not _match(dump["nodes"], text=step.get("text", ""), desc=step.get("desc", ""),
                      rid=step.get("id", ""), exact=bool(step.get("exact", False))):
            raise BadArgument(
                f"assertion failed: {want!r} is not on screen",
                detail={"node_count": dump.get("count"),
                        "sample": [n.get("text") or n.get("desc")
                                   for n in dump["nodes"][:8] if n.get("text") or n.get("desc")]},
            )
        return {"asserted": want}


def _match(nodes: list[dict[str, Any]], *, text: str = "", desc: str = "",
           rid: str = "", exact: bool = False) -> list[dict[str, Any]]:
    """Filter nodes by any of text/desc/id; substring by default, exact on request."""
    needle_t, needle_d, needle_i = text.lower(), desc.lower(), rid.lower()

    def hit(value: str, needle: str) -> bool:
        if not needle:
            return False
        value = (value or "").lower()
        return value == needle if exact else needle in value

    return [
        n for n in nodes
        if hit(n.get("text", ""), needle_t)
        or hit(n.get("desc", ""), needle_d)
        or hit(n.get("id", ""), needle_i)
    ]


def parse_steps(raw: Any) -> list[dict[str, Any]]:
    """Accept a JSON string as well as a list, because MCP clients differ."""
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BadArgument(f"steps is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise BadArgument("steps must be a list of step objects")
    return raw
