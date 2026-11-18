#!/system/bin/sh
# post-fs-data: runs early enough that no app can read the un-hidden surface, but
# before the property area is fully locked, so resetprop can still write it.
#
# Kept separate from service.sh: post-fs-data is blocking and must not wait for
# boot_completed, so it does the property half only and lets service.sh do the
# mounts once the system is up.

MODDIR=${0%/*}
KSU_BIN=/data/adb/ksu/bin
LOG=$MODDIR/stealth.log

log() { echo "[post-fs-data $(date '+%H:%M:%S')] $*" >> "$LOG"; }

# Runs once per boot; KernelSU may invoke it more than once, which would double
# every log line.
BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
STAMP="$MODDIR/.pf_boot"
if [ -n "$BOOT" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$BOOT" ]; then
  exit 0
fi
[ -n "$BOOT" ] && echo "$BOOT" > "$STAMP" 2>/dev/null

RESETPROP="$KSU_BIN/resetprop"
[ -x "$RESETPROP" ] || { log "resetprop missing"; exit 0; }

"$RESETPROP" -n ro.hardware qcom
"$RESETPROP" -n ro.product.board e1q
"$RESETPROP" -n ro.boot.hardware qcom
"$RESETPROP" -n ro.hardware.egl adreno
"$RESETPROP" -n ro.boot.verifiedbootstate green
"$RESETPROP" -n ro.boot.flash.locked 1
"$RESETPROP" -n ro.boot.vbmeta.device_state locked
log "post-fs-data properties done"

# Vendor-marked properties carry MuMu's own service names; they are set by init
# as the services start, so they are handled again in service.sh.
