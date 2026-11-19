# mumu-mcp

An [MCP](https://modelcontextprotocol.io) server for **Netease MuMu Player** — the Android emulator
used for mobile game automation and app testing. 110 tools, root, environment hiding and kernel-level uname spoofing, structured errors.

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

### Root

| tool | what it does |
|---|---|
| `mumu_root_status` | uid, SELinux, `/system` writability and *how* it is writable, whether that survives reboot, whether `su` exists |
| `mumu_root_adb` | `adb root` / `adb unroot` — adbd as uid 0, no reboot, instance keeps running |
| `mumu_root_permission` | set `root_permission` + restart, so root comes up at boot |
| `mumu_root_system` | make `/system` writable via MuMu's overlay, or go back to the stock image |
| `mumu_root_restore_system` | delete the overlay diff image — the clean undo for the above |
| `mumu_root_push_system` | copy a file into `/system`, refusing before it half-succeeds |
| `mumu_root_pull_system` | copy out of `/data/data` and other paths the `shell` user cannot read |
| `mumu_root_exec` | shell with a uid assertion, so a non-root run fails loudly instead of quietly |
| `mumu_root_remount` | `mount -o rw,remount`, with the reason it cannot work here |

**What root actually looks like on MuMu** — measured on 6.8.x / Android 15, not assumed:

- **There is no `su` binary.** `root_permission=true` starts *adbd as root*
  (`uid=0`, context `u:r:su:s0`). So `mumu_shell` is already root and nothing needs
  `su -c`; conversely, anything that shells out to `su` fails. To drop privileges, use
  `run-as <package>`.
- **`/system` is read-only at the block level, writable in practice.** `/dev/block/sda6`
  is mounted `ro`, so `mount -o rw,remount /system` fails with `'/dev/block/sda6' is
  read-only`. With `system_disk_readonly=false`, MuMu stacks an overlayfs instead:

  ```
  overlay on /system type overlay (rw,
      lowerdir=/system, upperdir=/mnt/scratch/upperdir, workdir=/mnt/scratch/workdir)
  ```

  That upperdir is **not visible inside the guest** (`/mnt/scratch` is an empty tmpfs
  there). It lives on the host as `<vm>/system-diff.vdi` and grows as you write — 9.4 MB
  → 11.5 MB over a few files — so writes **persist across reboots** while the stock image
  stays untouched. `mumu_root_restore_system` deletes that diff image: because the lower
  layer was never modified, that is a complete restore, not a rollback of half-applied
  changes.
- **`service.adb.root=1` is per-boot.** A reboot brings adbd back as `shell` even with
  `root_permission=true`; that setting decides the *next* boot. `mumu_root_adb` fixes the
  current one, `mumu_root_permission` fixes all future ones.

SELinux is `Permissive` on MuMu's image, so policy denials are not the obstacle they
would be on a stock device.

### Device state

| tool | purpose |
|---|---|
| `mumu_snapshot` | foreground app, screen, battery, adbd uid, proxy, storage — one call, one moment |
| `mumu_foreground` | which package/activity is in front |
| `mumu_wait_activity` | block until the foreground package (or regex) matches |
| `mumu_launch` | `am start` with `-W`, returns `total_ms` and cold/warm |
| `mumu_battery` · `mumu_set_battery` | read, override, or `reset` the reported battery |
| `mumu_proxy` · `mumu_set_proxy` | the global HTTP proxy apps actually read |
| `mumu_hosts` · `mumu_add_hosts` | pin DNS by editing `/system/etc/hosts` through the root overlay |
| `mumu_storage` · `mumu_processes` | free space; top processes with load average |

`mumu_snapshot` exists because five separate calls are both slower and internally
inconsistent: the foreground app and the screen size should describe the same moment. If one
probe fails it lands in `errors` instead of sinking the call.

`mumu_wait_activity` is the honest "did my tap navigate" check — it reads what Android reports,
not what a screenshot looks like mid-animation.

### Flows — scripting a UI sequence

`mumu_flow` runs a list of steps in one call. This is the difference between an agent doing
5–15 round trips per screen and doing one:

```json
[
  {"action": "wait_activity", "target": "com.example.app", "timeout_s": 20},
  {"action": "tap", "text": "登录"},
  {"action": "text", "text": "user@example.com"},
  {"action": "tap", "id": "password"},
  {"action": "text", "text": "hunter2"},
  {"action": "tap", "text": "提交"},
  {"action": "wait_for", "text": "欢迎回来", "timeout_s": 30},
  {"action": "screenshot"}
]
```

| action | fields |
|---|---|
| `tap` | `x`/`y`, or a locator |
| `swipe` | `x1 y1 x2 y2`, optional `duration_ms` |
| `long_press` | locator or `x`/`y`, optional `duration_ms` |
| `text` | `text` |
| `key` | name or numeric keycode |
| `wait_for` / `wait_gone` | locator, optional `timeout_s` |
| `wait_activity` | `target` (package or regex) |
| `assert_text` | locator — fails the flow if absent |
| `launch` | `package` (+ `activity`) or `component` |
| `sleep` | `ms`, capped at 30 s |
| `shell` | `command` |
| `screenshot` | optional `path` |

A **locator** is `text` / `desc` / `id`, substring by default, with `exact` and `index` to
disambiguate. It polls the parsed view hierarchy, so it is immune to animation and OCR noise.

A failing flow stops at that step and tells you which one, plus what was actually on screen:

```json
{
  "completed": false,
  "failed_step": 1,
  "error": "assertion failed: 'zzz' is not on screen",
  "detail": {"node_count": 36, "sample": ["广告轮播", "游戏中心", "每日新发现"]}
}
```

That `sample` is the point: a broken flow is diagnosable without re-running it.

### Environment surface — scan and hide

For the case where something on the guest actively looks for emulator or root traces.

| tool | purpose |
|---|---|
| `mumu_stealth_scan` | enumerate the leak surface and grade every item; changes nothing |
| `mumu_stealth_hide` | close the reversible part, re-scan, report what is left |
| `mumu_stealth_deep` | bind-mount over the files a professional check reads |
| `mumu_stealth_deep_restore` | unmount every override and delete the staged fakes |
| `mumu_stealth_features` | list KernelSU's kernel-side switches |
| `mumu_stealth_set_feature` | toggle one (`selinux_hide`, `kernel_umount`, …) and verify |

Every finding is graded **`clean` / `leak` / `unfixable`**, and the headline `verdict` keys on the
unfixable ones — one structural tell is enough for an integrity check to fail, whatever else got
cleaned up. `mumu_stealth_hide(restore=True)` puts the original values back; a reboot does too.

**What actually closes, measured:**

| surface | fix |
|---|---|
| `ro.hardware`, `ro.product.board`, `ro.boot.verifiedbootstate`, … (7 keys) | rewritten to real-SM-S9210 values |
| **60** `nemu*` properties (`init.svc.nemuhotd`, `nemud.fps_limit`, …) | **deleted**, not blanked |
| adbd running as root | `adb unroot` → `uid=2000(shell)` |

`ro.*` cannot be changed with `setprop` after init has read it — KernelSU's `resetprop -n` writes the
property area directly, which is the only mechanism that works.

Two behaviours worth knowing, both found by testing rather than reading:

- **Deleting beats blanking.** `resetprop -n key ''` leaves the key in the property area, so
  `getprop` still prints it and a `grep nemu` still hits. `resetprop -d key` removes the entry.
  The first version blanked and reported success; the re-scan gave it away.
- **`setenforce 1` hangs this image.** It blocks past a 30 s timeout and leaves adbd `offline`
  until the instance is restarted — MuMu's init has no enforcing policy to load. So SELinux is
  graded `unfixable` and left alone, rather than "fixed" and breaking the instance.

**What is reported as `unfixable` instead of faked:**

| surface | why |
|---|---|
| `sensor_count: 1` (a real SM-S9210 reports 20+) | needs a faked sensors HAL, not a property |
| `uname=aarch64` vs `cpuinfo: AuthenticAMD, hypervisor` vs `__x64_sys_*` in kallsyms | closing it needs a uname/kallsyms hook, which is itself a signal |
| SELinux `Permissive` | see above — cannot be changed at runtime here |
| `/data/adb/ksu`, `/data/adb/magisk` | KernelSU needs them; a half-move is a *new* inconsistency |

The honest summary: properties and adbd privilege close completely; the kernel-level and
HAL-level tells do not, and the scan says so up front instead of promising a clean verdict it
cannot deliver.

### Deep layer — the files a real check opens

`mumu_stealth_hide` fixes what `getprop` exposes. A professional check does not stop there: it opens
specific files. Those are read-only, but **`mount --bind` changes what a read returns**, and unlike a
kernel hook a bind mount leaves no code patch to find. `mumu_stealth_deep` uses that.

| file | before | after |
|---|---|---|
| `/proc/cpuinfo` | `vendor_id: AuthenticAMD` under `uname -m = aarch64` | 8-core ARM profile (`CPU implementer: 0x41`, Cortex-X4/A720/A520) |
| `/sys/class/dmi/id/*` | `innotek GmbH` / `NiohBox` — **VirtualBox** | Samsung SM-S9210 (8 fields) |
| `/proc/kallsyms` | 840 `__x64_sys_*` entries | ARM symbol stub |
| `clocksource` | `kvm-clock` | `arch_sys_counter` |
| `/proc/sys/kernel/osrelease` | `6.1.90-perf+` | `6.1.75-android14-…` |

The fake profile is deliberately consistent with the property layer — `ro.product.model=SM-S9210`,
`ro.product.cpu.abilist` ARM, and a cpuinfo claiming `e1q` cores. A fake that contradicts another
property is a *new* tell, worse than the one it replaced.

Verified from the detector's side: reading those paths as the plain `shell` user (no root env) returns
the fake values. `mumu_stealth_deep_restore` unmounts all 14 and deletes the staged files; a reboot
also clears them.

**What this still cannot close** — stated in the tool result, not left for you to discover:

| tell | why |
|---|---|
| `uname(2)` → `uname -r` still `6.1.90-perf+` | a syscall, not a file. Closing it needs an `LD_PRELOAD` shim (per-process — useless against a game with its own loader) or a kernel hook, which is a stronger signal than the leak |
| `/data/adb/ksu`, `/data/adb/modules` | KernelSU needs them; a half-move is a new inconsistency |
| SELinux `Permissive` | closed for app UIDs by KernelSU's `selinux_hide`; root still reads the truth, and `setenforce` hangs the image |

So the sequence that actually gets you furthest:

```python
mumu_root_adb(enabled=True)
mumu_stealth_hide()          # properties + adbd privilege
mumu_stealth_deep()          # cpuinfo, dmi, kallsyms, clocksource, osrelease
mumu_root_adb(enabled=False) # drop adbd last, or it re-exposes root
```

After that, the remaining surface is the sensor count, SELinux's mode, and `uname -r`. Those three are
not closable on this image, and no amount of property work changes that.

### Persistent module — `module/`

Everything above is runtime state: `resetprop` writes the property area and bind mounts die with the
namespace. `module/` packages it as a KernelSU module so it reapplies on every boot, with no PC
attached:

```bash
cd module && zip -r ../dist/mumu-stealth.zip *
adb push ../dist/mumu-stealth.zip /data/local/tmp/
adb shell ksud module install /data/local/tmp/mumu-stealth.zip
```

Verified by rebooting and reading as a plain user: cpuinfo ARM, DMI Samsung, 0 `x64_sys`, 59 vendor
properties deleted, and the 10 mounts still present 90 s after boot.

Three things that had to be right for it to hold, each found the hard way:

| | |
|---|---|
| `kernel_umount` must be **off before** the mounts are made | KernelSU unmounts module mounts "when not needed" — it silently wiped all 10 a few minutes after boot. Turning the feature off afterwards is too late. |
| `selinux_hide` closes the Permissive tell | KernelSU's own feature: it sanitises `/sys/fs/selinux` **for app UIDs**, so a game reads Enforcing while root still sees the truth. `setenforce` cannot do this (it hangs the image). |
| the script must be idempotent per boot | KernelSU can invoke `service.sh` once per stage; without a `boot_id` guard the log doubles and every mount is redone. |

`mumu_stealth_scan` grades SELinux from the app's angle, not its own: with `selinux_hide` on it reports
`Permissive (hidden from apps)` as **clean**, because a scan running as root would otherwise call a
solved problem unfixable.

Final state on this image:

```
clean:      properties, vendor_markers, sensor_count, loaded_modules, su_binary,
            selinux, dmi, cpuinfo, clocksource, block_devices        (10)
leak:       root_dirs, adbd_privilege                                (2, fixable)
unfixable:  uname_r → closeable with mumu_uname_patch                (see below)
```

### Kernel level — `uname(2)`

`uname` is the one tell neither `resetprop` nor a bind mount can reach: it reads a kernel
struct, not a file. Verified — after covering `/proc/version`, `uname -r` still printed
`6.1.90-perf+`.

**The working fix is a kernel module that rewrites `init_uts_ns.name`** — the single struct
every `uname` copies from. System-wide, no per-process tracer, nothing bind-mounted.

```python
mumu_kmod_load()      # push + load, verify the effect
mumu_kmod_status()    # loaded? spoofed? what does uname say now
mumu_kmod_unload()    # restore the real identity
```

Measured across every path a check reads:

```
before: 6.1.90-perf+
after : 6.1.75-android14-11-g4d9e0d1b2c3f
```

`uname -r`, `uname -m`, `uname -a`, `/proc/sys/kernel/osrelease`, `/proc/version`, and a
freshly spawned shell all agree; `rmmod` restores them. The module also loads from the
KernelSU module's `service.sh`, so it survives a reboot.

Why the obvious approach cannot work, stated by the 6.1.90 source itself
(`arch/x86/entry/syscall_64.c`):

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

Dispatch is that `switch`. Writing `sys_call_table[63]` changes a table nothing reads —
which is why a module that poisoned all 55 slots of `struct module` still loaded cleanly and
did nothing. And `set_memory_rw`, needed to make that table writable, takes `cpa_lock` and
flushes the TLB; calling it from module init wedged the guest hard enough that adb and the
manager channel both stopped answering.

`init_uts_ns` sidesteps all of it: it is exported (`__ksymtab_init_uts_ns` is in kallsyms)
and sits past `__end_rodata` in ordinary writable data, so the module references it directly
and writes it with a plain store.

Building it is the hard part — the module must match `uname -r` exactly
(`deprecated/android14-6.1-2024-08`, commit `ab81bbc79`), and on Windows the kernel's own
build plumbing needs several replacements. `native/km/README.md` records every step and the
bug that cost the most time: a `fixdep` stub that wrote to `argv[2]`, truncating every
object file to zero bytes.

### Per-process tracer — `uname(2)` without a module

`native/uname_patch` attaches with ptrace, breaks on syscall exit, and rewrites the
`utsname` in the tracee. It needs no module, so it works on a device where loading one is
not possible. It sits below libc, so it covers a target that calls the syscall directly,
ships its own loader, or clears `LD_PRELOAD`.

```python
mumu_uname_selftest()        # prove it works, with a built-in target
mumu_uname_patch(pid=...)    # attach to a real app
mumu_uname_verify(pid=...)   # read the struct back out of /proc/<pid>/mem
mumu_uname_detach(pid=...)   # stop (kills the tracee — see below)
mumu_uname_daemon_start()    # attach to every matching process, keep them traced
```

Two costs, stated rather than discovered later: every traced thread pays a ptrace stop per
syscall, and `PTRACE_O_EXITKILL` means killing the tracer kills the tracee (so a crashed
tracer cannot leave a game permanently stopped).

The binaries are freestanding x86-64 assembly — this host has clang but no Android sysroot,
and the device ships no headers. `native/README.md` records the three bugs that took:
wait-status decoding (`0x2343`, not `0x7f`), `PTRACE_POKEDATA` argument order, and 65-byte
utsname field padding.

### Kernel input channel

`adb shell input` only does single-pointer events. The touchscreen node exposes the full MT
protocol B, so this writes to it directly:

| tool | purpose |
|---|---|
| `mumu_touch_device` | the multitouch node, found by capability — name, axis range, slot count |
| `mumu_raw_sequence` | **a batch of events in one adb call** |
| `mumu_raw_event` | one event (each call is ~250 ms apart — read the warning below) |
| `mumu_pinch` · `mumu_two_finger_drag` · `mumu_two_finger_hold` | multi-touch gestures |

**`mumu_raw_sequence` is the one that works, and the distinction is not cosmetic.** A tap is
down → SYN → up → SYN within microseconds. Sent as separate `adb shell` calls that becomes a
~500 ms slow drag, and the app ignores it — verified both ways: batched, tapping the launcher's
game-centre icon navigates into `com.mumu.store/.MainActivity`; unbatched, nothing happens.
Single-finger drags written to the node scroll lists correctly.

**Two-finger gestures do not reach the app on this MuMu build.** Pinch and two-finger drag were
driven with a correct MT-B sequence at coordinates where a single-finger swipe scrolls the same
list; nothing moved. MuMu's host input bridge forwards single-pointer events and drops multi-slot
ones, and no MuMu setting changes that. The helpers are kept — they emit a well-formed gesture and
will work unchanged on a build whose bridge forwards multi-touch — but they are documented as
platform-limited rather than shipped as if they worked. To zoom or pan today, drive the UI's own
controls.

```json
[
  {"action": "tap", "x": 720, "y": 1600},
  {"action": "wait_for", "text": "详情"}
]
```
Flows use the standard `input` channel; for a raw event sequence, call `mumu_raw_sequence`.

---

## Design notes

Five things bite anyone writing this against MuMu 6.x. Each is handled in a specific place, on
purpose:

1. **adb endpoint discovery** — NAT loopback first, guest `wlan0` second (`mumu_mcp/adb.py`). Hard
   coding `16384 + idx*32` costs you every adb-backed tool on the current release, silently.
2. **A non-zero exit is not a dead connection.** `uiautomator dump` exits 1 *after* printing
   `UI hierchary dumped to: …`; `pm clear` exits non-zero on a missing package. Only real transport
   markers (`device offline`, `cannot connect`, …) invalidate the cached endpoint, otherwise the UI
   dump tool would tear down its own connection every single call. A genuine transport failure is
   retried once with the endpoint cache dropped, because a restarted instance leaves a dead socket
   registered with adb.
3. **Encoding.** MuMu speaks UTF-8. Decoding with the host codepage (what `subprocess(text=True)`
   does on a zh-CN Windows) turns every `app_name` into `���˽Ȩ`. Bytes are decoded explicitly,
   UTF-8 first, GB18030 as the guest-output fallback.
4. **Writable keys.** `vm_cpu` / `vm_mem` are build-managed on 6.x and reject writes; the
   `.custom` performance keys are the ones that accept them. Both are exposed, and the rejection is
   a typed `key_not_writable` rather than a silent no-op.
5. **Shell escaping.** `input text` needs `%s` for spaces and backslashes for `& < > ' " ( ) | ;`.
   Escape only spaces and half of a login form breaks.

Root has its own set of surprises — no `su`, a host-side overlay, per-boot `adb root` — written up
under [Root](#root) rather than repeated here.

And four interface decisions aimed at the caller being a model:

- **`ok`/`error` envelope** with a `retryable` hint and a stable code — branch on data, not prose.
- **`mumu_diagnose`** — one call that explains the environment instead of a traceback.
- **`mumu_ui_dump` / `mumu_wait_for`** — coordinate-free driving.
- **Preflight validation** — ranges, presets, keycode names and file paths are checked before a
  subprocess is spawned, so a bad argument costs 0 ms and returns a useful message.

## Development

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q                       # hermetic: 215 tests
$env:MUMU_LIVE="1"; .venv\Scripts\python -m pytest -q   # + live emulator: 233
.venv\Scripts\ruff check mumu_mcp tests
```

Layout:

```
mumu_mcp/
  manager.py     MuMuManager wrapper: argv, JSON, typed errors, install discovery
  adb.py         adb transport + endpoint resolution + input escaping
  errors.py      error taxonomy, errcode -> exception map
  constants.py   keycodes, toolbar names, resolution presets, limits
  root.py        root state, adb root, system overlay, diff-image restore
  device.py      foreground, launch, battery, proxy, hosts, processes
  flow.py        scripted step lists, locator resolution, failure reporting
  gesture.py     kernel input channel, MT-protocol-B frames, raw batching
  stealth.py     environment surface scan, property/root hiding, grading, bind-mount layer
  fakeprofile.py the ARM/DMI/clocksource content the deep layer installs
module/        KernelSU module: reapplies everything on boot
  native/        ptrace uname tracer (freestanding asm)
  native/km/     the kernel module that fixes uname(2), and how it builds
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
