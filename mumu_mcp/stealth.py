"""Environment surface: what this instance leaks, and how to close it.

Written for the case where something on the guest (an anti-cheat, an app's
integrity check, a QA harness) actively looks for emulator and root traces.
Two halves:

* `scan()` — enumerate the leak surface and grade each item. Nothing is changed.
* `hide()` — apply the fixes that are reversible, keep a record, and verify.

What actually works on MuMu 6.8.x / Android 15, measured:

* `ro.*` properties are read-only to `setprop`, but **`resetprop` can overwrite
  them in place** — it is KernelSU's Magisk-compatible property tool, present at
  `/data/adb/ksu/bin/resetprop`. That is the only way to fix a `ro.` value after
  init has read it. Verified: setting `ro.kernel.qemu` and reading it back works.
* MuMu already suppresses the classic `ro.kernel.qemu`; `ro.hardware` /
  `ro.product.board` carry a real device identity. The leaks are elsewhere.
* Anything written through the root overlay (`/system/...`) survives a reboot and
  is undone by `mumu_root_restore_system`.

What does **not** work, and is reported as such instead of being faked:

* **Sensor count.** `dumpsys sensorservice` lists one sensor here; a real
  SM-S9210 reports 20+. Closing that needs a faked sensors HAL, not a property.
* **Kernel self-consistency.** `uname` reports aarch64 while `/proc/cpuinfo`
  carries `AuthenticAMD` and `hypervisor`, and kallsyms exposes
  `__x64_sys_*`. Each mismatch is a signal.
* **Loaded modules.** `/proc/modules` lists every module; hiding one requires a
  kernel hook, which is itself detectable.

`scan()` grades each item `clean` / `leak` / `unfixable` so a caller can see the
ceiling before spending effort on it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .adb import Adb
from .errors import BadArgument
from .manager import Manager
from .root import Root

log = logging.getLogger("mumu_mcp.stealth")

KSU_BIN = "/data/adb/ksu/bin"

# (property, expected), where "expected" is what a real handset looks like.
PROP_PROFILE: dict[str, tuple[str, str]] = {
    "ro.kernel.qemu": ("", "empty on real hardware"),
    "ro.hardware": ("qcom", "Samsung SM-S9210 uses qcom"),
    "ro.product.board": ("e1q", "SM-S9210 board codename"),
    "ro.product.device": ("e1q", "SM-S9210 device codename"),
    "ro.boot.hardware": ("qcom", ""),
    "ro.hardware.egl": ("adreno", ""),
    "ro.debuggable": ("0", ""),
    "ro.secure": ("1", ""),
    "ro.build.tags": ("release-keys", ""),
    "ro.build.type": ("user", ""),
    "ro.adb.secure": ("1", ""),
    "ro.boot.verifiedbootstate": ("green", "green = locked verity"),
    "ro.boot.flash.locked": ("1", ""),
    "ro.boot.vbmeta.device_state": ("locked", ""),
}

# Substrings that give the game away when they appear in a property value.
# `vbox` was missing from the first version and `ro.build.hv.platform=vbox` is a
# direct VirtualBox admission — one of the strongest tells on the image.
EMULATOR_MARKERS = ("nemu", "mumu", "qemu", "goldfish", "vbox", "virtualbox",
                    "vmware", "hypervisor", "haxm", "whpx")

# Su binaries at the paths integrity checks grep for.
SU_PATHS = ("/system/bin/su", "/system/xbin/su", "/sbin/su", "/su/bin/su",
            "/magisk/.core/bin/su")

# Directories that only exist on a rooted device.
ROOT_PATHS = ("/data/adb/ksu", "/data/adb/magisk", "/data/adb/modules", "/sbin/.magisk")

# MuMu's own init services. Each one is a `nemu*` name in getprop, and stopping
# the service is the only way to keep the property from being rewritten.
NEMU_SERVICES = ("nemu_sys_opt", "nemufreepfn", "nemuhotd", "nemuinit", "nemuinput")


@dataclass
class Finding:
    name: str
    value: str
    status: str          # clean | leak | unfixable
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "status": self.status,
                "detail": self.detail, "fix": self.fix}


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        leaks = [f for f in self.findings if f.status == "leak"]
        unfixable = [f for f in self.findings if f.status == "unfixable"]
        return {
            "score": _score(self.findings),
            "counts": {"total": len(self.findings), "clean": len(self.findings) - len(leaks) - len(unfixable),
                       "leak": len(leaks), "unfixable": len(unfixable)},
            "fixable_leaks": [f.to_dict() for f in leaks],
            "unfixable": [f.to_dict() for f in unfixable],
            "all": [f.to_dict() for f in self.findings],
        }


def _score(findings: list[Finding]) -> dict[str, Any]:
    fixable = sum(1 for f in findings if f.status == "leak")
    fatal = sum(1 for f in findings if f.status == "unfixable")
    # A single unfixable item is enough for anything serious to fail, so the
    # headline verdict keys on those, not on the leak count.
    verdict = "clean" if fatal == 0 and fixable == 0 else ("compromised" if fatal else "hides_needed")
    return {"verdict": verdict, "fixable": fixable, "unfixable": fatal}


class Stealth:
    """Scan and hide the emulator/root surface of one instance."""

    def __init__(self, adb: Adb, root: Root, manager: Manager):
        self.adb = adb
        self.root = root
        self.manager = manager
        self._applied: dict[int, dict[str, str]] = {}

    # -- helpers -----------------------------------------------------------
    def _sh(self, vmindex: int, cmd: str, timeout: float = 30.0) -> str:
        return self.adb.shell(vmindex, cmd, timeout=timeout)

    def _has_resetprop(self, vmindex: int) -> bool:
        out = self._sh(vmindex, f"ls {KSU_BIN}/resetprop 2>/dev/null")
        return "resetprop" in out

    def _getprop(self, vmindex: int, name: str) -> str:
        return self._sh(vmindex, f"getprop {name}").strip()

    # -- scan --------------------------------------------------------------
    def scan(self, vmindex: int = 0) -> Scan:
        if not self.root.state(vmindex).is_root:
            raise BadArgument(
                "an accurate scan needs root: half the surface is unreadable otherwise",
                detail={"fix": "mumu_root_adb(True)"},
            )
        scan = Scan()
        props = self._collect_props(vmindex)
        scan.findings.append(self._props_finding(props))
        scan.findings.append(self._markers_finding(props))
        scan.findings.append(self._sensors_finding(vmindex))
        scan.findings.append(self._kernel_finding(vmindex))
        scan.findings.append(self._modules_finding(vmindex))
        scan.findings.append(self._su_finding(vmindex))
        scan.findings.append(self._rootfs_finding(vmindex))
        scan.findings.append(self._debug_finding(vmindex))
        scan.findings.append(self._selinux_finding(vmindex))
        scan.findings.append(self._dmi_finding(vmindex))
        scan.findings.append(self._cpuinfo_finding(vmindex))
        scan.findings.append(self._clocksource_finding(vmindex))
        scan.findings.append(self._blockdev_finding(vmindex))
        return scan

    def _dmi_finding(self, vmindex: int) -> Finding:
        """DMI is the strongest VM tell on this image: `innotek GmbH` is VirtualBox."""
        from . import fakeprofile as fp

        values = {}
        for key in ("sys_vendor", "product_name", "bios_vendor", "board_vendor"):
            out = self._sh(vmindex, f"cat {fp.DMI_DIR}/{key} 2>/dev/null").strip()
            if out:
                values[key] = out
        bad = {k: v for k, v in values.items()
               if any(m in v.lower() for m in ("innotek", "vbox", "virtualbox", "qemu", "vmware"))}
        covered = self._verify_fakes(vmindex).get("dmi_sys_vendor", {}).get("covered", False)
        if covered and not bad:
            return Finding("dmi", "overridden", "clean")
        return Finding(
            name="dmi", value=f"{len(bad)} VM-vendor fields" if bad else "suspicious",
            status="leak",
            detail="; ".join(f"{k}={v}" for k, v in bad.items()) or str(values)[:80],
            fix="mumu_stealth_deep(cpuinfo/dmi) — bind-mounts a handset DMI over the sysfs file",
        )

    def _cpuinfo_finding(self, vmindex: int) -> Finding:
        """`vendor_id : AuthenticAMD` under `uname -m = aarch64` cannot both be true."""
        from . import fakeprofile as fp

        real = self._sh(vmindex, f"head -20 {fp.OVERRIDES['cpuinfo']} 2>&1")
        covered = "CPU implementer" in real and "AuthenticAMD" not in real
        if covered:
            return Finding("cpuinfo", "ARM profile", "clean")
        x86 = "AuthenticAMD" in real or "vendor_id" in real
        return Finding(
            name="cpuinfo", value="x86 on aarch64" if x86 else "unknown",
            status="leak" if x86 else "clean",
            detail="vendor_id: AuthenticAMD while uname -m says aarch64" if x86 else "",
            fix="mumu_stealth_deep(cpuinfo=True)" if x86 else "",
        )

    def _clocksource_finding(self, vmindex: int) -> Finding:
        """`kvm-clock` names the hypervisor; no handset has it."""
        from . import fakeprofile as fp

        current = self._sh(vmindex, f"cat {fp.OVERRIDES['clocksource_current']} 2>/dev/null").strip()
        available = self._sh(vmindex, f"cat {fp.OVERRIDES['clocksource_available']} 2>/dev/null").strip()
        leak = "kvm" in current.lower() or "kvm" in available.lower()
        return Finding(
            name="clocksource", value=current or "unknown",
            status="leak" if leak else "clean",
            detail=f"available: {available}" if leak else "",
            fix="mumu_stealth_deep(clocksource=True)" if leak else "",
        )

    def _blockdev_finding(self, vmindex: int) -> Finding:
        """VirtIO `vda`/`vdb` is a hard tell; SATA `sd*` is merely unusual on a phone."""
        out = self._sh(vmindex, "ls /dev/block/ 2>/dev/null | grep -E '^vd|^nvme' | head -4")
        virtio = [x for x in out.split() if x]
        return Finding(
            name="block_devices", value="virtio" if virtio else "sd/ufs",
            status="unfixable" if virtio else "clean",
            detail=", ".join(virtio) if virtio else "no virtio/nvme device names",
            fix="" if not virtio else "device naming comes from the kernel driver set; not a file",
        )

    def _collect_props(self, vmindex: int) -> dict[str, str]:
        out = self._sh(vmindex, "getprop", timeout=40)
        props: dict[str, str] = {}
        for line in out.splitlines():
            m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]$", line.strip())
            if m:
                props[m.group(1)] = m.group(2)
        return props

    def _props_finding(self, props: dict[str, str]) -> Finding:
        wrong = []
        for key, (expected, _why) in PROP_PROFILE.items():
            actual = props.get(key, "")
            if expected == "":
                if actual:
                    wrong.append(f"{key}={actual!r} (want empty)")
            elif actual != expected:
                wrong.append(f"{key}={actual!r} (want {expected!r})")
        status = "leak" if wrong else "clean"
        return Finding(
            name="properties", value=f"{len(wrong)} mismatched",
            status=status, detail="; ".join(wrong[:8]),
            fix="mumu_stealth_hide(properties=True)" if wrong else "",
        )

    def _markers_finding(self, props: dict[str, str]) -> Finding:
        """Vendor markers that still carry a value.

        A deleted property can linger in the property area with an empty value —
        `resetprop -d` removes the entry, but the name is not always reclaimed.
        Counting those as leaks reports a fix as a failure, so only non-empty
        values count.
        """
        hits = []
        for key, value in props.items():
            if not value:
                continue
            low = f"{key}={value}".lower()
            for marker in EMULATOR_MARKERS:
                if marker in low:
                    hits.append(f"{key}={value}")
                    break
        status = "leak" if hits else "clean"
        return Finding(
            name="vendor_markers", value=f"{len(hits)} live properties",
            status=status, detail="; ".join(hits[:8]),
            fix="mumu_stealth_hide(properties=True)" if hits else "",
        )

    def _sensors_finding(self, vmindex: int) -> Finding:
        """Count real sensors from the `Sensor List:` block.

        The first version grepped `^[0-9]{6,}` against the whole dump, which only
        matched the one line whose handle happens to start with six digits — it
        reported 1 sensor on an image that has 15, and graded that as unfixable.
        Handles are printed as `0x00000000)`, so the pattern has to anchor on the
        hex form and stay inside the sensor-list section.
        """
        out = self._sh(
            vmindex,
            "dumpsys sensorservice 2>/dev/null | sed -n '/Sensor List:/,/^$/p' "
            "| grep -cE '^0x[0-9a-f]{8}\\)'",
        )
        try:
            count = int(out.strip() or 0)
        except ValueError:
            count = 0
        # A handset exposes far more than this; 15 is what this image reports.
        status = "clean" if count >= 10 else "unfixable"
        return Finding(
            name="sensor_count", value=str(count),
            status=status,
            detail=f"{count} sensors reported" if count < 10 else f"{count} sensors reported",
            fix="" if count >= 10 else
                "fewer sensors than a handset; needs a faked sensors HAL",
        )

    def _kernel_finding(self, vmindex: int) -> Finding:
        """Report *which* kernel tell is still open, not a single aggregate verdict.

        After the bind-mount layer the cpuinfo and kallsyms halves can be clean
        while `uname -r` still names the real kernel, so an all-or-nothing
        "inconsistent" hides how much is left.
        """
        uname_m = self._sh(vmindex, "uname -m").strip()
        uname_r = self._sh(vmindex, "uname -r").strip()
        cpu = self._sh(vmindex, "grep -m1 'vendor_id\\|CPU implementer' /proc/cpuinfo").strip()
        hypervisor = "hypervisor" in self._sh(vmindex, "grep -m1 '^flags' /proc/cpuinfo")
        kallsyms = "x64_sys" in self._sh(vmindex, "grep -m1 init_module /proc/kallsyms 2>/dev/null")

        open_tells: list[str] = []
        if "AuthenticAMD" in cpu or "vendor_id" in cpu:
            open_tells.append("cpuinfo says x86")
        if hypervisor:
            open_tells.append("cpuinfo has the hypervisor flag")
        if kallsyms:
            open_tells.append("kallsyms exposes __x64_sys_*")
        if "perf+" in uname_r or "android" not in uname_r.lower():
            open_tells.append(f"uname -r is {uname_r!r} (a handset reports -android)")

        detail = f"uname={uname_m}/{uname_r}"
        if not open_tells:
            return Finding("kernel_consistency", "consistent", "clean", detail=detail)
        return Finding(
            name="kernel_consistency",
            value=f"{len(open_tells)} open tell(s)",
            status="unfixable",
            detail="; ".join(open_tells),
            fix="uname(2) is a syscall; the cpuinfo/kallsyms halves close via mumu_stealth_deep, "
                "the uname half needs an LD_PRELOAD shim or a kernel hook",
        )

    def _modules_finding(self, vmindex: int) -> Finding:
        out = self._sh(vmindex, "cat /proc/modules")
        names = [ln.split()[0] for ln in out.splitlines() if ln.split()]
        # Modules that ship with the image are expected; anything else is yours.
        known = {"f2fs", "qmi_wwan", "vaddress"}
        extra = [n for n in names if n not in known]
        return Finding(
            name="loaded_modules", value=f"{len(names)}",
            status="leak" if extra else "clean",
            detail=", ".join(extra[:6]) if extra else "stock set only",
            fix="" if not extra else
                "loading any module is visible in /proc/modules; hiding it needs a kernel hook",
        )

    def _su_finding(self, vmindex: int) -> Finding:
        found = []
        for path in SU_PATHS:
            if "No such" not in self._sh(vmindex, f"ls -l {path} 2>&1"):
                found.append(path)
        which = self._sh(vmindex, "which su 2>/dev/null").strip()
        if which:
            found.append(f"which su -> {which}")
        return Finding(
            name="su_binary", value=f"{len(found)} found",
            status="leak" if found else "clean",
            detail=", ".join(found[:5]),
            fix="mumu_stealth_hide(root=True)" if found else "",
        )

    def _rootfs_finding(self, vmindex: int) -> Finding:
        found = [p for p in ROOT_PATHS if "No such" not in self._sh(vmindex, f"ls {p} 2>&1")]
        return Finding(
            name="root_dirs", value=f"{len(found)} present",
            status="leak" if found else "clean",
            detail=", ".join(found),
            fix="mumu_stealth_hide(root=True)" if found else "",
        )

    def _debug_finding(self, vmindex: int) -> Finding:
        debuggable = self._getprop(vmindex, "ro.debuggable")
        adb_root = self._sh(vmindex, "id").strip()
        is_root_adbd = "uid=0" in adb_root
        # adbd as root is the one that actually matters: a game can read it.
        status = "leak" if is_root_adbd else "clean"
        return Finding(
            name="adbd_privilege", value="root" if is_root_adbd else "shell",
            status=status, detail=f"ro.debuggable={debuggable}",
            fix="mumu_root_adb(enabled=False) before shipping, or mumu_stealth_hide(root=True)"
                if is_root_adbd else "",
        )

    def _selinux_finding(self, vmindex: int) -> Finding:
        """Report SELinux from the angle that matters: what an app sees.

        KernelSU's `selinux_hide` feature sanitises `/sys/fs/selinux` **for app
        UIDs only** — root and shell still read the real mode. So a scan running as
        root would call this a leak even when the game is already being told
        "Enforcing". The check queries the feature state and reports both views.
        """
        mode = self._sh(vmindex, "getenforce").strip()
        feature = self._sh(
            vmindex,
            "timeout 10 /data/adb/ksu/bin/ksud feature get selinux_hide 2>/dev/null "
            "| grep -i '^Value' | grep -oE '[01]'",
        ).strip()
        hidden_for_apps = feature == "1"

        if mode == "Enforcing":
            return Finding(name="selinux", value=mode, status="clean")
        if hidden_for_apps:
            return Finding(
                name="selinux", value=f"{mode} (hidden from apps)", status="clean",
                detail="ksud selinux_hide enabled: app UIDs see a sanitised value, root/shell see the truth",
            )
        return Finding(
            name="selinux", value=mode,
            status="leak",
            detail="Permissive, and selinux_hide is off so apps can see it",
            fix="mumu_stealth_selinux_hide(enabled=True) — toggles KernelSU's built-in feature",
        )

    # -- deep hide: bind-mount over the fake-heavy files ------------------
    def deep_hide(self, vmindex: int = 0, *, cpuinfo: bool = True, dmi: bool = True,
                  clocksource: bool = True, osrelease: bool = True,
                  kallsyms: bool = True) -> dict[str, Any]:
        """Cover the files a professional check opens, using bind mounts.

        Measured on this image: `/sys/class/dmi/id/*` and `/proc/cpuinfo` are not
        writable, but `mount --bind <fake> <target>` **does** change what a read
        returns. That is the only mechanism that fixes the DMI and cpuinfo leaks
        without a kernel hook — and unlike a hook, a bind mount leaves no code
        patch to detect.

        What this cannot do, and the caller is told so: `uname(2)` is a syscall,
        not a file, so `uname -m` still says what the kernel says. Covering it
        needs an LD_PRELOAD shim (per-process, useless for a game that has its own
        loader) or a kernel hook (a stronger signal than the leak itself).

        Everything here is runtime state: a reboot removes the mounts.
        """
        state = self.root.state(vmindex)
        if not state.is_root:
            raise BadArgument("bind-mount overrides need root", detail={"fix": "mumu_root_adb(True)"})
        if not state.system_writable and (cpuinfo or dmi):
            # /data/local/tmp is writable either way; /system is not involved.
            pass

        from . import fakeprofile as fp

        stage = fp.STAGE_DIR
        self._sh(vmindex, f"mkdir -p {stage}/dmi", timeout=20)
        applied: dict[str, Any] = {"staged": stage}

        if cpuinfo:
            applied["cpuinfo"] = self._bind(
                vmindex, f"{stage}/cpuinfo", fp.OVERRIDES["cpuinfo"], fp.cpuinfo())
        if osrelease:
            applied["osrelease"] = self._bind(
                vmindex, f"{stage}/osrelease", fp.OVERRIDES["osrelease"], fp.OSRELEASE + "\n")
        if clocksource:
            applied["clocksource"] = {
                "available": self._bind(
                    vmindex, f"{stage}/cs_avail", fp.OVERRIDES["clocksource_available"],
                    fp.CLOCKSOURCE_AVAILABLE),
                "current": self._bind(
                    vmindex, f"{stage}/cs_cur", fp.OVERRIDES["clocksource_current"],
                    fp.CLOCKSOURCE_CURRENT + "\n"),
            }
        if kallsyms:
            raw = self._sh(vmindex, "grep -c 'x64_sys' /proc/kallsyms 2>/dev/null").strip()
            # `grep -c` prints a number, but a shell that failed prints anything;
            # never let an unparsable value take the whole hide down.
            try:
                x64 = int(raw)
            except ValueError:
                x64 = 0
            if x64 > 0:
                applied["kallsyms"] = self._bind(
                    vmindex, f"{stage}/kallsyms", fp.OVERRIDES["kallsyms"], fp.KALLSYMS)
                applied["kallsyms"]["replaced_x64_sysymbols"] = x64
        if dmi:
            dmi_results = {}
            for key, value in fp.DMI.items():
                target = f"{fp.DMI_DIR}/{key}"
                exists = "No such" not in self._sh(vmindex, f"ls {target} 2>&1")
                if not exists:
                    continue
                dmi_results[key] = self._bind(vmindex, f"{stage}/dmi/{key}", target, value + "\n")
            applied["dmi"] = dmi_results

        applied["uname"] = {
            "covered": False,
            "note": "uname(2) is a syscall, not a file; bind-mounting /proc/version does not change it",
            "still_reports": self._sh(vmindex, "uname -m").strip(),
        }
        return applied

    def _bind(self, vmindex: int, staged: str, target: str, content: str) -> dict[str, Any]:
        """Write `content` to `staged`, then bind-mount it over `target`.

        A bind mount over an existing mount needs the fake to be a real file on a
        writable fs first, so it is staged under /data/local/tmp. `--bind` on a
        single file works on this kernel; `--rbind` would hide a whole directory and
        break tools that enumerate it.
        """
        # Write via base64 to keep newlines and tabs intact through the shell.
        import base64
        blob = base64.b64encode(content.encode()).decode()
        self._sh(vmindex, f"echo {blob} | base64 -d > {staged} 2>&1", timeout=20)
        self._sh(vmindex, f"umount {target} 2>/dev/null; mount --bind {staged} {target} 2>&1",
                 timeout=20)
        after = self._sh(vmindex, f"head -c 200 {target} 2>&1").strip()
        return {"target": target, "ok": after.startswith(content.strip()[:20]),
                "read_back": after.splitlines()[0] if after else ""}

    def deep_restore(self, vmindex: int = 0) -> dict[str, Any]:
        """Unmount every override and remove the staged fakes."""
        from . import fakeprofile as fp

        targets = list(fp.OVERRIDES.values())
        targets += [f"{fp.DMI_DIR}/{k}" for k in fp.DMI]
        unmounted = []
        for target in targets:
            out = self._sh(vmindex, f"umount {target} 2>&1").strip()
            if "not mounted" not in out.lower() and "No such" not in out:
                unmounted.append(target)
        self._sh(vmindex, f"rm -rf {fp.STAGE_DIR}", timeout=20)
        return {"unmounted": unmounted, "staged_removed": fp.STAGE_DIR}

    def _verify_fakes(self, vmindex: int) -> dict[str, Any]:
        """Read each faked file back and say which ones are actually covered."""
        from . import fakeprofile as fp

        checks = {
            "cpuinfo": (fp.OVERRIDES["cpuinfo"], "CPU implementer"),
            "osrelease": (fp.OVERRIDES["osrelease"], fp.OSRELEASE[:10]),
            "clocksource": (fp.OVERRIDES["clocksource_current"], fp.CLOCKSOURCE_CURRENT),
            "dmi_sys_vendor": (f"{fp.DMI_DIR}/sys_vendor", fp.DMI["sys_vendor"]),
        }
        out: dict[str, Any] = {}
        for name, (path, needle) in checks.items():
            value = self._sh(vmindex, f"head -c 300 {path} 2>&1").strip()
            out[name] = {"covered": needle in value, "value": value[:80]}
        # kallsyms is checked by absence: the x86 symbols must be gone.
        x64 = self._sh(vmindex, "grep -c 'x64_sys' /proc/kallsyms 2>/dev/null").strip()
        out["kallsyms"] = {"covered": x64 in {"0", ""}, "x64_symbols": x64}
        return out

    def features(self, vmindex: int = 0) -> dict[str, Any]:
        """List KernelSU's kernel features, which is where several hides live.

        They are not properties and not files — they are kernel-side switches, so
        this is the only place they show up. `selinux_hide` in particular is what
        makes a Permissive build look Enforcing to an app, and it exists only
        because KernelSU ships it; there is no equivalent to hand-roll.
        """
        out = self._sh(vmindex, "timeout 15 /data/adb/ksu/bin/ksud feature list 2>&1", timeout=25)
        features: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in out.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("[ENABLED") or stripped.startswith("[DISABLED"):
                if current:
                    features.append(current)
                # `[ENABLED (1)] su_compat (ID=0)`
                body = stripped.split("]", 1)[1].strip()
                name, _, ident = body.partition(" (ID=")
                current = {
                    "name": name.strip(),
                    "id": ident.rstrip(")") if ident else None,
                    "enabled": stripped.startswith("[ENABLED"),
                }
            elif current and line.startswith(" ") and "description" not in current:
                # The description is the indented line under the header, with no
                # `Description:` prefix — ksud prints it bare.
                current["description"] = stripped
        if current:
            features.append(current)
        return {"available": "Available Features" in out, "features": features}

    def set_feature(self, vmindex: int, name: str, enabled: bool) -> dict[str, Any]:
        """Toggle one KernelSU kernel feature and read it back."""
        if not name:
            raise BadArgument("feature name is required")
        known = {f["name"]: f for f in self.features(vmindex).get("features", [])}
        if name not in known:
            raise BadArgument(
                f"unknown KernelSU feature {name!r}",
                detail={"known": sorted(known)},
            )
        self._sh(vmindex, f"timeout 15 /data/adb/ksu/bin/ksud feature set {name} "
                          f"{'1' if enabled else '0'} 2>&1", timeout=25)
        after = {f["name"]: f for f in self.features(vmindex).get("features", [])}
        state = after.get(name, {})
        return {
            "feature": name,
            "requested": enabled,
            "applied": bool(state.get("enabled")),
            "value": state.get("value"),
            "note": "kernel-side switch; persists across reboot",
        }

    # -- hide --------------------------------------------------------------
    def hide(self, vmindex: int = 0, *, properties: bool = True, root: bool = True,
             restore: bool = False) -> dict[str, Any]:
        """Apply the reversible hides and report what changed and what cannot be closed.

        `restore=True` puts back the values read at the first hide, so this is not
        a one-way door.
        """
        state = self.root.state(vmindex)
        if not state.is_root:
            raise BadArgument("hiding needs root", detail={"fix": "mumu_root_adb(True)"})
        if not self._has_resetprop(vmindex):
            raise BadArgument(
                "no resetprop on this image; ro.* properties cannot be rewritten",
                detail={"looked_for": f"{KSU_BIN}/resetprop"},
            )

        before = self._collect_props(vmindex)
        self._applied.setdefault(vmindex, {k: before.get(k, "") for k in PROP_PROFILE})
        applied: dict[str, Any] = {}

        if restore:
            original = self._applied.get(vmindex, {})
            reverted = {}
            for key, value in original.items():
                self._resetprop(vmindex, key, value)
                reverted[key] = value
            return {"restored": reverted, "verified": self._verify_props(vmindex)}

        if properties:
            applied["properties"] = self._hide_properties(vmindex)

        # Root hiding must run before the final scan: it drops adbd to `shell`,
        # and the scan needs root to read the surface. Scanning first and hiding
        # second is what makes a re-scan impossible — but reporting the *pre-hide*
        # scan as the result is wrong too, so the half that does not need root
        # (`id`, getprop) is re-read afterwards.
        root_stage: dict[str, Any] | None = None
        if root:
            scan = self.scan(vmindex)
            root_stage = self._hide_root(vmindex)
            applied["root"] = root_stage

            props = self._collect_props(vmindex)
            refreshed = {f.name: f for f in scan.findings}
            refreshed["properties"] = self._props_finding(props)
            refreshed["vendor_markers"] = self._markers_finding(props)
            # `id` and `getenforce` work as `shell`; refresh them so the report
            # describes the state *after* the hide, not before it.
            refreshed["adbd_privilege"] = self._debug_finding(vmindex)
            refreshed["selinux"] = self._selinux_finding(vmindex)
            scan = Scan(findings=list(refreshed.values()))
        else:
            scan = self.scan(vmindex)

        return {
            "applied": applied,
            "verified": self._verify_props(vmindex),
            "surface": scan.to_dict(),
            "reversible": "mumu_stealth_hide(restore=True), or reboot",
        }

    def _resetprop(self, vmindex: int, key: str, value: str) -> str:
        # -n skips property_service and writes the property area directly, which is
        # what makes a `ro.` key writable after init has already read it.
        quoted = "" if value == "" else f"'{value}'"
        return self._sh(vmindex, f"{KSU_BIN}/resetprop -n {key} {quoted} 2>&1").strip()

    def _hide_properties(self, vmindex: int) -> dict[str, Any]:
        before = self._collect_props(vmindex)
        changes: dict[str, str] = {}
        already: dict[str, str] = {}
        for key, (expected, _why) in PROP_PROFILE.items():
            current = before.get(key, "")
            if current == expected:
                already[key] = expected
                continue
            changes[key] = expected
            self._resetprop(vmindex, key, expected)

        # Vendor-named properties: DELETE them, do not blank them. Measured:
        # `resetprop -n key ''` leaves the key in the property area, so `getprop`
        # still prints it and a `grep nemu` still hits; `resetprop -d key` removes
        # the entry outright. Blanking looked like a fix and was not one.
        deleted: list[str] = []
        kept: list[str] = []
        for key, value in before.items():
            if not value:
                continue
            if not any(marker in f"{key}={value}".lower() for marker in EMULATOR_MARKERS):
                continue
            if key in changes:
                continue
            self._sh(vmindex, f"{KSU_BIN}/resetprop -d {key} 2>&1")
            (deleted if self._getprop(vmindex, key) == "" else kept).append(key)
        return {
            "set": changes,
            "already_correct": already,
            "deleted": deleted,
            "still_present": kept,
            "note": "resetprop writes the property area directly; a reboot restores the originals",
        }

    def _hide_root(self, vmindex: int) -> dict[str, Any]:
        """Close the root surface that a property change cannot touch.

        Order matters and is not obvious: `setenforce` and `stop` both need root,
        so they run **before** `adb unroot`. Doing the unroot first left the
        remaining steps failing with `Permission denied` — the hide reported the
        SELinux change as attempted while the mode was still Permissive.

        The KernelSU directories are reported rather than moved: relocating them
        breaks KernelSU itself, and any half-move leaves a different, equally
        detectable inconsistency.
        """
        out: dict[str, Any] = {}
        was_root = "uid=0" in self._sh(vmindex, "id")

        # Privileged stages first, while adbd is still root.
        #
        # SELinux is deliberately NOT switched. Measured: `setenforce 1` on this
        # image hangs the shell past a 30 s timeout and leaves adbd `offline`
        # until the instance is restarted — MuMu's init has no enforcing policy to
        # load. It is reported as unfixable instead of attempted.
        selinux = self._sh(vmindex, "getenforce").strip()
        if selinux != "Enforcing":
            out["selinux"] = {
                "was": selinux,
                "now": selinux,
                "ok": False,
                "note": "left as-is: setenforce hangs this image and kills adbd; "
                        "a Permissive build cannot be made Enforcing at runtime",
            }

        stopped: list[str] = []
        if was_root:
            for service in NEMU_SERVICES:
                if not self._getprop(vmindex, f"init.svc.{service}"):
                    continue
                self._sh(vmindex, f"stop {service} 2>&1")
                time.sleep(0.3)
                self._sh(vmindex, f"{KSU_BIN}/resetprop -d init.svc.{service} 2>&1")
                if self._getprop(vmindex, f"init.svc.{service}") == "":
                    stopped.append(service)
        out["services_stopped"] = stopped

        # Then drop adbd. After this, nothing below can require root.
        if was_root:
            self.adb.run(vmindex, "unroot", timeout=30, connect=False)
            time.sleep(2.0)
            self.adb.invalidate(vmindex)
            out["adbd"] = {"changed": True, "now": self._sh(vmindex, "id").strip()[:60]}

        out["remaining"] = {
            "ksu_dirs": self._rootfs_finding(vmindex).detail,
            "su": self._su_finding(vmindex).detail,
            "note": "these are structural: KernelSU needs them, and a partial move is a new signal",
        }
        if stopped:
            out["services_note"] = (
                "stopped services may restart on the next boot; this is a runtime hide, "
                "not a persistent one"
            )
        return out

    def _verify_props(self, vmindex: int) -> dict[str, Any]:
        props = self._collect_props(vmindex)
        wrong = {k: props.get(k, "") for k, (want, _w) in PROP_PROFILE.items()
                 if props.get(k, "") != want}
        markers = [f"{k}={v}" for k, v in props.items()
                   if v and any(m in f"{k}={v}".lower() for m in EMULATOR_MARKERS)]
        return {"mismatched": wrong, "markers_left": markers,
                "clean": not wrong and not markers}
