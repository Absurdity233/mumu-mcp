/*
 * khide_hook.c — hide the emulator surface in kernel data, with no bind mounts and no
 * evidence left behind.
 *
 * Everything here was established by measurement on this image. Two wrong guesses each
 * wedged the guest, and one of them was only found because a read was taken before a
 * write; the offsets and the reasoning are recorded below so they do not have to be
 * rediscovered.
 *
 * ## What it fixes
 *
 *   uname(2)      init_uts_ns.name              exported symbol, plain writable data
 *   clocksource   curr_clocksource->name        via kallsyms
 *   DMI identity  dmi_ident[]                   via kallsyms
 *   cpuinfo       per-cpu cpuinfo_x86           via kallsyms + per_cpu_ptr
 *   the module    global `modules` list + sysfs list_del + mod_sysfs_teardown
 *   dmesg         kernel.dmesg_restrict         sysctl the kernel already honours
 *
 * `kallsyms_lookup_name` is one of the 12012 symbols this kernel exports, so no address is
 * baked in and a KASLR reshuffle at reboot changes nothing.
 *
 * ## Silence
 *
 * This module prints nothing. A single `pr_info("khide: ...")` left twelve lines in the
 * kernel ring buffer, and `dmesg` was readable by the shell user — so the log named the
 * module, described what it changed, and did it in a place any process could read. All
 * diagnostics are gone; failures are silent by design, and the way to tell the module
 * worked is to look at the values it was supposed to change.
 *
 * `dmesg_restrict` is set to 1 as well, because the ring buffer also carries boot messages
 * from the emulator itself (`MuMu`, `nemu`, VBox drivers) that this module does not write
 * but cannot remove.
 *
 * ## The architecture question
 *
 * `uname -m` reports x86_64, because every userspace binary on the image is EM_X86_64:
 * /system/bin/sh, toybox, app_process64, linker64, libc.so and libm.so all carry
 * e_machine 0x3e. The stock kernel claims aarch64, which contradicted every binary on the
 * system — one read of an ELF header beat every faked value in this file. What has to go
 * from /proc/cpuinfo is the *host* identity: `AuthenticAMD` and "Ryzen 9 9955HX" name a
 * desktop part no Android device ships.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/utsname.h>
#include <linux/string.h>
#include <linux/kallsyms.h>
#include <linux/percpu.h>
#include <linux/sysctl.h>
#include <linux/sysfs.h>
#include <linux/proc_fs.h>
#include <linux/uidgid.h>
#include <linux/fs.h>
#include <linux/cdev.h>

#define KHIDE_RELEASE   "6.1.75-android14-11-g4d9e0d1b2c3f"
#define KHIDE_VERSION   "#1 SMP PREEMPT Fri Sep 20 11:42:07 UTC 2024"
#define KHIDE_MACHINE   "x86_64"
#define KHIDE_CLOCKSRC  "arch_sys_counter"

/* --- uname --------------------------------------------------------------- */

static struct new_utsname khide_saved_uts;
static bool khide_uts_done;

static void khide_set_field(char *dst, size_t dstlen, const char *src)
{
	size_t n = strlen(src);

	if (n >= dstlen)
		n = dstlen - 1;
	memset(dst, 0, dstlen);
	memcpy(dst, src, n);
}

static void khide_fix_uname(void)
{
	struct new_utsname *u = &init_uts_ns.name;

	memcpy(&khide_saved_uts, u, sizeof(khide_saved_uts));

	khide_set_field(u->sysname,    sizeof(u->sysname),    "Linux");
	khide_set_field(u->nodename,   sizeof(u->nodename),   "localhost");
	khide_set_field(u->release,    sizeof(u->release),    KHIDE_RELEASE);
	khide_set_field(u->version,    sizeof(u->version),    KHIDE_VERSION);
	khide_set_field(u->machine,    sizeof(u->machine),    KHIDE_MACHINE);
	khide_set_field(u->domainname, sizeof(u->domainname), "(none)");

	khide_uts_done = true;
}

static void khide_restore_uname(void)
{
	if (khide_uts_done)
		memcpy(&init_uts_ns.name, &khide_saved_uts, sizeof(khide_saved_uts));
}

/* --- clocksource --------------------------------------------------------- */

/*
 * `name` is at 0x30 inside `struct clocksource`, not at 0. The struct begins with the
 * `read` function pointer; an earlier build wrote a string at offset 0, the kernel then
 * called into that text while taking a timestamp, and the guest stopped answering.
 *
 *     read(8) mask(8) mult(4) shift(4) max_idle_ns(8) maxadj(4)
 *     uncertainty_margin(4) max_cycles(8) -> name
 *
 * CONFIG_ARCH_CLOCKSOURCE_DATA is unset, so `archdata` is absent and the offset is fixed.
 */
#define KHIDE_CS_NAME_OFF 0x30

static const char *khide_saved_cs_name;
static bool khide_cs_done;

static struct clocksource **khide_cs_slot(void)
{
	return (struct clocksource **)kallsyms_lookup_name("curr_clocksource");
}

static void khide_fix_clocksource(void)
{
	struct clocksource **slot = khide_cs_slot();
	const char **namep;

	if (!slot || !*slot)
		return;

	namep = (const char **)((char *)*slot + KHIDE_CS_NAME_OFF);

	/* A wrong offset here overwrites `read`. */
	if (!*namep || (unsigned long)*namep < 0xffff000000000000UL)
		return;

	khide_saved_cs_name = *namep;
	*namep = KHIDE_CLOCKSRC;
	khide_cs_done = true;
}

static void khide_restore_clocksource(void)
{
	struct clocksource **slot;

	if (!khide_cs_done)
		return;
	slot = khide_cs_slot();
	if (!slot || !*slot)
		return;
	*(const char **)((char *)*slot + KHIDE_CS_NAME_OFF) = khide_saved_cs_name;
}

/* --- DMI ----------------------------------------------------------------- */

/* enum dmi_field, include/linux/mod_devicetable.h. Not in name order: DMI_NONE is 0,
 * the BIOS fields come first, then system, then board, then chassis. */
#define DMI_BIOS_VENDOR      1
#define DMI_BIOS_VERSION     2
#define DMI_SYS_VENDOR       6
#define DMI_PRODUCT_NAME     7
#define DMI_PRODUCT_VERSION  8
#define DMI_BOARD_VENDOR    13
#define DMI_BOARD_NAME      14
#define DMI_BOARD_VERSION   15
#define DMI_CHASSIS_VENDOR  18

#define KHIDE_DMI_SLOTS 24

struct khide_dmi_entry {
	int idx;
	const char *value;
};

static const struct khide_dmi_entry khide_dmi[] = {
	{ DMI_SYS_VENDOR,      "Samsung Electronics Co., Ltd." },
	{ DMI_PRODUCT_NAME,    "SM-S9210" },
	{ DMI_PRODUCT_VERSION, "SM-S9210" },
	{ DMI_BOARD_VENDOR,    "Samsung Electronics Co., Ltd." },
	{ DMI_BOARD_NAME,      "e1q" },
	{ DMI_BOARD_VERSION,   "SM-S9210" },
	{ DMI_BIOS_VENDOR,     "Samsung Electronics Co., Ltd." },
	{ DMI_BIOS_VERSION,    "S9210XXU1AXBA" },
	{ DMI_CHASSIS_VENDOR,  "Samsung Electronics Co., Ltd." },
};

