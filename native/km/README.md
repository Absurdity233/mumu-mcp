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
| `/proc/version` | `linux_proc_banner` | `text_poke` |
| `/proc/cmdline` | `saved_command_line` | direct buffer write |
| clocksource | `curr_clocksource->name` | kallsyms |
| DMI identity | `dmi_ident[]` | kallsyms |
| cpuinfo | per-cpu `cpuinfo_x86` | kallsyms + `per_cpu_ptr` |
| dmesg access | `dmesg_restrict` | kallsyms |
| `/sys/module` net+PC drivers | global `modules` list + sysfs | `list_del` / `kernfs_remove` |
| `/sys/module` Qualcomm dirs | `/sys` → "module" node | `kernfs_create_dir_ns` |
| `/sys/bus/virtio` | kernfs subtree | `kernfs_remove` |
| virtio device nodes | `kernfs_node->name` | `kernfs_rename_ns` |
| device tree | `file` nodes under `/sys/firmware` | `kernfs_create_dir_ns` + `sysfs_add_bin_file_mode_ns` |
| `/proc/device-tree` | procfs | `proc_symlink` |
| Adreno GPU | `/sys/class/kgsl` | kernfs, walked from `/sys` |
| `/dev/kgsl-3d0` | character device | `alloc_chrdev_region` + `cdev_add`, then `mknod` from `service.sh` |
| the module itself | `modules` list + sysfs | `list_del` + `mod_sysfs_teardown` |

`kallsyms_lookup_name` is one of the 12012 symbols this kernel exports, so nothing is baked
in — a KASLR reshuffle at every boot changes nothing.

## Three deadlocks, and what each one taught

Every entry that crashed or hung the guest is recorded here, because the pattern repeats:
**the kset-backed trees cannot be extended from a module, but the same directories are
reachable a second way.**

### `set_memory_rw` on `.rodata`

`set_memory_rw` takes `cpa_lock` and flushes the TLB. Calling it from module init wedged the
guest so completely that adb and the manager channel both stopped answering.

`text_poke` does the same job through the text mutex and is safe. Verified on its own before
being used: the guest stayed responsive and the string changed.

### `class_kset->kobj.sd` as a parent

`class_kset` exists as a symbol, but creating children under `kobj.sd` deadlocks. `/sys` is
reachable another way — `firmware_kobj->sd->parent` is the `/sys` node — and walking down
from there to `class` gives a **different** node that creates fine.

### `module_kset->kobj.sd` as a parent

Same trap, and deferring to a workqueue does not help, so it is not a calling-context
problem. Measuring both paths showed why:

```
module_kset->kobj.sd        ffff9190c0157900   <- deadlocks on any create
/sys -> "module" node       ffff9190c0bcf110   <- different node
```

`kernfs_find_and_get_ns(sys_sd, "module")` returns the second one. Children created under it
land in `/sys/module` normally, and the kset lock is never touched.

## Why `kernfs_create_dir_ns` needs seven arguments

```c
struct kernfs_node *kernfs_create_dir_ns(parent, name, mode, uid, gid, priv, ns);
```

Called with six, `ns` shifts into `priv`, and `kernfs_add_one` rejects the node with a WARN
and `-EINVAL`:

```c
has_ns = kernfs_ns_enabled(parent);
if (WARN(has_ns != (bool)kn->ns, "kernfs: ns %s in '%s' for '%s'\n", ...))
        goto out_unlock;   /* ret = -EINVAL */
```

That WARN runs while holding `kernfs_rwsem`, which is why the first attempt looked like a
hang rather than an error.

## Why `sysfs_add_bin_file_mode_ns`, not `sysfs_create_bin_file`

