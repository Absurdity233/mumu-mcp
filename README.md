# mumu-mcp

An [MCP](https://modelcontextprotocol.io) server for **Netease MuMu Player** — the Android emulator
used for mobile game automation and app testing. 66 tools, two independent channels, structured
errors.

MuMu exposes two things a program can drive: the `MuMuManager` CLI (lifecycle, settings, app
management, a guest shell) and ADB (input, screenshots, files, logcat). This server wraps both,
resolves the adb endpoint per instance, and turns every failure into a value the model can branch
on instead of a string it has to guess about.

---

## Why this shape

MuMu 6.x (Android 15, the NX engine) moved the emulator off the legacy NAT loopback port onto a
bridged LAN interface, and renamed several settings keys. A server that assumes `127.0.0.1:16384`
and `vm_cpu` still looks healthy — it lists tools, it returns JSON — while the input, screenshot,
logcat and file tools are dead. The failure mode is *empty output*, not an error, which is the
worst possible thing to hand an agent.

So the two design rules here:

| concern | approach |
|---|---|
| where is adb | probe loopback `16384 + idx*32`; if closed, read the guest `wlan0` IP through the manager shell channel and use `:5555` |
| how does a failure look | `{"ok": false, "error": "instance_not_ready", "retryable": true}` — a stable code plus a retry hint |

---

## Install

```bash
git clone https://github.com/Absurdity233/mumu-mcp
cd mumu-mcp
python -m venv .venv
.venv\Scripts\pip install -e .          # Windows
# .venv/bin/pip install -e .            # Linux/macOS, for a remote emulator host
```

Requires Python ≥ 3.10 and a MuMu Player install (6.x or 12.x) on the same machine.

Verify it can see your emulator before wiring it into a client:

```bash
mumu-mcp --diagnose
```

```json
{
  "manager": "D:\\Program Files\\Netease\\MuMu\\nx_main\\mumu-cli.exe",
  "install_dir": "D:\\Program Files\\Netease\\MuMu\\nx_main",
  "version": "6.8.14.0",
  "channels": { "manager": true, "adb": true },
  "instances": {
    "0": {
      "name": "MuMu安卓设备",
      "state": "start_finished",
      "android": "15.0",
      "running": true,
      "endpoint": "192.168.2.49:5555",
      "adb": true
    }
  },
  "hints": []
}
```

`hints` is where the diagnosis actually lives — it names the one thing to fix
(`no instances: mumu_create …`, `index 0 has no adb: enable ADB debug …`).

## Client config

**Claude Desktop / Claude Code** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mumu": {
      "command": "F:\\deepseek\\mumu-mcp\\.venv\\Scripts\\mumu-mcp.exe",
      "args": ["--mumu-home", "D:\\Program Files\\Netease\\MuMu\\nx_main"]
    }
  }
}
```

**Cursor / any stdio client**: same shape — `command` is the `mumu-mcp` entry point, `args` optional.

**HTTP transport** (for a shared box or a non-Node client):

```bash
mumu-mcp --transport streamable-http
```

## Configuration

| env | flag | meaning |
|---|---|---|
| `MUMU_HOME` | `--mumu-home` | dir containing `MuMuManager.exe`, or the binary path. Autodetected across `C:`–`H:` otherwise. |
| `MUMU_ADB` | `--adb` | force an endpoint (`host:port`) for every instance |
| `MUMU_ADB_<n>` | — | force the endpoint for index `<n>` only |
| `MUMU_LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` (default `INFO`, on stderr) |

---

## The tool surface

Every tool returns `{"ok": true, …}` or `{"ok": false, "error": "<code>", …}`. Error codes:
`install_not_found`, `instance_not_found`, `instance_not_ready`, `key_not_writable`,
`adb_unavailable`, `manager_failed`, `timeout`, `bad_argument`, `internal_error`.
`retryable` says whether a retry can plausibly succeed — `instance_not_ready` yes,
`bad_argument` no.

### Start here

| tool | purpose |
|---|---|
| `mumu_diagnose` | install, version, instances, resolved endpoints, which channels are live, what to fix |
| `mumu_list` | every instance: state, Android version, PID, adb port |
| `mumu_info` | one instance in full |

### Lifecycle

`mumu_create` · `mumu_clone` · `mumu_delete` · `mumu_rename` · `mumu_start` · `mumu_stop`
· `mumu_reboot` · `mumu_boot_ready` · `mumu_window` · `mumu_sort`

`mumu_boot_ready` polls `sys.boot_completed` through the **manager** shell channel, so it works
before adb is up — no racing the emulator with `sleep`.

### Configuration

`mumu_settings_get` · `mumu_setting_set` · `mumu_set_resolution` · `mumu_set_cpu` · `mumu_set_ram`
· `mumu_set_fps` · `mumu_set_device` · `mumu_set_root` · `mumu_spoof_identity`

`mumu_set_resolution` takes a named preset (`phone`, `phone-hd+`, `tablet`, `widescreen-2k`,
`widescreen-4k`, `square`, …) or explicit `width`/`height`/`dpi`, and validates against the real
limits (380–4096 px, 10–960 dpi).

`mumu_spoof_identity(imei="random")` generates a **Luhn-valid** IMEI and a locally-administered
MAC (`02:…`) — apps do check the check digit.

### Apps

`mumu_app_list` · `mumu_app_info` · `mumu_app_install` · `mumu_app_batch_install` ·
`mumu_app_uninstall` · `mumu_app_launch` · `mumu_app_close` · `mumu_app_clear_data` ·
`mumu_app_fresh_start` · `mumu_app_monkey`

`mumu_app_fresh_start` = force-stop → `pm clear` → launch: the reproducible first-run state.
`mumu_app_monkey` returns `clean: bool` plus the crash lines, not 5 000 lines of monkey chatter.

### Input — two channels

| adb channel (finer) | manager channel (no adb needed) |
|---|---|
| `mumu_tap` `mumu_swipe` `mumu_long_press` `mumu_text` | `mumu_tap_sync` `mumu_swipe_sync` `mumu_text_sync` |
| `mumu_key` `mumu_press_home` `mumu_press_back` `mumu_press_recent` | `mumu_toolbar(name=…)` |
| `mumu_scroll` `mumu_rotate` `mumu_volume` | `mumu_gps_set` `mumu_shake` |

The `_sync` variants go through `MuMuManager control tool cmd`, which keeps working when adb is
disabled — useful on locked-down images. `mumu_gps_set` and `mumu_shake` call MuMu's own toolbar
functions, so they move the real emulated location and fire a real accelerometer event rather than
broadcasting an intent and hoping something listens.

`mumu_text_sync` handles CJK input, which `adb shell input text` cannot.

### Reading the screen

| tool | purpose |
|---|---|
| `mumu_ui_dump` | view hierarchy with `bounds` **and a ready-to-click `center`** |
| `mumu_wait_for` | block until text appears, return the matching node |
| `mumu_screenshot` | PNG on the host, returns the path + guest size |
| `mumu_screenshot_base64` | inline base64 (≈2.6 MB — prefer the file variant) |
| `mumu_screen_size` | resolution + density |

`mumu_ui_dump` is the point of this section. Tapping pixel coordinates guessed from a screenshot is
brittle; the dump hands you `"center": [720, 1600]` for the node whose `text` is `"登录"`, and
`mumu_wait_for(text="登录")` gets you there without a single `sleep`.

### Network · shell · files

`mumu_network_info` · `mumu_public_ip` · `mumu_airplane_mode` · `mumu_wifi` ·
`mumu_shell` · `mumu_push` · `mumu_pull` · `mumu_logcat` · `mumu_prop_get` · `mumu_prop_set` ·
`mumu_export` · `mumu_import`

---

## Design notes

Five things bite anyone writing this against MuMu 6.x. Each is handled in a specific place, on
purpose:

1. **adb endpoint discovery** — NAT loopback first, guest `wlan0` second (`mumu_mcp/adb.py`). Hard
   coding `16384 + idx*32` costs you every adb-backed tool on the current release, silently.
2. **A non-zero exit is not a dead connection.** `uiautomator dump` exits 1 *after* printing
   `UI hierchary dumped to: …`; `pm clear` exits non-zero on a missing package. Only real transport
   markers (`device offline`, `cannot connect`, …) invalidate the cached endpoint, otherwise the UI
   dump tool would tear down its own connection every single call.
3. **Encoding.** MuMu speaks UTF-8. Decoding with the host codepage (what `subprocess(text=True)`
   does on a zh-CN Windows) turns every `app_name` into `���˽Ȩ`. Bytes are decoded explicitly,
   UTF-8 first, GB18030 as the guest-output fallback.
4. **Writable keys.** `vm_cpu` / `vm_mem` are build-managed on 6.x and reject writes; the
   `.custom` performance keys are the ones that accept them. Both are exposed, and the rejection is
   a typed `key_not_writable` rather than a silent no-op.
5. **Shell escaping.** `input text` needs `%s` for spaces and backslashes for `& < > ' " ( ) | ;`.
   Escape only spaces and half of a login form breaks.