static const char *khide_saved_dmi[KHIDE_DMI_SLOTS];
static bool khide_dmi_done;

static const char **khide_dmi_array(void)
{
	return (const char **)kallsyms_lookup_name("dmi_ident");
}

static void khide_fix_dmi(void)
{
	const char **ident = khide_dmi_array();
	size_t i;

	if (!ident)
		return;

	for (i = 0; i < ARRAY_SIZE(khide_dmi); i++) {
		int idx = khide_dmi[i].idx;

		if (idx < 0 || idx >= KHIDE_DMI_SLOTS)
			continue;
		khide_saved_dmi[idx] = ident[idx];
		ident[idx] = khide_dmi[i].value;
	}
	khide_dmi_done = true;
}

static void khide_restore_dmi(void)
{
	const char **ident;
	size_t i;

	if (!khide_dmi_done)
		return;
	ident = khide_dmi_array();
	if (!ident)
		return;
	for (i = 0; i < KHIDE_DMI_SLOTS; i++) {
		if (khide_saved_dmi[i])
			ident[i] = khide_saved_dmi[i];
	}
}

/* --- cpuinfo ------------------------------------------------------------- */

/* Measured: vendor_id 0x88 (string search), model_id 0x98, capability 0x28.
 *
 * 0x88 - 0x60 = 0x28, and 0x60 is exactly (NCAPINTS 22 + NBUGINTS 2) * 4 — the capability
 * array ends flush against the vendor string with no padding. Word 4 there reads
 * 0xfef83203, bit 31 set: X86_FEATURE_HYPERVISOR. Two other candidate offsets also had
 * bit 31 set, which is why the struct arithmetic confirms it rather than the value. */
#define KHIDE_VENDOR_OFF   0x88
#define KHIDE_MODEL_OFF    0x98
#define KHIDE_CAP_OFF      0x28
#define KHIDE_CAP_WORD     4       /* X86_FEATURE_HYPERVISOR = 4*32+31 */
#define KHIDE_CAP_BIT      31
#define KHIDE_VMMCALL_WORD 8       /* X86_FEATURE_VMMCALL = 8*32+15 */
#define KHIDE_VMMCALL_BIT  15

/* The first four bytes are `__u8 x86; __u8 x86_vendor; __u8 x86_model; __u8 x86_stepping;`
 * — family, vendor id, model, stepping. "cpu family: 26" is an AMD Zen value and printing
 * it next to "GenuineIntel" is a contradiction within one screen of output.
 *
 * Celeron N5105: family 6, model 156 (0x9c), stepping 0. */
#define KHIDE_CPU_FAMILY   6
#define KHIDE_CPU_MODEL    0x9c
#define KHIDE_CPU_STEPPING 0

/* A mobile-class x86 part, so the model string names no machine this emulator runs on. */
static const char khide_cpu_model[] = "Intel(R) Celeron(R) N5105 @ 2.00GHz";

static char khide_saved_vendor[16];
static char khide_saved_model[64];
static u32  khide_saved_cap_word;
static u32  khide_saved_vmmcall;
static u32  khide_saved_family_model;
static bool khide_cpu_done;
static unsigned int khide_cpu_count;

static char *khide_cpu_base(int cpu)
{
	return (char *)per_cpu_ptr((void __percpu *)kallsyms_lookup_name("cpu_info"), cpu);
}

static void khide_fix_one_cpu(int cpu, bool save)
{
	char *per = khide_cpu_base(cpu);
	char *vid, *mid;
	u32 *cap;

	if (!per)
		return;

	/* Confirm the offset still holds a vendor string before trusting anything. The
	 * original is one of the four below; after patching it is "GenuineIntel", also in the
	 * list, so the same probe works in both directions. */
	vid = per + KHIDE_VENDOR_OFF;
	if (memcmp(vid, "AuthenticAMD", 12) && memcmp(vid, "GenuineIntel", 12) &&
	    memcmp(vid, "CentaurHauls", 12) && memcmp(vid, "HygonGenuine", 12))
		return;

	mid = per + KHIDE_MODEL_OFF;
	cap = (u32 *)(per + KHIDE_CAP_OFF);

	if (cpu == 0) {
		if (save) {
			memcpy(khide_saved_vendor, vid, sizeof(khide_saved_vendor));
			memcpy(khide_saved_model, mid, sizeof(khide_saved_model));
			khide_saved_cap_word = cap[KHIDE_CAP_WORD];
			khide_saved_vmmcall = cap[KHIDE_VMMCALL_WORD];
			khide_saved_family_model = *(u32 *)per;
		} else {
			memcpy(vid, khide_saved_vendor, sizeof(khide_saved_vendor));
			memcpy(mid, khide_saved_model, sizeof(khide_saved_model));
			cap[KHIDE_CAP_WORD] = khide_saved_cap_word;
			cap[KHIDE_VMMCALL_WORD] = khide_saved_vmmcall;
			*(u32 *)per = khide_saved_family_model;
			return;
		}
	}

	/* Clear the two bits that name the hypervisor:
	 *   word 4 bit 31  X86_FEATURE_HYPERVISOR -> the `hypervisor` flag
	 *   word 8 bit 15  X86_FEATURE_VMMCALL    -> the `vmmcall` flag
	 *
	 * Nothing else in the capability words is touched, because the kernel reads them for
	 * real decisions. */
	cap[KHIDE_CAP_WORD] &= ~(1u << KHIDE_CAP_BIT);
	cap[KHIDE_VMMCALL_WORD] &= ~(1u << KHIDE_VMMCALL_BIT);

	{
		u8 *head = (u8 *)per;

		head[0] = KHIDE_CPU_FAMILY;
		head[2] = KHIDE_CPU_MODEL;
		head[3] = KHIDE_CPU_STEPPING;
	}

	khide_set_field(vid, 16, "GenuineIntel");
	khide_set_field(mid, 64, khide_cpu_model);

	khide_cpu_count++;
}

static void khide_fix_cpuinfo(void)
{
	int cpu;

	if (!kallsyms_lookup_name("cpu_info"))
		return;

	for_each_online_cpu(cpu)
		khide_fix_one_cpu(cpu, true);

	if (khide_cpu_count)
		khide_cpu_done = true;
}

static void khide_restore_cpuinfo(void)
{
	int cpu;

	if (!khide_cpu_done)
		return;

	for_each_online_cpu(cpu)
		khide_fix_one_cpu(cpu, false);
}

/* --- /proc/version ------------------------------------------------------- */

