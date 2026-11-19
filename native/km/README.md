# native/km — the kernel module that hides the emulator surface

`khide_hook.ko` rewrites kernel data so the emulator and root surface disappear, with **no
bind mounts at all**. Everything is set where the kernel keeps it, so nothing appears in
`/proc/self/mounts`.

## Why bind mounts had to go

An earlier version covered `/proc/cpuinfo`, `/proc/kallsyms`, the clocksource files and the
eight DMI attributes with `mount --bind`. The values were right, and it still gave itself
away:

```
/dev/block/sdc3 /proc/cpuinfo ext4 rw,seclabel,noatime 0 0
/dev/block/sdc3 /sys/class/dmi/id/sys_vendor ext4 rw,seclabel,noatime 0 0
```

On real hardware those files come from procfs and sysfs. An ext4 partition as the mount
source is a stronger signal than any value being hidden, and every process can read
`/proc/mounts`.

## What it fixes, and where that data lives

| item | kernel data | how it is reached |
|---|---|---|
| `uname -r`, `uname -m` | `init_uts_ns.name` | exported symbol |
| clocksource | `curr_clocksource->name` | kallsyms |
| DMI identity | `dmi_ident[]` | kallsyms |
| cpuinfo | per-cpu `cpuinfo_x86` | kallsyms + `per_cpu_ptr` |
| the module itself | global `modules` list + sysfs | `list_del` + `mod_sysfs_teardown` |

`kallsyms_lookup_name` is one of the 12012 symbols this kernel exports, so nothing is baked
in — a KASLR reshuffle at every boot changes nothing.

## Measured offsets, not assumed ones

Two wrong guesses each cost a wedged guest, so every offset below was read out of the live
kernel before being written to.

**`struct clocksource` — `name` is at 0x30, not 0.** The struct begins with the `read`
function pointer. Writing a string at offset 0 replaced that pointer with text; the kernel
then called into it while taking a timestamp and the guest stopped answering:

```
read(8) mask(8) mult(4) shift(4) max_idle_ns(8) maxadj(4)
uncertainty_margin(4) max_cycles(8) -> name @ 0x30
```

(`CONFIG_ARCH_CLOCKSOURCE_DATA` is unset, so `archdata` is absent and the offset is fixed.)

**`struct cpuinfo_x86`:**

```
x86_vendor_id   @ 0x88   found by string search, not arithmetic
x86_model_id    @ 0x98   immediately after
x86_capability  @ 0x28
```

Capability at 0x28 is worth checking twice: `0x88 - 0x60 = 0x28`, and 0x60 is exactly
(NCAPINTS 22 + NBUGINTS 2) × 4 — the array ends flush against the vendor string with no
padding. Word 4 there reads `0xfef83203`, bit 31 set, which is `X86_FEATURE_HYPERVISOR`.
Two other candidate offsets also had bit 31 set, which is why the struct arithmetic had to
confirm the answer rather than the value being trusted on appearance.

## The architecture question

The stock kernel reports `aarch64`, and the first version of this module kept that. It was
backwards. Every userspace binary on the image is EM_X86_64:

```
/system/bin/sh            e_machine=3e00   x86_64
/system/bin/toybox        e_machine=3e00   x86_64
/system/bin/app_process64 e_machine=3e00   x86_64
/system/bin/linker64      e_machine=3e00   x86_64
/system/lib64/libc.so     e_machine=3e00   x86_64
```

One read of an ELF header beats every faked value in this file. `uname -m` now reports
`x86_64`, `vendor_id` says `GenuineIntel`, `cpu family 6 / model 156`, and `model name`
names a mobile-class part — so cpuinfo, uname and the binaries all agree.

## Hiding the module itself

`/proc/modules` walks the global `modules` list via `list_entry()`, and `/sys/module/<name>`
is created by `mod_sysfs_setup`. There is no "hidden" flag to set, so the module unlinks
from both. `mod_sysfs_teardown` is not exported and is found through kallsyms.

**The cost, stated rather than discovered later:** after `list_del` the module cannot be
unloaded from userspace and `module_put` bookkeeping no longer sees it. It stays resident
until reboot. That is why the unlink happens last, after everything else has succeeded.

## Building it

The module must match `uname -r` exactly — AOSP branch `deprecated/android14-6.1-2024-08`,
whose Makefile says `SUBLEVEL = 90` (commit `ab81bbc79`):

```bash
git clone --depth 1 --branch deprecated/android14-6.1-2024-08 \
    https://android.googlesource.com/kernel/common android-6.1.90
```

On Windows that needs MSYS2 plus several headers the kernel normally generates for itself.
`build.sh` is the working recipe; the obstacles worth knowing:

| obstacle | resolution |
|---|---|
| `fixdep` aborts with "read: No error" (binary I/O on a Windows path) | no-op stub — **which must not write to `argv[2]`**, because that is the target `.o`. The first version did, truncating every object to zero bytes and making modpost report `Invalid argument` on an empty ELF. |
| `objtool` needs libelf, unavailable for mingw | stub; it only validates |
| `check-local-export` kills itself when `$NM`/`$AWK` arrive empty | stub; this module exports nothing |
| `CC_VERSION_TEXT` contains parentheses, which MSYS `sh` reads as syntax | rewritten to a plain value |
| `M=` rejects `F:/path` ("cannot contain ':'") and mangles `/f/path` | module lives at `khide_build/`, so `M=` is relative |
| generated headers (`autoconf.h`, `bounds.h`, `timeconst.h`, `unistd_64.h`, asm wrappers, `module.lds`) | generated from `.config` and the syscall tables |
| `file2alias.c` needs `devicetable-offsets.h` | stub; this module declares no device tables |

`scripts/service.sh` must have **LF line endings** — Android's `sh` fails on `\r` with
`syntax error: unexpected 'do'`, and the module silently does nothing at boot.

## Result

```
uname -r : 6.1.75-android14-11-g4d9e0d1b2c3f
uname -m : x86_64
cpuinfo  : GenuineIntel / Intel(R) Celeron(R) N5105 @ 2.00GHz
flags    : no hypervisor, no vmmcall
dmi      : Samsung Electronics Co., Ltd. / SM-S9210
clocksrc : arch_sys_counter
lsmod    : 0   (the module is not listed)
mounts   : 0   (nothing is mounted over anything)
```

Verified after a reboot, so the KernelSU module's `service.sh` loads it at boot.
`rmmod` restores the originals — but only in the build that skips the self-hiding step, as
noted above.
