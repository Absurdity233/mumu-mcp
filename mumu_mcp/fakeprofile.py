"""Per-instance content of the faked files, and the ARM profile they imitate.

The profile is a Samsung SM-S9210 (Galaxy S24, Exynos 2400 / `e1q`), because that
is what MuMu's own property set already claims — faking toward a different device
would create a new inconsistency between `ro.product.model` and the cpuinfo.

Two kinds of content:

* `cpuinfo` / `dmi` — bind-mounted over the real file. Verified to take effect for
  reads through the mount namespace.
* `sensors` — not a file override: the count comes from `sensorservice`, so the
  only lever is stopping services and adding a HIDL/AIDL shim, which this image
  does not have. Reported, not faked.

Every value here is chosen to be mutually consistent: an ARM cpuinfo that matches
`ro.product.cpu.abilist`, a DMI that matches a handset (no DMI at all would be the
honest answer for a phone, but absence is itself unusual on `ro.hardware=qcom`
builds, so a plausible vendor string is used).
"""

from __future__ import annotations

# 8 cores, as an Exynos 2400 / `e1q` reports: 1x Cortex-X4 + 2x A720 + 3x A720 + 4x A520.
_ARM_CORES = [
    ("0", "0x41", "0xd85", "Cortex-A520", "1280"),
    ("1", "0x41", "0xd85", "Cortex-A520", "1280"),
    ("2", "0x41", "0xd85", "Cortex-A520", "1280"),
    ("3", "0x41", "0xd85", "Cortex-A520", "1280"),
    ("4", "0x41", "0xd81", "Cortex-A720", "1980"),
    ("5", "0x41", "0xd81", "Cortex-A720", "1980"),
    ("6", "0x41", "0xd81", "Cortex-A720", "2600"),
    ("7", "0x41", "0xd82", "Cortex-X4", "3200"),
]

CPUINFO = None  # built by cpuinfo(); kept as a name for import symmetry


def cpuinfo() -> str:
    blocks = []
    for idx, impl, part, _name, _mhz in _ARM_CORES:
        blocks.append(
            f"processor\t: {idx}\n"
            "BogoMIPS\t: 38.40\n"
            "Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp "
            "cpuid asimdrdm lrcpc dcpop asimddp sha3 asimdfhm\n"
            f"CPU implementer\t: {impl}\n"
            "CPU architecture: 8\n"
            "CPU variant\t: 0x1\n"
            f"CPU part\t: {part}\n"
            "CPU revision\t: 0\n"
        )
    blocks.append(
        "Hardware\t: Samsung Exynos 2400\n"
        "Revision\t: 000f\n"
        "Serial\t\t: 0000000000000000\n"
    )
    return "\n".join(blocks)


# DMI for a handset is usually absent; when a build exposes it, the vendor is the
# SoC vendor rather than a hypervisor. `innotek GmbH` is the VirtualBox tell and has
# to go regardless.
DMI = {
    "sys_vendor": "Samsung Electronics Co., Ltd.",
    "product_name": "SM-S9210",
    "product_version": "SM-S9210",
    "board_vendor": "Samsung Electronics Co., Ltd.",
    "board_name": "e1q",
    "bios_vendor": "Samsung Electronics Co., Ltd.",
    "bios_version": "S9210XXU1AXBA",
    "chassis_vendor": "Samsung Electronics Co., Ltd.",
    "product_uuid": "4f5a2c31-9b6d-4e18-8a7f-2d3c5b1e9a40",
}

# Clocksource: `kvm-clock` is a hard tell. The list is what a Snapdragon/Exynos
# kernel offers; the *active* one reads `arch_sys_counter` on real ARM.
CLOCKSOURCE_AVAILABLE = "arch_sys_counter tsc hpet acpi_pm "
CLOCKSOURCE_CURRENT = "arch_sys_counter"

# Files a professional check opens. Path -> which fake applies.
OVERRIDES = {
    "cpuinfo": "/proc/cpuinfo",
    "osrelease": "/proc/sys/kernel/osrelease",
    "clocksource_available": "/sys/devices/system/clocksource/clocksource0/available_clocksource",
    "clocksource_current": "/sys/devices/system/clocksource/clocksource0/current_clocksource",
    "kallsyms": "/proc/kallsyms",
}

# A handset's kallsyms is ARM symbols; the giveaway here is the 840 `__x64_sys_*`
# entries. Replacing the file with a short, plausible ARM listing is enough for a
# check that greps for x86 symbols, and is honest about being a stub (an empty file
# would itself look wrong).
KALLSYMS = """\
ffffffff81000000 T _stext
ffffffff81000000 T _text
ffffffff81000100 t __cpu_setup
ffffffff81001000 T do_one_initcall
ffffffff81002000 T start_kernel
ffffffff81004000 T __secondary_switched
ffffffff81008000 T kasan_early_init
ffffffff8100a000 T do_error_info
ffffffff8100c000 T el0_svc
ffffffff8100e000 T el0_svc_handler
ffffffff81010000 T __arm64_sys_read
ffffffff81010200 T __arm64_sys_write
ffffffff81010400 T __arm64_sys_openat
ffffffff81010600 T __arm64_sys_close
ffffffff81010800 T __arm64_sys_ioctl
ffffffff81010a00 T __arm64_sys_mmap
"""

DMI_DIR = "/sys/class/dmi/id"
STAGE_DIR = "/data/local/tmp/.mumu_stealth"

# Kernel release a real SM-S9210 on Android 15 would report. The running kernel is
# `6.1.90-perf+`; the fake keeps the 6.1 line so a check comparing against known
# Samsung builds does not trip on an implausible jump.
OSRELEASE = "6.1.75-android14-11-g4d9e0d1b2c3f"

# The ARM `uname -m` value a real handset reports.
MACHINE = "aarch64"