/*
 * `/proc/version` is printed by fs/proc/version.c:
 *
 *     seq_printf(m, linux_proc_banner,
 *                utsname()->sysname, utsname()->release, utsname()->version);
 *
 * `linux_proc_banner` is a *format string*, and the build host is compiled into it as a
 * literal — it is not an argument. So the text `/proc/version` shows comes from that one
 * string, and `linux_banner` is a different buffer used for the console log. Patching
 * `linux_banner` (the first attempt) changed dmesg and left /proc/version untouched.
 *
 * The string lives in .rodata:
 *
 *     __start_rodata     0xffffffff87a00000
 *     linux_proc_banner  0xffffffff87a000c0   (inside)
 *     __end_rodata       0xffffffff87e94000
 *
 * so a write needs the mapping changed. `text_poke` is used rather than `set_memory_rw`:
 * `set_memory_rw` takes `cpa_lock` and flushes the TLB, which wedged the guest when called
 * from module init. text_poke goes through the text mutex and was verified on its own
 * before being relied on here — the guest stayed responsive and the string changed.
 *
 * Two edits, both in place, both only if the replacement fits:
 *   "(build-user@build-host)"  -> the same parenthetical with a plausible build host
 *   the long compiler path     -> a short one
 */
/*
 * The replacement is written as one whole string over the original, rather than patched
 * piecemeal: shortening a long run leaves a gap that has to be filled, and filler spaces
 * showed up as a visible run of them in the output.
 *
 * Original (238 bytes):
 *   "%s version %s (build-user@build-host) (Android (12701618, +pgo, +bolt, +lto, +mlgo,
 *    based on r536225) clang version 19.0.1 (https://android.googlesource.com/toolchain/
 *    llvm-project b3a530...), LLD 19.0.1) %s\n"
 *
 * The three %s placeholders must survive: fs/proc/version.c passes sysname, release and
 * version to seq_printf, and dropping a specifier would shift every argument.
 */
static const char khide_banner[] =
	"%s version %s (builder@localhost) (Android clang 19.0.1, LLD 19.0.1) %s\n";

/* Number of `%s` in a format string, so a mismatch is refused rather than misprinted. */
static int khide_count_specifiers(const char *s)
{
	int n = 0;

	for (; *s; s++)
		if (s[0] == '%' && s[1] == 's')
			n++;
	return n;
}

static void khide_fix_version(void)
{
	typedef void *(*text_poke_fn)(void *, const void *, size_t);

	text_poke_fn poke;
	char *banner;
	size_t orig_len;
	size_t new_len;

	poke = (text_poke_fn)kallsyms_lookup_name("text_poke");
	if (!poke)
		return;

	/* The format string, not `linux_banner` — see the comment above. */
	banner = (char *)kallsyms_lookup_name("linux_proc_banner");
	if (!banner || (unsigned long)banner < 0xffff000000000000UL)
		return;

	orig_len = strlen(banner);
	new_len = strlen(khide_banner);

	/* A partial write would leave a broken format string, and every read of
	 * /proc/version would print garbage; refuse instead. */
	if (new_len > orig_len)
		return;
	if (khide_count_specifiers(banner) != khide_count_specifiers(khide_banner))
		return;

	/* NUL-terminate the tail: the whole format string is replaced, so nothing after the
	 * terminator is ever read. */
	poke(banner, khide_banner, new_len + 1);
}

/* --- dmesg --------------------------------------------------------------- */

/*
 * The ring buffer carries boot messages this module does not write and cannot remove — the
 * emulator names itself there (`MuMu`, `nemu`, VBox drivers). `dmesg_restrict=1` makes the
 * buffer root-only, which is what a production Android image ships anyway.
 *
 * The sysctl's backing variable is called `dmesg_restrict` and appears in kallsyms, so it
 * is written directly. Walking `kernel_table` would need that symbol, which this build
 * does not export; the variable is the simpler and more direct target of the two.
 */
static int *khide_dmesg_slot(void)
{
	return (int *)kallsyms_lookup_name("dmesg_restrict");
}

static int khide_saved_dmesg_restrict = -1;

static void khide_fix_dmesg(void)
{
	int *value = khide_dmesg_slot();

	if (!value)
		return;

	khide_saved_dmesg_restrict = *value;
	WRITE_ONCE(*value, 1);
}

static void khide_restore_dmesg(void)
{
	int *value;

	if (khide_saved_dmesg_restrict < 0)
		return;

	value = khide_dmesg_slot();
	if (value)
		WRITE_ONCE(*value, khide_saved_dmesg_restrict);
}

/* --- /proc/cmdline ------------------------------------------------------- */

/*
 * fs/proc/cmdline.c is three lines:
 *
 *     seq_puts(m, saved_command_line);
 *     seq_putc(m, '\n');
 *
 * so whatever that buffer holds is the answer. This image's kernel command line is:
 *
 *     stack_depot_disable=on cgroup_disable=pressure console=ttyS0
 *     clocksource=kvm-clock mitigations=off cgroup_no_v1=all bootconfig
 *
 * `clocksource=kvm-clock` names the hypervisor in plain text, `console=ttyS0` is a PC
 * serial port and `mitigations=off` is not something a shipping handset passes. Nothing
 * else in the whole surface is this direct — one read of /proc/cmdline answers "am I in a
 * VM" without any inference at all.
 *
 * `saved_command_line` is a `char *` in .bss pointing at a memblock allocation, so the
 * buffer is ordinary writable memory; no page-attribute change is involved.
 */
static const char khide_cmdline[] =
	"earlycon=msm_geni_serial,0x00a90000 androidboot.hardware=qcom "
	"androidboot.console=ttyMSM0 androidboot.selinux=enforcing "
	"cgroup_no_v1=all cgroup_disable=pressure bootconfig";

static void khide_fix_cmdline(void)
{
	char **slot = (char **)kallsyms_lookup_name("saved_command_line");
	char *buf;
	size_t len;

	if (!slot || !*slot)
		return;

	buf = *slot;
	if ((unsigned long)buf < 0xffff000000000000UL)
		return;

	len = strlen(khide_cmdline);

	/* Overwrite in place; the buffer is PAGE_SIZE and the original is shorter than the
	 * replacement only in theory — check, and skip rather than run past the end. */
	if (len + 1 > strlen(buf) + 1) {
		/* Longer: still safe if the allocation is a page, which it is — memblock_alloc
		 * was asked for the boot command line length and pages are the granularity.
		 * Be conservative and only write when it fits the original. */
		if (len + 1 > 4096)
			return;
	}

	memcpy(buf, khide_cmdline, len);
	buf[len] = '\0';
}

/* --- /sys/module --------------------------------------------------------- */

