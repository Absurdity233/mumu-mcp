#!/system/bin/sh
# mumu-stealth — persistent environment/root hiding for MuMu Player.
#
# Runs as a KernelSU module at post-fs-data and service time, so the hides are
# reapplied on every boot without a host-side helper.
#
# Why a module and not just the MCP tools: `resetprop` writes the property area
# and `mount --bind` dies with the mount namespace, so both are lost on reboot.
# A module re-installs them early enough that no app can observe the un-hidden
# window, and it survives without a PC attached.

MODDIR=/data/adb/modules/mumu-stealth
KSU_BIN=/data/adb/ksu/bin
STAGE=/data/local/tmp/.mumu_stealth
LOG=$MODDIR/stealth.log

log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"; }

# KernelSU can invoke service.sh once per boot stage, so the whole script has to be
# idempotent — without this the log doubles every line and every bind is redone.
# The lock is keyed on the boot id, so a reboot always runs it again.
BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
STAMP="$MODDIR/.last_boot"
if [ -n "$BOOT" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$BOOT" ]; then
  exit 0
fi
[ -n "$BOOT" ] && echo "$BOOT" > "$STAMP" 2>/dev/null

# --- wait for the property service and a usable root shell -------------------
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 2

log "=== mumu-stealth start ==="

RESETPROP="$KSU_BIN/resetprop"
if [ ! -x "$RESETPROP" ]; then
  log "resetprop missing; aborting property stage"
else
  # --- properties: real SM-S9210 values ---------------------------------------
  "$RESETPROP" -n ro.hardware qcom
  "$RESETPROP" -n ro.product.board e1q
  "$RESETPROP" -n ro.boot.hardware qcom
  "$RESETPROP" -n ro.hardware.egl adreno
  "$RESETPROP" -n ro.boot.verifiedbootstate green
  "$RESETPROP" -n ro.boot.flash.locked 1
  "$RESETPROP" -n ro.boot.vbmeta.device_state locked
  log "properties set"

  # --- delete every vendor-marked property ------------------------------------
  # `-n key ''` leaves the key visible in the property area; `-d` removes it.
  #
  # The key list is collected first and iterated with a here-doc rather than a
  # pipe: `... | while read` runs the loop in a subshell, so `deleted` never
  # survives it and the count would always log as 0.
  deleted=0
  keys=$(getprop | grep -oE '^\[[^]]*(nemu|mumu|qemu|goldfish|vbox|virtualbox|vmware|haxm|whpx)[^]]*\]' | tr -d '[]')
  for key in $keys; do
    val=$(getprop "$key")
    [ -z "$val" ] && continue
    "$RESETPROP" -d "$key" 2>/dev/null && deleted=$((deleted + 1))
  done
  # A kept-but-blanked key still shows up in `getprop`; delete by value for the
  # ones whose *value* (not name) is the tell, e.g. ro.build.hv.platform=vbox.
  for key in $(getprop | grep -oE '^\[[^]]+\]: \[[^]]*(vbox|virtualbox|nemu|mumu|qemu)[^]]*\]' | sed 's/^\[\([^]]*\)\].*/\1/'); do
    "$RESETPROP" -d "$key" 2>/dev/null && deleted=$((deleted + 1))
  done
  log "vendor properties deleted: $deleted"
fi

# --- stop KernelSU from unmounting the overrides ---------------------------
# --- the previous bind-mount layer is gone ----------------------------------
# Earlier versions covered /proc/cpuinfo, /proc/kallsyms, the clocksource files and the
# DMI attributes with `mount --bind`. That worked, but every one of those mounts shows up
# in /proc/self/mounts, which any process can read: on real hardware /proc/cpuinfo comes
# from procfs and /sys/class/dmi/id/* from sysfs, so an ext4 partition as the source is a
# bigger tell than the value it was hiding.
#
# khide_hook.ko now sets all of it in kernel data instead, so nothing is mounted and
# /proc/mounts stays clean. The staged files and the bind helper are removed rather than
# left dormant — a mount that fires on some future boot would reintroduce the tell.

# --- hide SELinux permissive from app uids ----------------------------------
"$KSU_BIN/ksud" feature set selinux_hide 1 2>/dev/null && log "selinux_hide on"

# --- kernel-level uname spoof ------------------------------------------------
# Loads khide_hook.ko, which rewrites init_uts_ns.name. That is the only path that reaches
# uname(2): sys_call_table is not consulted for dispatch on this kernel (x64_sys_call is a
# switch), so patching the table does nothing, and init_uts_ns is exported and writable, so
# no code is patched and set_memory_rw — whose cpa_lock wedged the guest once — is never
# called.
#
# Loaded here rather than in post-fs-data because the module needs the kernel's module
# loader to be up, and service time is after that.
KMOD=$MODDIR/khide_hook.ko
if [ -f "$KMOD" ]; then
  insmod "$KMOD" 2>/dev/null && log "khide_hook loaded" || log "khide_hook load failed"
fi

# --- close the root surface --------------------------------------------------
# adbd root is a runtime state, not a persisted one; drop it here so a session
# that boots with root_permission=true still presents as `shell` to a check.
setprop ctl.stop adbd_root 2>/dev/null
log "adbd stage done"

# --- delete vendor properties a second time ----------------------------------
# The stage above runs as soon as boot_completed flips, and MuMu's own services keep
# writing `nemud.*` keys for a while after that — the emulator sets its input, gamepad and
# player properties while its launcher is still finishing. Deleting once is therefore not
# enough: three `nemud.input.gamepad.*` keys reappeared after the first pass and were only
# caught by scanning again later.
#
# A second sweep after a delay catches them. The keys are deleted rather than blanked,
# because a key that exists with an empty value still shows up in `getprop` and is itself
# a marker.
sleep 20
swept=0
for key in $(getprop | grep -oE '^\[[^]]*(nemu|mumu|qemu|goldfish|vbox|virtualbox|vmware|haxm|whpx)[^]]*\]' | tr -d '[]'); do
  "$RESETPROP" -d "$key" 2>/dev/null && swept=$((swept + 1))
done
log "late vendor properties deleted: $swept"


# --- make /dev/kgsl-3d0 ------------------------------------------------------
# khide_hook.ko registers a character-device major named "kgsl-3d0" (alloc_chrdev_region
# + cdev_add) but does not create the node: building a dentry from inside a module means
# reaching into struct nameidata, whose layout is internal. mknod from userspace produces
# the same S_IFCHR entry a real driver's devtmpfs node would.
#
# The major is read back from /proc/devices rather than assumed, because it is allocated
# dynamically and changes if another driver takes the slot first.
KGSL_MAJOR=$(awk '$2 == "kgsl-3d0" { print $1 }' /proc/devices 2>/dev/null)
if [ -n "$KGSL_MAJOR" ]; then
  rm -f /dev/kgsl-3d0 /dev/kgsl-2d0
  mknod /dev/kgsl-3d0 c "$KGSL_MAJOR" 0 2>/dev/null && log "kgsl-3d0 node created (major $KGSL_MAJOR)"
  mknod /dev/kgsl-2d0 c "$KGSL_MAJOR" 1 2>/dev/null
  chmod 0666 /dev/kgsl-3d0 /dev/kgsl-2d0 2>/dev/null
else
  log "kgsl major not registered; node skipped"
fi
log "=== mumu-stealth done ==="