And four interface decisions aimed at the caller being a model:

- **`ok`/`error` envelope** with a `retryable` hint and a stable code — branch on data, not prose.
- **`mumu_diagnose`** — one call that explains the environment instead of a traceback.
- **`mumu_ui_dump` / `mumu_wait_for`** — coordinate-free driving.
- **Preflight validation** — ranges, presets, keycode names and file paths are checked before a
  subprocess is spawned, so a bad argument costs 0 ms and returns a useful message.

## Development

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q                       # hermetic: 35 tests
$env:MUMU_LIVE="1"; .venv\Scripts\python -m pytest -q   # + live emulator: 37
.venv\Scripts\ruff check mumu_mcp tests
```

Layout:

```
mumu_mcp/
  manager.py     MuMuManager wrapper: argv, JSON, typed errors, install discovery
  adb.py         adb transport + endpoint resolution + input escaping
  errors.py      error taxonomy, errcode -> exception map
  constants.py   keycodes, toolbar names, resolution presets, limits
  server.py      FastMCP tool definitions (the only file that knows about MCP)
tests/test_units.py
```

The split exists so `manager.py` and `adb.py` can be tested without an emulator and without MCP in
the loop.

## Notes and limits

- Windows-only in practice: MuMu ships `MuMuManager.exe` and an `adb.exe` next to it.
- `mumu_window` handles show/hide; arbitrary window geometry goes through `mumu_sort` or MuMu's own
  UI.
- Screenshots are taken with `screencap` + `adb pull` (≈60 ms at 1440×2560). `exec-out screencap -p`
  is faster but corrupts on some Windows adb builds, so it is not used.
- No tool needs root; `mumu_shell` and `mumu_prop_set` will simply fail with the guest's own error
  if the instance has root disabled.

## License

MIT