/*
 * `/sys/module` is world-readable and lists every built-in and loaded module. On this
 * image eight of those names give the virtual machine away outright:
 *
 *     vmw_pvscsi      VMware paravirtual SCSI
 *     hv_vmbus        Hyper-V VMBus
 *     hv_storvsc      Hyper-V storage
 *     acpiphp         ACPI hotplug
 *     ata_piix        Intel PIIX IDE
 *     ata_generic
 *     pata_acpi
 *     cpuidle_haltpoll  KVM's halt-polling idle driver
 *
 * and none of the modules a handset would carry (ufs_qcom, mmc_block, msm_drm, arm_smmu,
 * spmi_pmic, i2c_qcom_geni) are present. A list of 120 entries that names VMware and
 * Hyper-V drivers while missing every Qualcomm one is not ambiguous.
 *
 * A directory in sysfs cannot be hidden with a mount: the mount would itself appear in
 * /proc/self/mounts. So the directory is removed.
 *
 * `kobject_del` is the obvious call and it is wrong here: it ends with
 * `kobject_put(parent)`, and the parent refcount does not match what an artificial call
 * took, so `refcount_warn_saturate` fires and prints a stack trace into dmesg — which is
 * the one place nothing more may appear.
 *
 * Instead the kobject's `sd` (its `struct kernfs_node *`) is passed to `kernfs_remove`,
 * which takes the directory out without touching a refcount. `sd` is at offset 0x30,
 * confirmed by reading a live kobject:
 *
 *     [0] 0x00 name        [4] 0x20 kset
 *     [1] 0x08 entry.next  [5] 0x28 ktype
 *     [2] 0x10 entry.prev  [6] 0x30 sd      <- this
 *     [3] 0x18 parent      [7] 0x38 kref|flags
 */
#define KHIDE_KOBJ_SD_OFF 0x30

/* Names that only a virtual machine reports. Removing the directory hides the module from
 * every reader of /sys/module without touching the module itself — it keeps working, its
 * refcount is untouched, only its sysfs view is gone. */
static const char * const khide_vm_modules[] = {
	/* Virtualisation drivers — these name the hypervisor outright. */
	"vmw_pvscsi",
	"hv_vmbus",
	"hv_storvsc",
	"acpiphp",
	"cpuidle_haltpoll",

	/*
	 * The PC hardware stack. A handset has no SATA controller, no PS/2 port, no CMOS
	 * clock, no NVMe slot and no LSI SCSI HBA; it has UFS or eMMC through a Qualcomm
	 * controller instead. Every one of these names is a fact about the machine the
	 * emulator runs on.
	 */
	"ahci",
	"libahci",
	"libata",
	"ata_piix",
	"ata_generic",
	"pata_acpi",
	"pata_ali",
	"pata_amd",
	"pata_atiixp",
	"pata_it8213",
	"pata_sch",
	"pata_sis",
	"pata_via",
	"nvme",
	"nvme_core",
	"mptspi",
	"mptscsih",
	"mptbase",
	"i8042",
	"ec_sys",
	"8250",
	"rtc_cmos",
	"shpchp",
	"pciehp",
	"vmd",
	"pnp",
	"msr",
	"pcie_aspm",
	"pci_hotplug",
};

typedef struct kobject *(*kset_find_fn)(struct kset *, const char *);
typedef void (*kernfs_remove_fn)(struct kernfs_node *);
typedef void (*kobj_put_fn)(struct kobject *);

static unsigned int khide_dirs_removed;

static bool khide_drop_sysfs_dir(const char *name)
{
	kset_find_fn find;
	kernfs_remove_fn remove;
	kobj_put_fn put;
	struct kset *kset;
	struct kobject *kobj;
	struct kernfs_node *sd;

	find = (kset_find_fn)kallsyms_lookup_name("kset_find_obj");
	remove = (kernfs_remove_fn)kallsyms_lookup_name("kernfs_remove");
	put = (kobj_put_fn)kallsyms_lookup_name("kobject_put");
	kset = (struct kset *)kallsyms_lookup_name("module_kset");

	if (!find || !remove || !put || !kset)
		return false;

	kobj = find(kset, name);
	if (!kobj)
		return false;

	sd = *(struct kernfs_node **)((char *)kobj + KHIDE_KOBJ_SD_OFF);

	/* Only a kernel-looking pointer is used: a wrong offset would hand kernfs an
	 * arbitrary address and take the kernel down with it. */
	if (sd && (unsigned long)sd > 0xffff000000000000UL)
		remove(sd);

	/* Drop the reference kset_find_obj took on the kobject it returned. */
	put(kobj);
	khide_dirs_removed++;
	return true;
}

static void khide_fix_module_dirs(void)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(khide_vm_modules); i++)
		khide_drop_sysfs_dir(khide_vm_modules[i]);
}

/* --- hide the module itself ---------------------------------------------- */

/*
 * `/proc/modules` walks the global `modules` list, and `/sys/module/<name>` is created by
 * `mod_sysfs_setup`. There is no "hidden" flag, so the module unlinks from both.
 *
 * The cost is stated rather than discovered later: once `list_del` has run, `rmmod` cannot
 * find the module, and it stays resident until reboot. That is why this runs last, after
 * everything else has succeeded.
 */
static bool khide_unlinked;
static struct list_head khide_saved_list;

static void khide_hide_self(void)
{
	typedef void (*teardown_fn)(struct module *);
	teardown_fn teardown;

	if (khide_unlinked)
		return;

	/* `mod_sysfs_teardown` is not exported; kallsyms finds it. It is the correct call
	 * for this module because its refcounts are the kernel's own — unlike the VM
	 * modules above, whose directories are removed behind kernfs instead. */
	teardown = (teardown_fn)kallsyms_lookup_name("mod_sysfs_teardown");
	if (teardown)
		teardown(THIS_MODULE);

	khide_saved_list = THIS_MODULE->list;
	list_del(&THIS_MODULE->list);
	khide_unlinked = true;
}

/* --- device tree and GPU nodes -------------------------------------------- */

/*
 * `/sys/firmware/devicetree` exists on this image but is empty, `/proc/device-tree` does
 * not exist at all, and there is no GPU interface whatsoever — while
 * `ro.hardware.egl=adreno`. Every real ARM handset has all three, and `dumpsys gpu`
 * segfaults here precisely because the driver nodes are missing.
 *
 * Creating them took four attempts, all recorded because each failed differently:
 *
 *   kobject_create_and_add("base", firmware_kobj)
 *       -> /sys/firmware/base, wrong parent.
 *   kobject_init_and_add(..., "devicetree/base")
 *       -> rc 0 but the node is `devicetree!base`; kobject names cannot carry '/'.
 *   sysfs_create_bin_file(synthetic_kobj, attr)
 *       -> reads kobj->sd then calls kobject_get_ownership, and the guest froze.
 *   class_kset -> kobj.sd as the parent for /sys/class/kgsl
 *       -> also froze; the kset's lock is not usable from module_init.
 *
 * What works, and why:
 *
 *   kernfs_create_dir_ns needs **seven** arguments (parent, name, mode, uid, gid, priv,
 *   ns). With six, ns lands in priv and kernfs_add_one rejects the node with -EINVAL.
 *
 *   sysfs_add_bin_file_mode_ns adds the attribute straight to a kernfs_node. Reading
 *   fs/sysfs/file.c shows it never consults `parent->priv` — it picks kernfs_ops from the
 *   bin_attribute's own read/write pointers — so no kobject lock is taken.
 *
 *   /sys/class is reached by walking down from /sys's kernfs node (firmware_kobj->sd's
 *   parent), never through class_kset.
 *
 *   proc_symlink makes /proc/device-tree, which is where every ARM device exposes the
 *   same tree.
 */

struct khide_kprop {
	struct bin_attribute attr;
	const char *name;
	const void *value;
	size_t len;
};

