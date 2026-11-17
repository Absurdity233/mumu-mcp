"""Unit tests: no emulator required. `pytest -m integration` needs a live one."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mumu_mcp import constants as C
from mumu_mcp.errors import (
    AdbUnavailable,
    BadArgument,
    InstanceNotFound,
    KeyNotWritable,
    ManagerFailed,
    MuMuError,
    from_errcode,
)
from mumu_mcp.manager import Manager, Run, decode, find_cli


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------
def test_decode_utf8_chinese():
    raw = "广告隐私权".encode()
    assert decode(raw) == "广告隐私权"


def test_decode_gbk_guest_text():
    raw = "安卓设备".encode("gb18030")
    assert decode(raw) == "安卓设备"


def test_decode_garbage_does_not_raise():
    assert decode(b"\xff\xfe\x80bad") != ""


def test_decode_empty():
    assert decode(b"") == ""


# ---------------------------------------------------------------------------
# Run.json error mapping
# ---------------------------------------------------------------------------
def _run(stdout: str = "", returncode: int = 0, stderr: str = "") -> Run:
    return Run(argv=["mumu-cli"], stdout=stdout, stderr=stderr, returncode=returncode)


def test_json_ok_payload():
    assert _run(json.dumps({"version": "6.8.14.0"})).json() == {"version": "6.8.14.0"}


def test_json_errcode_raises_typed_error():
    with pytest.raises(InstanceNotFound):
        _run(json.dumps({"errcode": -200, "errmsg": "player index not found"})).json()


def test_json_errcode_zero_is_success():
    payload = {"errcode": 0, "errmsg": ""}
    assert _run(json.dumps(payload)).json() == payload


def test_key_not_writable_maps():
    with pytest.raises(KeyNotWritable):
        _run(json.dumps({"errcode": -101, "errmsg": "key not writable"})).json()


def test_nonzero_exit_without_stdout_raises():
    with pytest.raises(ManagerFailed):
        _run("", returncode=2, stderr="boom").json()


def test_plain_text_passthrough():
    # `sh` returns raw text, not JSON.
    assert _run("1").json() == "1"


def test_json_or_empty_swallows_manager_errors():
    assert _run("", returncode=1, stderr="x").json_or_empty() == {}


# ---------------------------------------------------------------------------
# error payloads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "err,code",
    [
        (InstanceNotFound("a"), "instance_not_found"),
        (KeyNotWritable("a"), "key_not_writable"),
        (BadArgument("a"), "bad_argument"),
        (AdbUnavailable("a"), "adb_unavailable"),
    ],
)
def test_error_envelope(err, code):
    payload = err.to_dict()
    assert payload["ok"] is False
    assert payload["error"] == code
    assert payload["message"] == "a"


def test_retryable_flags():
    assert AdbUnavailable("x").retryable is True
    assert BadArgument("x").retryable is False


def test_unknown_errcode_falls_back():
    err = from_errcode(-999, "weird")
    assert isinstance(err, ManagerFailed)
    assert err.errcode == -999


# ---------------------------------------------------------------------------
# install discovery
# ---------------------------------------------------------------------------
def test_find_cli_explicit_binary(tmp_path: Path):
    binary = tmp_path / "MuMuManager.exe"
    binary.write_bytes(b"MZ")
    assert find_cli(str(binary)) == binary


def test_find_cli_explicit_dir(tmp_path: Path):
    (tmp_path / "mumu-cli.exe").write_bytes(b"MZ")
    assert find_cli(str(tmp_path)).name == "mumu-cli.exe"


def test_find_cli_missing_raises(tmp_path: Path):
    with pytest.raises(MuMuError) as exc:
        find_cli(str(tmp_path))
    assert exc.value.code == "install_not_found"


def test_find_cli_env_override(tmp_path: Path, monkeypatch):
    (tmp_path / "MuMuManager.exe").write_bytes(b"MZ")
    monkeypatch.setenv("MUMU_HOME", str(tmp_path))
    assert find_cli().name == "MuMuManager.exe"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
def _manager(tmp_path: Path) -> Manager:
    binary = tmp_path / "mumu-cli.exe"
    binary.write_bytes(b"MZ")
    return Manager(cli=binary)


def test_manager_instances_propagates_typed_error(tmp_path: Path):
    """`instances()` must raise, not return {} — a silent empty dict makes a
    "no instances" and a "manager refused" look identical to the caller."""
    mgr = _manager(tmp_path)
    failure = _run(json.dumps({"errcode": -200, "errmsg": "no"}))
    with patch.object(Manager, "run", return_value=failure), pytest.raises(InstanceNotFound):
        mgr.instances()


def test_manager_instances_filters_non_dict_entries(tmp_path: Path):
    mgr = _manager(tmp_path)
    payload = {"0": {"player_state": "start_finished"}, "note": "x"}
    with patch.object(Manager, "run", return_value=_run(json.dumps(payload))):
        assert set(mgr.instances()) == {"0"}


def test_manager_version_cached(tmp_path: Path):
    mgr = _manager(tmp_path)
    calls = []

    def fake_run(*a, **kw):
        calls.append(a)
        return _run(json.dumps({"version": "6.8.14.0"}))

    with patch.object(Manager, "run", side_effect=fake_run):
        assert mgr.version() == "6.8.14.0"
        assert mgr.version() == "6.8.14.0"
    assert len(calls) == 1


def test_setting_set_requires_pairs(tmp_path: Path):
    with pytest.raises(BadArgument):
        _manager(tmp_path).setting_set(0, {})


# ---------------------------------------------------------------------------
# input escaping
# ---------------------------------------------------------------------------
def test_input_text_escaping():
    from mumu_mcp.adb import Adb

    sent = {}

    class FakeAdb(Adb):
        def shell(self, vmindex, command, **kw):
            sent["cmd"] = command
            return ""

    fake = FakeAdb(binary=Path("adb.exe"), manager=None)  # type: ignore[arg-type]
    fake.input_text(0, "a b&c<d>")
    assert "%s" in sent["cmd"]
    assert "\\&" in sent["cmd"]
    assert sent["cmd"].startswith("input text '")


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
def test_presets_inside_limits():
    for name, (w, h, dpi) in C.RESOLUTION_PRESETS.items():
        lo, hi = C.RESOLUTION_LIMITS["width"]
        assert lo <= w <= hi, name
        lo, hi = C.RESOLUTION_LIMITS["height"]
        assert lo <= h <= hi, name
        lo, hi = C.RESOLUTION_LIMITS["dpi"]
        assert lo <= dpi <= hi, name


def test_keycodes_include_basics():
    for key in ("home", "back", "enter", "volume_up"):
        assert key in C.KEYCODES


def test_toolbar_funcs_match_mumu_help():
    # These are the names `control tool func --name` actually accepts.
    assert "screenshot" in C.TOOLBAR_FUNCS
    assert "shake" in C.TOOLBAR_FUNCS


# ---------------------------------------------------------------------------
# UI dump parsing
# ---------------------------------------------------------------------------
def test_parse_ui_extracts_center():
    from mumu_mcp.server import _parse_ui

    xml = (
        '<hierarchy rotation="0"><node class="android.widget.Button" text="OK" '
        'resource-id="com.x:id/ok" clickable="true" enabled="true" '
        'bounds="[100,200][300,400]" content-desc="" /></hierarchy>'
    )
    nodes = _parse_ui(xml)
    assert len(nodes) == 1
    node = nodes[0]
    assert node["text"] == "OK"
    assert node["class"] == "Button"
    assert node["clickable"] is True
    assert node["center"] == [200, 300]


def test_parse_ui_handles_missing_bounds():
    from mumu_mcp.server import _parse_ui

    nodes = _parse_ui('<node text="x" class="a.B" bounds="" />')
    assert nodes[0].get("center") is None


# ---------------------------------------------------------------------------
# identity generation
# ---------------------------------------------------------------------------
def test_random_imei_is_luhn_valid():
    from mumu_mcp.server import _random_for

    imei = _random_for("imei")
    assert len(imei) == 15 and imei.isdigit()
    digits = [int(d) for d in imei]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    assert total % 10 == 0


def test_random_mac_is_locally_administered():
    from mumu_mcp.server import _random_for

    mac = _random_for("mac_address")
    assert mac.startswith("02:")
    assert len(mac.split(":")) == 6


# ---------------------------------------------------------------------------
# tool surface
# ---------------------------------------------------------------------------
def test_tool_names_are_prefixed_and_unique():
    from mumu_mcp.server import mcp

    tools = mcp._tool_manager._tools
    assert len(tools) >= 60
    assert all(name.startswith("mumu_") for name in tools)
    assert len(set(tools)) == len(tools)


def test_every_tool_has_a_docstring():
    from mumu_mcp.server import mcp

    for name, tool in mcp._tool_manager._tools.items():
        assert (tool.description or "").strip(), name


# ---------------------------------------------------------------------------
# integration (needs a running emulator)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1 to run")
def test_live_diagnose():
    from mumu_mcp.server import mumu_diagnose

    report = mumu_diagnose.__wrapped__()  # raw: no envelope
    assert report["channels"]["manager"] is True
    assert report["instances"], "no instance running"
    for entry in report["instances"].values():
        assert entry["state"]
        assert entry["endpoint"], "endpoint must resolve for a running instance"


@pytest.mark.skipif(os.environ.get("MUMU_LIVE") != "1", reason="set MUMU_LIVE=1 to run")
def test_live_screen_and_ui():
    from mumu_mcp.server import mumu_screen_size, mumu_ui_dump

    size = mumu_screen_size.__wrapped__(0)
    assert size["width"] > 0 and size["height"] > 0
    dump = mumu_ui_dump.__wrapped__(0, simplify=True)
    assert dump["count"] > 0
