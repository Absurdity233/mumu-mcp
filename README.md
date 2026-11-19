# mumu-mcp

An [MCP](https://modelcontextprotocol.io) server for **Netease MuMu Player** — 110 tools over
the `MuMuManager` CLI and ADB, with a kernel module that removes the emulator from the
surface a detector reads.

MuMu exposes two things a program can drive: the `MuMuManager` CLI (lifecycle, settings, app
management, a guest shell) and ADB (input, screenshots, files, logcat). This server wraps
both, resolves the adb endpoint per instance, and turns every failure into a value a model
can branch on instead of a string it has to guess about.

**Status on this image** — `mumu_stealth_scan` reports **14/14 clean, 0 unfixable**:

```
uname -r / -m      6.1.75-android14-11-g4d9e0d1b2c3f / x86_64
/proc/version      builder@localhost
cpuinfo            GenuineIntel Celeron N5105, no hypervisor flag
DMI                Samsung / SM-S9210
device tree        model + compatible + 8 properties
GPU                /dev/kgsl-3d0 (real char device) + Adreno640v2
DRM                card0, renderD128
disks              SAMSUNG / KLUDG4UHGC-B0E1
/sys/module        106 entries, phone drivers complete (12 attrs + 21 sections)
virtio             absent from /sys/bus, /sys/devices, /sys/module
root               no su, /data/adb 700, adbd not root
```

---

## Why this shape

MuMu 6.x (Android 15, the NX engine) moved the emulator off the legacy NAT loopback port onto
a bridged LAN interface, and renamed several settings keys. A server that assumes
`127.0.0.1:16384` and `vm_cpu` still looks healthy — it lists tools, it returns JSON — while
the input, screenshot, logcat and file tools are dead. The failure mode is *empty output*,
not an error, which is the worst possible thing to hand an agent.

Two design rules follow:

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
  "version": "6.8.14.0",
  "channels": { "manager": true, "adb": true },
  "instances": {
    "0": {
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

`hints` is where the diagnosis lives — it names the one thing to fix (`no instances:
mumu_create …`, `index 0 has no adb: enable ADB debug …`).

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

**Cursor / any stdio client**: same shape — `command` is the `mumu-mcp` entry point.

**HTTP transport** (shared box, or a non-Node client):

```bash
mumu-mcp --transport streamable-http
```

## Configuration

| env | flag | meaning |
|---|---|---|
| `MUMU_HOME` | `--mumu-home` | dir containing `MuMuManager.exe`, or the binary path. Autodetected across `C:`–`H:` otherwise |
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

### Everyday

| tool | purpose |
|---|---|
| `mumu_diagnose` | install, version, instances, channels, adb endpoints — run this first |
| `mumu_list` | instances with state, Android version, PID, adb port |
| `mumu_snapshot` | foreground app, screen, battery, adbd uid, proxy, storage in one call |
| `mumu_start` / `mumu_stop` / `mumu_reboot` | lifecycle |
| `mumu_create` / `mumu_clone` / `mumu_delete` / `mumu_rename` | instance management |
| `mumu_info` / `mumu_settings_get` / `mumu_setting_set` | settings, with writability checked |

### Apps

`mumu_app_list` · `mumu_app_info` · `mumu_app_install` · `mumu_app_batch_install` ·
`mumu_app_uninstall` · `mumu_app_launch` · `mumu_app_close` · `mumu_app_clear_data` ·
`mumu_app_fresh_start` · `mumu_app_monkey`

`mumu_app_fresh_start` is stop + wipe + launch in one call — the reproducible first-run state.

### Input — two channels

| channel | tools | notes |
|---|---|---|
| `input` | `mumu_tap` · `mumu_swipe` · `mumu_text` · `mumu_key` · `mumu_long_press` | via `adb shell input` |
| kernel | `mumu_raw_sequence` · `mumu_raw_event` · `mumu_pinch` · `mumu_two_finger_drag` | written to the MT protocol-B touchscreen node |

**`mumu_raw_sequence` is the one that works, and the distinction is not cosmetic.** A tap is
down → SYN → up → SYN within microseconds. Sent as separate `adb shell` calls that becomes a
~500 ms slow drag and the app ignores it. Verified both ways: batched, tapping the launcher's
game-centre icon navigates into `com.mumu.store/.MainActivity`; unbatched, nothing happens.

**Two-finger gestures do not reach the app on this MuMu build.** Pinch and two-finger drag
were driven with a correct MT-B sequence at coordinates where a single-finger swipe scrolls
the same list; nothing moved. MuMu's host input bridge forwards single-pointer events and
drops multi-slot ones. The helpers are kept — they emit a well-formed gesture and will work
on a build whose bridge forwards multi-touch — but they are documented as platform-limited
rather than shipped as if they worked.

### Reading the screen

| tool | purpose |
|---|---|
| `mumu_ui_dump` | view hierarchy with clickable bounds; each node carries a `center` for `mumu_tap` |
| `mumu_wait_for` | block until text appears, return the matching node |
| `mumu_wait_activity` | block until the foreground package matches |
| `mumu_screenshot` | PNG to the host, returns the path (≈60 ms at 1440×2560) |
| `mumu_logcat` | filtered log tail |

### Flows — scripting a UI sequence

```python
mumu_flow(steps=[
    {"action": "launch", "package": "com.example"},
    {"action": "wait_for", "text": "登录"},
    {"action": "tap", "text": "登录"},
    {"action": "assert_text", "text": "欢迎"},
])
```

13 actions: `tap` `swipe` `long_press` `text` `key` `wait_for` `wait_gone` `wait_activity`
`assert_text` `launch` `sleep` `shell` `screenshot`. Locators poll the parsed view hierarchy,
so they are immune to animation and OCR noise. Stops at the first failure and reports
`failed_step` plus what it saw.

### Network · shell · files

`mumu_shell` · `mumu_push` · `mumu_pull` · `mumu_proxy` · `mumu_set_proxy` ·
`mumu_network_info` · `mumu_public_ip` · `mumu_hosts` · `mumu_add_hosts`

### Root

MuMu ships **no `su` binary** — `adb root` starts adbd as uid 0 directly, which is the only
way in. `mumu_root_adb` toggles it without a reboot.

| tool | purpose |
|---|---|
| `mumu_root_adb` | `adb root` / `adb unroot`; returns the verified uid and SELinux context |
| `mumu_root_status` | what root actually looks like: adbd uid, SELinux, `/system` writability, whether a `su` exists |
| `mumu_root_exec` | asserts uid 0, then runs — a non-root run fails loudly instead of half-working |
| `mumu_root_system` | make `/system` writable; on MuMu this stacks an overlayfs backed by a host-side diff image |
| `mumu_root_push_system` / `mumu_root_pull_system` | copy in and out of `/system`, and out of other apps' `/data/data` |
| `mumu_root_restore_system` | delete the overlay diff image — the clean undo |

**`/system` is never modified.** The block device stays read-only; `mumu_root_system` flips
`system_disk_readonly`, which makes MuMu stack an overlayfs over it. Writes persist across
reboots while the stock image is untouched, and removing the diff image restores stock
exactly.

### Device state

`mumu_foreground` · `mumu_battery` · `mumu_set_battery` · `mumu_volume` · `mumu_rotate` ·
`mumu_screen_size` · `mumu_set_resolution` · `mumu_wifi` · `mumu_airplane_mode` ·
`mumu_gps_set` · `mumu_shake` · `mumu_set_cpu` · `mumu_set_ram` · `mumu_set_fps` ·
`mumu_set_device` · `mumu_spoof_identity` · `mumu_processes` · `mumu_storage` ·
`mumu_window` · `mumu_toolbar` · `mumu_prop_get` · `mumu_prop_set` · `mumu_sort`

---

## Hiding the emulator

Three layers, in order of increasing strength and decreasing reversibility. Each one records
what it could not close rather than claiming a clean result.

### 1. Properties and adbd — `mumu_stealth_hide`

| surface | fix |
|---|---|
| `ro.hardware`, `ro.product.board`, `ro.boot.verifiedbootstate`, … (14 keys) | rewritten to real-SM-S9210 values |
| `nemu*` properties (`init.svc.nemuhotd`, `nemud.fps_limit`, …) | **deleted**, not blanked |
| adbd running as root | `adb unroot` → `uid=2000(shell)` |

`ro.*` cannot be changed with `setprop` after init has read it — KernelSU's `resetprop -n`
writes the property area directly, which is the only mechanism that works.

Two behaviours worth knowing, both found by testing:

- **Deleting beats blanking.** `resetprop -n key ''` leaves the key in the property area, so
  `getprop` still prints it and a `grep nemu` still hits. `resetprop -d key` removes the
  entry. The first version blanked and reported success; the re-scan gave it away.
- **`setenforce 1` hangs this image.** It blocks past a 30 s timeout and leaves adbd
  `offline` until the instance is restarted — init has no enforcing policy to load. So
  SELinux stays Permissive and KernelSU's `selinux_hide` sanitises `/sys/fs/selinux` for app
  UIDs instead, which is the only mechanism that closes that tell without taking the
  instance down.

**MuMu writes new properties at runtime.** Switching apps produces
`nemud.current.focused_display`, `nemud.current.focused_native_display` and
`nemud.current.focused_uid`. Hiding is not a one-time step — re-scan after using the
instance.

### 2. Kernel module — `mumu_kmod_load`

`uname` reads kernel data, not a file, so neither `resetprop` nor a bind mount reaches it.
The fix is a module that rewrites that data in place, so nothing is mounted and
`/proc/self/mounts` stays clean.

| target | before | after |
|---|---|---|
| `uname -r` | `6.1.90-perf+` | `6.1.75-android14-11-g4d9e0d1b2c3f` |
| `uname -m` | `aarch64` | `x86_64` |
| cpuinfo | `AuthenticAMD` · Ryzen 9 9955HX · `hypervisor` | `GenuineIntel` · Celeron N5105 · no VM flags |
| DMI | `innotek GmbH` · `NiohBox` | `Samsung` · `SM-S9210` |
| clocksource | `kvm-clock` | `arch_sys_counter` |
| `/proc/version` | `6.1.90-perf+ (build-user@build-host)` | `6.1.75-android14-11 … (builder@localhost)` |
| `/proc/cmdline` | `kvm-clock` in it | phone `cmdline` |
| `/sys/bus/virtio` | present, with `ath10k` and `virtio_scsi` | removed |
| `virtio0/1/2` under `/sys/devices` | visible | renamed to `usb`/`wifi`/`storage` |
| `/sys/module` | 120 entries | 86, then +20 phone drivers (106 total) |
| `/proc/modules` | lists the module | does not |
| `/proc/mounts` | 15 override lines | none |

It also **creates what the image lacked but a handset has**:

| node | how |
|---|---|
| `/proc/device-tree` → `/sys/firmware/devicetree/base` | `proc_symlink` |
| 8 device-tree properties (`model`, `compatible`, `serial-number`, `qcom,msm-id`, …) | `kernfs_create_dir_ns` + `sysfs_add_bin_file_mode_ns` |
| `/sys/class/kgsl/kgsl-3d0` + `gpu_model: Adreno640v2` | kernfs, walked from `/sys` |
| `/dev/kgsl-3d0`, `/dev/kgsl-2d0` | real `alloc_chrdev_region` + `cdev_add`, `mknod` in `service.sh` |
| `/sys/class/drm/{card0,renderD128}` | kernfs |
| 20 Qualcomm `/sys/module` entries | kernfs, each with the full 12-attribute set and 21 `sections/` |
| disk `vendor`/`model` | rewritten in the SCSI INQUIRY buffer |

#### Why bind mounts had to go

The earlier version covered those files with `mount --bind`. The values were right and it
still gave itself away:

```
/dev/block/sdc3 /proc/cpuinfo ext4 rw,seclabel,noatime 0 0
```

On real hardware `/proc/cpuinfo` comes from procfs. An ext4 partition as the mount source is
a stronger signal than any value being hidden, and every process can read `/proc/mounts`.

#### Why the syscall table cannot work

Stated by the 6.1.90 source itself (`arch/x86/entry/syscall_64.c`):

```c
/*
 * The sys_call_table[] is no longer used for system calls, but
 * kernel/trace/trace_syscalls.c still wants to know the system call address.
 */
const sys_call_ptr_t sys_call_table[] = { ... };

long x64_sys_call(const struct pt_regs *regs, unsigned int nr)
{
        switch (nr) { case 63: return __x64_sys_newuname(regs); ... }
}
```

Dispatch is that `switch`. Writing `sys_call_table[63]` changes a table nothing reads — which
is why a module that poisoned all 55 slots of `struct module` still loaded cleanly and did
nothing. And `set_memory_rw`, needed to make the table writable, takes `cpa_lock` and flushes
the TLB; calling it from init wedged the guest hard enough that adb and the manager channel
both stopped answering.

#### The architecture correction

`uname -m` used to report `aarch64`, matching the stock kernel. That was backwards — every
userspace binary on the image is EM_X86_64 (`/system/bin/sh`, `toybox`, `app_process64`,
`linker64`, `libc.so`), so one read of an ELF header beat every faked value. Reporting
`x86_64` makes uname, cpuinfo and the binaries agree instead of contradicting.

#### Hiding the module

`/proc/modules` walks the global `modules` list; there is no "hidden" flag, so the module
unlinks itself from that list and from sysfs. The cost is real: after the unlink it cannot be
unloaded from userspace and stays resident until reboot — which is why that step runs last.

Building it is the hard part — the module must match `uname -r` exactly
(`deprecated/android14-6.1-2024-08`, commit `ab81bbc79`), and on Windows the kernel's own
build plumbing needs several replacements. [`native/km/README.md`](native/km/README.md)
records every step, the measured struct offsets, three deadlocks and what each taught, and
the two bugs that cost the most time: a `fixdep` stub that wrote to `argv[2]`, truncating
every object file to zero bytes, and `scsi_host_lookup` taking an `unsigned short` rather
than a name.

#### What is still different

`/sys/module/ufs_qcom` is a directory with the right attributes, not a driver.
`/sys/module/ufs_qcom/sections/.text` reads `0x0000000000000000`. On this image a real module
reads the same, because `kptr_restrict=2` hides the addresses — so matching the image is met.
On a build with `kptr_restrict=0` a real module would show real addresses and this would not.

### 3. Persistent — `module/`

The two layers above are runtime state: `resetprop` writes the property area, and a loaded
module dies on reboot. `module/` packages both as a KernelSU module so they reapply at boot
with no PC attached:

```bash
cd module && zip -r ../dist/mumu-stealth.zip *
adb push ../dist/mumu-stealth.zip /data/local/tmp/
adb shell ksud module install /data/local/tmp/mumu-stealth.zip
```

Three things had to be right, each found the hard way:

| | |
|---|---|
| `kernel_umount` must be **off before** the mounts are made | KernelSU unmounts module mounts "when not needed". Turning the feature off afterwards is too late |
| `selinux_hide` closes the Permissive tell | It sanitises `/sys/fs/selinux` **for app UIDs**; root still reads the truth |
| the script must be idempotent per boot | KernelSU can invoke `service.sh` once per stage; without a `boot_id` guard every step is redone |

---

## Per-process tracer — `uname(2)` without a module

`native/uname_patch` attaches with ptrace, breaks on syscall exit, and rewrites the
`utsname` in the tracee. It needs no module, so it works where loading one is not possible.
It sits below libc, so it covers a target that calls the syscall directly, ships its own
loader, or clears `LD_PRELOAD`.

```python
mumu_uname_selftest()        # prove it works, with a built-in target
mumu_uname_patch(pid=...)    # attach to a real app
mumu_uname_verify(pid=...)   # read the struct back out of /proc/<pid>/mem
mumu_uname_detach(pid=...)   # stop (kills the tracee)
mumu_uname_daemon_start()    # attach to every matching process, keep them traced
```

Two costs, stated rather than discovered later: every traced thread pays a ptrace stop per
syscall, and `PTRACE_O_EXITKILL` means killing the tracer kills the tracee — so a crashed
tracer cannot leave a game permanently stopped.

The binaries are freestanding x86-64 assembly: this host has clang but no Android sysroot,
and the device ships no headers. [`native/README.md`](native/README.md) records the three
bugs that took: wait-status decoding (`0x2343`, not `0x7f`), `PTRACE_POKEDATA` argument
order, and 65-byte `utsname` field padding.

---

## Design notes

Five things bite anyone writing this against MuMu 6.x. Each is handled in a specific place,
on purpose:

1. **adb endpoint discovery** — NAT loopback first, guest `wlan0` second (`mumu_mcp/adb.py`).
   Hard-coding `16384 + idx*32` costs you every adb-backed tool on the current release,
   silently.
2. **A non-zero exit is not a dead connection.** `uiautomator dump` exits 1 *after* printing
   `UI hierchary dumped to: …`; `pm clear` exits non-zero on a missing package. Only real
   transport markers (`device offline`, `cannot connect`, …) invalidate the cached endpoint,
   otherwise the UI dump tool would tear down its own connection every call. A genuine
   transport failure is retried once with the cache dropped, because a restarted instance
   leaves a dead socket registered with adb.
3. **Encoding.** MuMu speaks UTF-8. Decoding with the host codepage (what
   `subprocess(text=True)` does on a zh-CN Windows) turns every `app_name` into `���˽Ȩ`.
   Bytes are decoded explicitly, UTF-8 first, GB18030 as the guest-output fallback.
4. **Writable keys.** `vm_cpu` / `vm_mem` are build-managed on 6.x and reject writes; the
   `.custom` performance keys are the ones that accept them. Both are exposed, and the
   rejection is a typed `key_not_writable` rather than a silent no-op.
5. **Shell escaping.** `input text` needs `%s` for spaces and backslashes for
   `& < > ' " ( ) | ;`. Escape only spaces and half of a login form breaks.

Four interface decisions aimed at the caller being a model:

- **`ok`/`error` envelope** with a `retryable` hint and a stable code — branch on data, not
  prose.
- **`mumu_diagnose`** — one call that explains the environment instead of a traceback.
- **`mumu_ui_dump` / `mumu_wait_for`** — coordinate-free driving.
- **Preflight validation** — ranges, presets, keycode names and file paths are checked
  before a subprocess is spawned, so a bad argument costs 0 ms and returns a useful message.

## Development

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q                       # hermetic: 214
$env:MUMU_LIVE="1"; .venv\Scripts\python -m pytest -q   # + live emulator: 233
.venv\Scripts\ruff check mumu_mcp tests
```

Layout:

```
mumu_mcp/
  manager.py     MuMuManager wrapper: argv, JSON, typed errors, install discovery
  adb.py         adb transport, endpoint resolution, input escaping
  errors.py      error taxonomy, errcode -> exception map
  constants.py   keycodes, toolbar names, resolution presets, limits
  root.py        root state, adb root, system overlay, diff-image restore
  device.py      foreground, launch, battery, proxy, hosts, processes
  flow.py        scripted step lists, locator resolution, failure reporting
  gesture.py     kernel input channel, MT-protocol-B frames, raw batching
  stealth.py     surface scan, property/root hiding, grading, bind-mount layer
  fakeprofile.py the ARM/DMI/clocksource content the deep layer installs
  server.py      FastMCP tool definitions (the only file that knows about MCP)
module/          KernelSU module: reapplies everything on boot
native/          ptrace uname tracer (freestanding asm)
  km/            the kernel module, and how it builds
tests/
```

`manager.py` and `adb.py` are testable without an emulator and without MCP in the loop, which
is why the split exists.

## Notes and limits

- Windows-only in practice: MuMu ships `MuMuManager.exe` and an `adb.exe` next to it.
- `mumu_window` handles show/hide; arbitrary window geometry goes through `mumu_sort` or
  MuMu's own UI.
- Screenshots use `screencap` + `adb pull` (≈60 ms at 1440×2560). `exec-out screencap -p` is
  faster but corrupts on some Windows adb builds, so it is not used.
- No tool requires root; `mumu_shell` and `mumu_prop_set` fail with the guest's own error if
  the instance has root disabled.
- **`mumu_stealth_hide` drops adbd to `shell`.** A scan run right after will report
  `selinux: Permissive, selinux_hide is off` — a false positive, because `ksud` cannot be
  queried without root. Call `mumu_root_adb(enabled=True)` before re-scanning.

## License

MIT