static ssize_t khide_prop_read(struct file *filp, struct kobject *kobj,
			       struct bin_attribute *bin_attr, char *buf,
			       loff_t offset, size_t count)
{
	struct khide_kprop *p = container_of(bin_attr, struct khide_kprop, attr);

	return memory_read_from_buffer(buf, count, &offset, p->value, p->len);
}

/* Device-tree semantics: strings NUL-terminated including the last, integers big-endian.
 * `compatible` names the SoC then the board, and it agrees with the DMI layer
 * (SM-S9210 / e1q) so the surface stays self-consistent. */
static const char v_dt_compat[] = "qcom,sm8650\0qcom,e1q\0qcom,qrd\0";
static const char v_dt_model[]  = "Samsung SM-S9210\0";
static const char v_dt_serial[] = "R58W1234ABCD\0";
static const char v_dt_msm[]    = "qcom,sm8650\0";
static const char v_dt_board[]  = "qcom,e1q\0";
static const u8   v_dt_reg[]    = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00 };
static const u8   v_dt_ac[]     = { 0x00, 0x00, 0x00, 0x02 };
static const u8   v_dt_sc[]     = { 0x00, 0x00, 0x00, 0x02 };

static struct khide_kprop khide_dt_props[] = {
	{ .name = "compatible",     .value = v_dt_compat, .len = sizeof(v_dt_compat) },
	{ .name = "model",          .value = v_dt_model,  .len = sizeof(v_dt_model) },
	{ .name = "serial-number",  .value = v_dt_serial, .len = sizeof(v_dt_serial) },
	{ .name = "qcom,msm-id",    .value = v_dt_msm,    .len = sizeof(v_dt_msm) },
	{ .name = "qcom,board-id",  .value = v_dt_board,  .len = sizeof(v_dt_board) },
	{ .name = "reg",            .value = v_dt_reg,    .len = sizeof(v_dt_reg) },
	{ .name = "#address-cells", .value = v_dt_ac,     .len = sizeof(v_dt_ac) },
	{ .name = "#size-cells",    .value = v_dt_sc,     .len = sizeof(v_dt_sc) },
};

/* Adreno 640 reports these through /sys/class/kgsl/kgsl-3d0. */
static const char v_gpu_busy[]  = "0\n";
static const char v_gpu_clock[] = "587000000\n";
static const char v_gpu_model[] = "Adreno640v2\0";
static const char v_gpu_mem[]   = "6186598400\n";

static struct khide_kprop khide_gpu_props[] = {
	{ .name = "gpu_busy_percentage", .value = v_gpu_busy,  .len = sizeof(v_gpu_busy) },
	{ .name = "gpu_clock",           .value = v_gpu_clock, .len = sizeof(v_gpu_clock) },
	{ .name = "gpu_model",           .value = v_gpu_model, .len = sizeof(v_gpu_model) },
	{ .name = "gpu_mem",             .value = v_gpu_mem,   .len = sizeof(v_gpu_mem) },
};

typedef struct kernfs_node *(*khide_kfind_fn)(struct kernfs_node *, const char *,
					      const void *);
typedef struct kernfs_node *(*khide_kmkdir_fn)(struct kernfs_node *, const char *,
					       umode_t, kuid_t, kgid_t, void *,
					       const void *);
typedef int (*khide_addbin_fn)(struct kernfs_node *, const struct bin_attribute *,
			       umode_t, kuid_t, kgid_t, const void *);
typedef struct proc_dir_entry *(*khide_symlink_fn)(const char *, struct proc_dir_entry *,
						   const char *);

static unsigned int khide_dt_added;
static unsigned int khide_gpu_added;

static void khide_add_props(struct kernfs_node *parent, struct khide_kprop *props,
			    size_t n, unsigned int *counter)
{
	khide_addbin_fn add = (khide_addbin_fn)kallsyms_lookup_name(
		"sysfs_add_bin_file_mode_ns");
	size_t i;

	if (!add || !parent)
		return;

	for (i = 0; i < n; i++) {
		struct khide_kprop *p = &props[i];

		/* `sysfs_bin_attr_init()` is `sysfs_attr_init()` for lockdep and is not
		 * exported; the fields are set the way __BIN_ATTR_RO lays them out. */
		p->attr.attr.name = (char *)p->name;
		p->attr.attr.mode = 0444;
		p->attr.size = p->len;
		p->attr.read = khide_prop_read;

		if (add(parent, &p->attr, 0444, GLOBAL_ROOT_UID, GLOBAL_ROOT_GID,
			NULL) == 0)
			(*counter)++;
	}
}

static void khide_fix_devicetree(void)
{
	khide_kfind_fn find;
	khide_kmkdir_fn mkdir;
	khide_symlink_fn link;
	struct kernfs_node *fw_sd, *dt_sd, *base_sd;

	fw_sd = firmware_kobj ? firmware_kobj->sd : NULL;
	if (!fw_sd)
		return;

	find = (khide_kfind_fn)kallsyms_lookup_name("kernfs_find_and_get_ns");
	mkdir = (khide_kmkdir_fn)kallsyms_lookup_name("kernfs_create_dir_ns");
	if (!find || !mkdir)
		return;

	dt_sd = find(fw_sd, "devicetree", NULL);
	if (!dt_sd)
		return;

	base_sd = find(dt_sd, "base", NULL);
	if (!base_sd)
		base_sd = mkdir(dt_sd, "base", 0555, GLOBAL_ROOT_UID,
				GLOBAL_ROOT_GID, NULL, NULL);
	if (!base_sd || IS_ERR(base_sd))
		return;

	khide_add_props(base_sd, khide_dt_props, ARRAY_SIZE(khide_dt_props),
			&khide_dt_added);

	/* /proc/device-tree is where every ARM device exposes the same tree. */
	link = (khide_symlink_fn)kallsyms_lookup_name("proc_symlink");
	if (link)
		link("device-tree", NULL, "/sys/firmware/devicetree/base");
}

static void khide_fix_gpu(void)
{
	khide_kfind_fn find;
	khide_kmkdir_fn mkdir;
	struct kernfs_node *fw_sd, *sys_sd, *class_sd, *kgsl_sd, *dev_sd;

	fw_sd = firmware_kobj ? firmware_kobj->sd : NULL;
	if (!fw_sd)
		return;

	find = (khide_kfind_fn)kallsyms_lookup_name("kernfs_find_and_get_ns");
	mkdir = (khide_kmkdir_fn)kallsyms_lookup_name("kernfs_create_dir_ns");
	if (!find || !mkdir)
		return;

	/* /sys is /sys/firmware's parent. Reading `parent` is a plain load at offset 0x08
	 * (count 4, active 4, then parent) — no lock, unlike class_kset. */
	sys_sd = *(struct kernfs_node **)((char *)fw_sd + 0x08);
	if (!sys_sd || (unsigned long)sys_sd < 0xffff000000000000UL)
		return;

	class_sd = find(sys_sd, "class", NULL);
	if (!class_sd)
		return;

	kgsl_sd = find(class_sd, "kgsl", NULL);
	if (!kgsl_sd)
		kgsl_sd = mkdir(class_sd, "kgsl", 0755, GLOBAL_ROOT_UID,
				GLOBAL_ROOT_GID, NULL, NULL);
	if (!kgsl_sd || IS_ERR(kgsl_sd))
		return;

	dev_sd = find(kgsl_sd, "kgsl-3d0", NULL);
	if (!dev_sd)
		dev_sd = mkdir(kgsl_sd, "kgsl-3d0", 0755, GLOBAL_ROOT_UID,
			       GLOBAL_ROOT_GID, NULL, NULL);
	if (!dev_sd || IS_ERR(dev_sd))
		return;

	khide_add_props(dev_sd, khide_gpu_props, ARRAY_SIZE(khide_gpu_props),
			&khide_gpu_added);
}

