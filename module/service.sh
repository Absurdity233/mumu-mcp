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
# `kernel_umount` (on by default) unmounts module mounts when it decides they are
# not needed — which silently wiped every bind below a few minutes after boot.
# It must be turned off BEFORE the mounts are made, or they vanish anyway.
"$KSU_BIN/ksud" feature set kernel_umount 0 2>/dev/null && log "kernel_umount off"

# --- bind-mount the files a professional check opens -------------------------
mkdir -p "$STAGE/dmi" 2>/dev/null

bind() { # bind <staged-name> <target> <content>
  printf '%s' "$3" > "$STAGE/$1" 2>/dev/null || return 1
  umount "$2" 2>/dev/null
  mount --bind "$STAGE/$1" "$2" 2>/dev/null || return 1
  return 0
}

CPUINFO=$(cat "$MODDIR/cpuinfo" 2>/dev/null)
[ -n "$CPUINFO" ] && bind cpuinfo /proc/cpuinfo "$CPUINFO" && log "cpuinfo covered"

KALLSYMS=$(cat "$MODDIR/kallsyms" 2>/dev/null)
[ -n "$KALLSYMS" ] && bind kallsyms /proc/kallsyms "$KALLSYMS" && log "kallsyms covered"

bind cs_cur  /sys/devices/system/clocksource/clocksource0/current_clocksource "arch_sys_counter" \
  && log "clocksource current covered"
# The available list names the hypervisor too — kvm-clock sitting in it is a tell
# even when the active one is clean.
bind cs_avail /sys/devices/system/clocksource/clocksource0/available_clocksource "arch_sys_counter tsc hpet acpi_pm" \
  && log "clocksource available covered"

bind osrelease /proc/sys/kernel/osrelease "$(cat "$MODDIR/osrelease" 2>/dev/null)" \
  && log "osrelease covered"

for kv in sys_vendor product_name product_version board_vendor board_name \
          bios_vendor bios_version chassis_vendor; do
  val=$(grep "^$kv=" "$MODDIR/dmi.prop" 2>/dev/null | cut -d= -f2-)
  [ -n "$val" ] && bind "dmi/$kv" "/sys/class/dmi/id/$kv" "$val"
done
log "dmi covered"

# --- hide SELinux permissive from app uids ----------------------------------
"$KSU_BIN/ksud" feature set selinux_hide 1 2>/dev/null && log "selinux_hide on"


# --- close the root surface --------------------------------------------------
# adbd root is a runtime state, not a persisted one; drop it here so a session
# that boots with root_permission=true still presents as `shell` to a check.
setprop ctl.stop adbd_root 2>/dev/null
log "adbd stage done"

log "=== mumu-stealth done ==="