```c
int sysfs_create_bin_file(struct kobject *kobj, const struct bin_attribute *attr)
{
        if (WARN_ON(!kobj || !kobj->sd || !attr)) return -EINVAL;
        kobject_get_ownership(kobj, &uid, &gid);            /* <- frozen the guest */
        return sysfs_add_bin_file_mode_ns(kobj->sd, ...);
}

int sysfs_add_bin_file_mode_ns(struct kernfs_node *parent, const struct bin_attribute *battr, ...)
{
        const struct kernfs_ops *ops;
        if (battr->read) ops = &sysfs_bin_kfops_ro;   /* never touches parent->priv */
        ...
        return __kernfs_create_file(parent, ...);
}
```

The second one works on a bare kernfs node because it reads the ops from the attribute and
never looks at the parent kobject.

(`sysfs_add_file_mode_ns` does not work for this: it dereferences `parent->priv` as a
kobject and then `kobj->ktype->sysfs_ops`, and a node made by `kernfs_create_dir_ns` has
`priv == NULL`.)

## Not solved

Two things remain, both recorded rather than hidden:

**Block device model.** `/sys/block/sdX/device/model` still reads `HARDDISK`. The field is
`sdev->model` on `struct scsi_device`, reached as:

    sdev_show_model -> to_scsi_device(dev) -> dev_get_drvdata(dev)

`kernfs_node->priv` is not uniformly a `struct device *` — on the SCSI target node
`0:0:0:0` it reads `5` — so the chain has to start from the right node, and
`struct device.driver_data` varies because the struct embeds `dev_links_info` and
`dev_pm_info`. Six approaches were tried; two faulted the guest, four found no match.
`struct scsi_device` is also `__randomize_layout`, so field offsets cannot be computed.

**Real driver code.** The `/sys/module` entries for `ufs_qcom`, `msm_drm`, `arm_smmu` and
`sdhci_msm` are directories with attributes, not drivers. A checker that opens
`/sys/module/ufs_qcom/sections/.text` sees nothing, where a handset shows a real section
address. Closing that needs the driver compiled into the kernel, and MuMu's kernel source
(`nemu-vbox7-source-code`, seen in the build path inside `VBoxDD.dll`) is not public.

The kernel image itself was located while investigating this: `system.vdi` at offset
`0x17c0000`, 12,362,752 bytes, uncompressed, `6.1.90-perf+ #3 SMP PREEMPT_DYNAMIC
Fri Nov 6 09:17:00` — matching the running guest exactly. So a replacement kernel has a
known landing site; what it cannot have is drivers that were never written.
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

Measured from the shell uid (2000), i.e. what an app can see:

```
uname -r  : 6.1.75-android14-11-g4d9e0d1b2c3f
uname -m  : x86_64
/proc/version : Linux version 6.1.75-... (builder@localhost) (Android clang 19.0.1, LLD 19.0.1)
cpuinfo   : GenuineIntel / Intel(R) Celeron(R) N5105 @ 2.00GHz
flags     : no hypervisor, no vmmcall
dmi       : Samsung Electronics Co., Ltd. / SM-S9210
/proc/device-tree : present, model = Samsung SM-S9210, compatible = qcom,sm8650 ...
/dev/kgsl-3d0     : crw-rw-rw- 241,0     /dev/kgsl-2d0: crw-rw-rw- 241,1
/sys/class/kgsl/kgsl-3d0/gpu_model : Adreno640v2
/sys/class/drm    : card0, renderD128
/sys/module       : 106 entries, incl. ufs_qcom, msm_drm, arm_smmu, sdhci_msm
/sys/bus/virtio   : absent, and 0 virtio device nodes
clocksource : arch_sys_counter
dmesg       : refused
mounts      : 0 override lines
/proc/modules : f2fs, qmi_wwan, vaddress   (the module hides itself)
```

Verified after a reboot, so the KernelSU module's `service.sh` loads it at boot and the
kernel-side node creation happens again each time.

`rmmod` restores the originals — but only in a build that skips the self-hiding step, since
the shipped one unlinks itself from the module list.

The one tell still open is the block device model string; see "Not solved" above for the
measurements behind that.