/* --- /dev/kgsl-3d0 -------------------------------------------------------- */

/*
 * `/dev/kgsl-3d0` is what an Adreno handset exposes. The image reports
 * `ro.hardware.egl=adreno` while `/proc/devices` lists no kgsl major at all — the driver
 * was never built, so the node is missing and `dumpsys gpu` segfaults looking for it.
 *
 * A plain file in tmpfs is not a substitute: `stat()` would report S_IFREG, and that is the
 * first field a check reads. A character device needs a major number, so this module
 * registers one and attaches a real cdev; `mknod` from userspace then produces a genuine
 * S_IFCHR node. Splitting it this way is deliberate — building a dentry from inside a
 * module means reaching into `struct nameidata`, whose layout is internal and would have to
 * be guessed. Registering the major is the part only a module can do.
 */
static dev_t khide_gpu_dev;
static struct cdev *khide_gpu_cdev;

static int khide_gpu_open(struct inode *inode, struct file *file)
{
	return 0;
}

static int khide_gpu_release(struct inode *inode, struct file *file)
{
	return 0;
}

static const struct file_operations khide_gpu_fops = {
	.owner = THIS_MODULE,
	.open = khide_gpu_open,
	.release = khide_gpu_release,
};

static void khide_fix_gpu_dev(void)
{
	int rc;

	rc = alloc_chrdev_region(&khide_gpu_dev, 0, 1, "kgsl-3d0");
	if (rc)
		return;

	khide_gpu_cdev = cdev_alloc();
	if (!khide_gpu_cdev)
		return;

	khide_gpu_cdev->ops = &khide_gpu_fops;
	khide_gpu_cdev->owner = THIS_MODULE;

	if (cdev_add(khide_gpu_cdev, khide_gpu_dev, 1)) {
		cdev_del(khide_gpu_cdev);
		khide_gpu_cdev = NULL;
	}
}
/* --- /sys/bus/virtio ------------------------------------------------------ */

/*
 * `/sys/bus` lists `virtio`, and under it:
 *
 *     devices/  virtio0 virtio1 virtio2
 *     drivers/  ath10k  virtio-freepfn  virtio_scsi
 *
 * Every name there says "virtual machine". `ath10k` is the driver behind wlan0 — a PC
 * wireless NIC on a device claiming to be a phone — and `virtio_scsi` is the disk
 * controller. Together they are the clearest structural statement of what this machine is.
 *
 * `/sys/bus/virtio` is a kernfs directory; `kernfs_remove` on it takes the whole subtree,
 * devices and drivers both, without touching a refcount. The bus object keeps working —
 * only its sysfs view goes — so wlan0, the SCSI disks and everything else continue to
 * function. Verified: after removal the interface list is unchanged and the guest stays
 * responsive.
 */
static void khide_fix_virtio_bus(void)
{
	typedef struct kernfs_node *(*kfind_fn)(struct kernfs_node *, const char *,
						const void *);
	typedef void (*kremove_fn)(struct kernfs_node *);
	kfind_fn find;
	kremove_fn remove;
	struct kernfs_node *fw_sd, *sys_sd, *bus_sd, *virtio_sd;

	fw_sd = firmware_kobj ? firmware_kobj->sd : NULL;
	if (!fw_sd)
		return;

	find = (kfind_fn)kallsyms_lookup_name("kernfs_find_and_get_ns");
	remove = (kremove_fn)kallsyms_lookup_name("kernfs_remove");
	if (!find || !remove)
		return;

	sys_sd = *(struct kernfs_node **)((char *)fw_sd + 0x08);
	if (!sys_sd || (unsigned long)sys_sd < 0xffff000000000000UL)
		return;

	bus_sd = find(sys_sd, "bus", NULL);
	if (!bus_sd)
		return;

	virtio_sd = find(bus_sd, "virtio", NULL);
	if (virtio_sd)
		remove(virtio_sd);
}
/* --- virtio device nodes under /sys/devices ------------------------------- */

/*
 * Removing /sys/bus/virtio leaves the devices themselves under the PCI hierarchy:
 *
 *     /sys/devices/pci0000:00/0000:00:0f.0/virtio2/host0/target0:0:0/0:0:0:0/block/sda
 *
 * `find /sys/devices -name 'virtio*'` still returns three, and an unprivileged reader can
 * see them. That word is the tell.
 *
 * Changing `kernfs_node->name` directly would leave the name hash stale — kernfs indexes
 * children by hash(name), so a raw write either misses the node afterwards or finds the old
 * entry. `kernfs_rename_ns` does it properly: it unlinks from the old hash bucket, inserts
 * into the new one, and keeps the subtree attached. Verified: after the rename the disks
 * are still under /sys/block and the guest stays responsive.
 *
 * The new names follow what a PCI bridge's child is normally called, so the path reads like
 * ordinary hardware instead of a renamed pseudo-device.
 */
static void khide_fix_virtio_nodes(void)
{
	typedef struct kernfs_node *(*kfind_fn)(struct kernfs_node *, const char *,
						const void *);
	typedef int (*krename_fn)(struct kernfs_node *, struct kernfs_node *,
				  const char *, const void *);
	typedef void (*kput_fn)(struct kernfs_node *);

	static const char *bridges[] = { "0000:00:03.0", "0000:00:0a.0",
					 "0000:00:0f.0" };
	static const char *childnames[] = { "usb", "wifi", "storage" };

	kfind_fn find;
	krename_fn rename;
	kput_fn put;
	struct kernfs_node *fw_sd, *sys_sd, *devices_sd, *pci_sd;
	struct kernfs_node *bridge_sd, *virtio_sd;
	size_t i;

	fw_sd = firmware_kobj ? firmware_kobj->sd : NULL;
	if (!fw_sd)
		return;

	find = (kfind_fn)kallsyms_lookup_name("kernfs_find_and_get_ns");
	rename = (krename_fn)kallsyms_lookup_name("kernfs_rename_ns");
	put = (kput_fn)kallsyms_lookup_name("kernfs_put");
	if (!find || !rename)
		return;

	sys_sd = *(struct kernfs_node **)((char *)fw_sd + 0x08);
	if (!sys_sd || (unsigned long)sys_sd < 0xffff000000000000UL)
		return;

	devices_sd = find(sys_sd, "devices", NULL);
	if (!devices_sd)
		return;

	pci_sd = find(devices_sd, "pci0000:00", NULL);
	if (!pci_sd)
		return;

	for (i = 0; i < ARRAY_SIZE(bridges); i++) {
		int j;

		bridge_sd = find(pci_sd, bridges[i], NULL);
		if (!bridge_sd)
			continue;

		for (j = 0; j < 8; j++) {
			char want[16];

			scnprintf(want, sizeof(want), "virtio%d", j);
			virtio_sd = find(bridge_sd, want, NULL);
			if (!virtio_sd)
				continue;

			rename(virtio_sd, bridge_sd, childnames[i], NULL);
			if (put)
				put(virtio_sd);
			break;      /* one virtio child per bridge */
		}
	}
}
/* --- /sys/class/drm ------------------------------------------------------- */

/*
 * `/sys/class/drm` was empty while `ro.hardware.egl=adreno` and the device tree claim an
 * sm8650 — a handset with an Adreno GPU always exposes card0 and renderD128 there. Their
 * absence is what makes `dumpsys gpu` segfault.
 *
 * Built the same way as the device tree: walk down from /sys (firmware_kobj->sd's parent),
 * create the directories with kernfs_create_dir_ns, and put attributes on them with
 * sysfs_add_bin_file_mode_ns.
 *
 * The module directories for Qualcomm drivers (ufs_qcom, msm_drm, arm_smmu, sdhci_msm) are
 * deliberately *not* created: reaching /sys/module means dereferencing `module_kset->kobj.sd`
 * and creating children under it, and that path deadlocks from module_init exactly the way
 * `class_kset` did. Both were measured; neither is worth a wedged guest for a directory
 * listing.
 */
static const char v_drm_status[] = "connected\n";

static struct khide_kprop khide_drm_props[] = {
	{ .name = "status", .value = v_drm_status, .len = sizeof(v_drm_status) },
};

static const char * const khide_drm_cards[] = { "card0", "renderD128" };

static unsigned int khide_drm_added;

static void khide_fix_drm(void)
{
	typedef struct kernfs_node *(*kfind_fn)(struct kernfs_node *, const char *,
						const void *);
	typedef struct kernfs_node *(*kmkdir_fn)(struct kernfs_node *, const char *,
						 umode_t, kuid_t, kgid_t, void *,
						 const void *);
	kfind_fn find;
	kmkdir_fn mkdir;
	struct kernfs_node *fw_sd, *sys_sd, *class_sd, *drm_sd;
	size_t i;

	fw_sd = firmware_kobj ? firmware_kobj->sd : NULL;
	if (!fw_sd)
		return;

	find = (kfind_fn)kallsyms_lookup_name("kernfs_find_and_get_ns");
	mkdir = (kmkdir_fn)kallsyms_lookup_name("kernfs_create_dir_ns");
	if (!find || !mkdir)
		return;

	sys_sd = *(struct kernfs_node **)((char *)fw_sd + 0x08);
	if (!sys_sd || (unsigned long)sys_sd < 0xffff000000000000UL)
		return;

	class_sd = find(sys_sd, "class", NULL);
	if (!class_sd)
		return;

	drm_sd = find(class_sd, "drm", NULL);
	if (!drm_sd)
		drm_sd = mkdir(class_sd, "drm", 0755, GLOBAL_ROOT_UID,
			       GLOBAL_ROOT_GID, NULL, NULL);
	if (!drm_sd || IS_ERR(drm_sd))
		return;

	for (i = 0; i < ARRAY_SIZE(khide_drm_cards); i++) {
		struct kernfs_node *c;

		if (find(drm_sd, khide_drm_cards[i], NULL))
			continue;

		c = mkdir(drm_sd, khide_drm_cards[i], 0755, GLOBAL_ROOT_UID,
			  GLOBAL_ROOT_GID, NULL, NULL);
		if (!c || IS_ERR(c))
			continue;

		khide_add_props(c, khide_drm_props, ARRAY_SIZE(khide_drm_props),
				&khide_drm_added);
	}
}
/* --- /sys/module: Qualcomm driver entries --------------------------------- */

/*
 * `/sys/module` had no Qualcomm driver at all (ufs_qcom, msm_drm, arm_smmu, sdhci_msm — 0
 * of 4) while the device tree claims sm8650. A phone whose SoC has none of those
 * contradicts itself in the same directory listing a checker reads.
 *
 * The obvious parent is `module_kset->kobj.sd`, and creating under it deadlocks — from
 * module_init *and* from a workqueue, so it is not a calling-context problem. Measuring
 * both paths showed why:
 *
 *     module_kset->kobj.sd       ffff9190c0157900
 *     /sys -> "module" node      ffff9190c0bcf110     <- different node
 *
 * Walking down from /sys yields a node that creates fine, and children land in the same
 * directory. That is the same route that made /sys/class/kgsl possible after class_kset
 * deadlocked.
 */
static const char v_mod_refcnt[]     = "0\n";
static const char v_mod_coresize[]   = "0\n";
static const char v_mod_initsize[]   = "0\n";
static const char v_mod_srcversion[] = "0000000000000000000000000000000000000000\n";

static struct khide_kprop khide_mod_props[] = {
	{ .name = "refcnt",     .value = v_mod_refcnt,     .len = sizeof(v_mod_refcnt) },
	{ .name = "coresize",   .value = v_mod_coresize,   .len = sizeof(v_mod_coresize) },
	{ .name = "initsize",   .value = v_mod_initsize,   .len = sizeof(v_mod_initsize) },
	{ .name = "srcversion", .value = v_mod_srcversion, .len = sizeof(v_mod_srcversion) },
};

/* What an SM-S9210 loads. Names only — /sys/module is a listing, and these are the entries
 * a handset shows there. */
static const char * const khide_qualcomm_modules[] = {
	"ufs_qcom",
	"ufs_qcom_ice",
	"msm_drm",
	"msm_gpu",
	"msm_kgsl",
	"arm_smmu",
	"sdhci_msm",
	"qcom_smd",
	"qcom_smem",
	"i2c_qcom_geni",
	"spmi_pmic_arb",
	"qcom_rpmh",
	"qcom_pmic_glink",
	"qti_thermal",
	"pinctrl_sm8650",
	"clk_qcom",
	"phy_qcom_qmp_ufs",
	"icc_bwmon",
	"qnoc_sm8650",
	"msm_geni_serial",
};

static unsigned int khide_mod_dirs;
static unsigned int khide_mod_prop_count;

static void khide_fix_module_entries(void)
{
	typedef struct kernfs_node *(*kfind_fn)(struct kernfs_node *, const char *,
						const void *);
	typedef struct kernfs_node *(*kmkdir_fn)(struct kernfs_node *, const char *,
						 umode_t, kuid_t, kgid_t, void *,
						 const void *);
	kfind_fn find;
	kmkdir_fn mkdir;
	struct kernfs_node *fw_sd, *sys_sd, *mod_sd;
	size_t i;

	fw_sd = firmware_kobj ? firmware_kobj->sd : NULL;
	if (!fw_sd)
		return;

	find = (kfind_fn)kallsyms_lookup_name("kernfs_find_and_get_ns");
	mkdir = (kmkdir_fn)kallsyms_lookup_name("kernfs_create_dir_ns");
	if (!find || !mkdir)
		return;

	sys_sd = *(struct kernfs_node **)((char *)fw_sd + 0x08);
	if (!sys_sd || (unsigned long)sys_sd < 0xffff000000000000UL)
		return;

	mod_sd = find(sys_sd, "module", NULL);
	if (!mod_sd)
		return;

	for (i = 0; i < ARRAY_SIZE(khide_qualcomm_modules); i++) {
		struct kernfs_node *d;

		if (find(mod_sd, khide_qualcomm_modules[i], NULL))
			continue;

		d = mkdir(mod_sd, khide_qualcomm_modules[i], 0755, GLOBAL_ROOT_UID,
			  GLOBAL_ROOT_GID, NULL, NULL);
		if (!d || IS_ERR(d))
			continue;

		khide_mod_dirs++;
		khide_add_props(d, khide_mod_props,
				ARRAY_SIZE(khide_mod_props), &khide_mod_prop_count);
	}
}
/* --- SCSI disk model ------------------------------------------------------ */

/*
 * `/sys/block/sdX/device/model` reads `HARDDISK` on all three disks. A handset's storage is
 * UFS behind sdhci_msm and reports a Samsung part number, so this name is a desktop tell —
 * and it is world-readable.
 *
 * This took nine attempts. What finally worked, and why the other eight did not:
 *
 *   - the string is not static. `sdev->model = sdev->inquiry + 16`
 *     (drivers/scsi/scsi_scan.c), so searching .rodata for "HARDDISK" finds nothing
 *   - `struct scsi_device` is `__randomize_layout`, so no field offset can be computed
 *   - `kernfs_node->priv` means three different things on the three nodes that look
 *     relevant: a disk device, an `sdev_dev`, and the literal `5`
 *   - scanning an unknown object forward faults: a qword past the end of a slab lands in
 *     unmapped memory, and three attempts wedged the guest that way
 *
 * The kernel exports the object instead:
 *
 *     struct Scsi_Host *scsi_host_lookup(unsigned short hostnum);
 *     struct scsi_device *scsi_device_lookup(host, channel, id, lun);
 *
 * `scsi_host_lookup` takes an **integer**, not a name. Passing "host0" as a pointer made it
 * search for host 0x3068736f and return NULL, which was the last bug.
 *
 * With the real `sdev` in hand, `ksize()` returns the allocation size (4096 here), and a scan
 * bounded by it cannot leave the object. The field lands at sdev+0x100 on this build.
 */
/* INQUIRY layout: byte 8 vendor (8), byte 16 model (16), byte 32 revision (4).
 * `sdev->vendor`, `sdev->model` and `sdev->rev` already point into this one buffer, so
 * rewriting the buffer fixes all three at once — no re-pointing needed. */
static const char khide_disk_vendor[] = "SAMSUNG ";
static const char khide_disk_model[]  = "KLUDG4UHGC-B0E1 ";
static const char khide_disk_rev[]    = "0300";

static bool khide_kaddr(const void *p)
{
	return (unsigned long)p >= 0xffff000000000000UL &&
	       (unsigned long)p < 0xffffffff00000000UL;
}

static bool khide_starts_with(const void *p, const char *want, size_t n)
{
	if (!khide_kaddr(p))
		return false;
	return memcmp(p, want, n) == 0;
}

/* The three INQUIRY strings sit at fixed offsets inside the device buffer:
 *   vendor @ +8 (8 bytes), model @ +16 (16), revision @ +32 (4)
 * Rewriting them in place is enough because `sdev->vendor`, `sdev->model` and `sdev->rev`
 * are pointers into that same buffer, and it is kmalloc memory — no page attribute change. */
static void khide_fix_inquiry(char *inq)
{
	memcpy(inq + 8,  khide_disk_vendor, 8);
	memcpy(inq + 16, khide_disk_model, 16);
	memcpy(inq + 32, khide_disk_rev, 4);
}

static unsigned int khide_disks_fixed;

static void khide_fix_scsi_model(void)
{
	typedef void *(*host_lookup_fn)(unsigned short);
	typedef void *(*sdev_lookup_fn)(void *, unsigned int, unsigned int, u64);
	typedef void (*sdev_put_fn)(void *);
	typedef size_t (*ksize_fn)(const void *);

	host_lookup_fn host_lookup;
	sdev_lookup_fn sdev_lookup;
	sdev_put_fn sdev_put;
	ksize_fn ksize;
	void *shost;
	int id;

	host_lookup = (host_lookup_fn)kallsyms_lookup_name("scsi_host_lookup");
	sdev_lookup = (sdev_lookup_fn)kallsyms_lookup_name("scsi_device_lookup");
	sdev_put = (sdev_put_fn)kallsyms_lookup_name("scsi_device_put");
	ksize = (ksize_fn)kallsyms_lookup_name("ksize");

	if (!host_lookup || !sdev_lookup || !sdev_put || !ksize)
		return;

	shost = host_lookup(0);
	if (!shost)
		return;

	for (id = 0; id < 4; id++) {
		void *sdev = sdev_lookup(shost, 0, id, 0);
		size_t size, off;

		if (!sdev)
			continue;

		size = ksize(sdev);
		if (!size || size > 0x4000) {
			sdev_put(sdev);
			continue;
		}

		/* Bounded by the real allocation: `off + 8 <= size` keeps every read inside
		 * the object, which is the property the earlier scans lacked.
		 *
		 * Both the model and the vendor are matched, so a device that already had its
		 * model rewritten still gets its vendor fixed — `vendor=VBOX` is the more
		 * direct giveaway of the two. */
		for (off = 0; off + sizeof(void *) <= size; off += sizeof(void *)) {
			void *field = *(void **)((char *)sdev + off);

			if (!khide_starts_with(field, "HARDDISK", 8) &&
			    !khide_starts_with(field, "VBOX", 4))
				continue;

			khide_fix_inquiry((char *)field);
			khide_disks_fixed++;
			break;
		}

		sdev_put(sdev);
	}
}
/* --- module -------------------------------------------------------------- */

static int __init khide_init(void)
{
	khide_fix_uname();
	khide_fix_clocksource();
	khide_fix_dmi();
	khide_fix_cpuinfo();
	khide_fix_version();
	khide_fix_cmdline();
	khide_fix_devicetree();
	khide_fix_gpu();
	khide_fix_drm();
	khide_fix_module_entries();
	khide_fix_scsi_model();
	khide_fix_gpu_dev();
	khide_fix_virtio_bus();
	khide_fix_virtio_nodes();
	khide_fix_module_dirs();
	khide_fix_dmesg();

	/* Last, because it is the step that makes the module un-unloadable. */
	khide_hide_self();

	return 0;
}

static void __exit khide_exit(void)
{
	/* Reachable only if the unlink did not happen — once it has, `rmmod` cannot find
	 * this module at all, so this path exists for the failure case. */
	khide_restore_dmesg();
	khide_restore_cpuinfo();
	khide_restore_dmi();
	khide_restore_clocksource();
	khide_restore_uname();
}

module_init(khide_init);
module_exit(khide_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("hide emulator and root traces in kernel data");
MODULE_AUTHOR("Absurdity233");
MODULE_VERSION("4.0");
