/*
 * khide_hook.c — hide the emulator surface in kernel data, with no bind mounts.
 *
 * A bind mount changes what a read returns, but it also adds a line to `/proc/self/mounts`,
 * and every process can read that. On a real device `/proc/cpuinfo`, `/proc/kallsyms` and
 * `/sys/class/dmi/id/*` come from procfs and sysfs, so seeing them backed by an ext4
 * partition gives everything away without reading a faked value. Each item below is
 * therefore fixed in the data the kernel serves.
 *
 * Everything here was established by measurement on this image, not by reading the header
 * and doing arithmetic. The measured numbers:
 *
 *   cpuinfo_x86.x86_vendor_id   at +0x88   (string search)
 *   cpuinfo_x86.x86_model_id    at +0x98   (immediately after)
 *   cpuinfo_x86.x86_capability  at +0x28
 *
 * The last one is the interesting one. `0x88 - 0x60 = 0x28`, and 0x60 is exactly
 * (NCAPINTS 22 + NBUGINTS 2) * 4 bytes — so the array ends flush against the vendor
 * string with no trailing padding. Reading u32 word 4 there gives 0xfef83203, bit 31 set,
 * which is X86_FEATURE_HYPERVISOR (4*32+31). Two other candidate offsets also looked
 * plausible by having bit 31 set, so the value was cross-checked against the struct maths
 * rather than trusted on appearance alone.
 *
 * `curr_clocksource` is a `struct clocksource *`, and `name` is at +0x30 — not at 0. The
 * struct begins with the `read` function pointer; writing a string there made the kernel
 * call into text while taking a timestamp, and the guest stopped answering. Every offset
 * here is now verified before it is written.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/utsname.h>
#include <linux/string.h>
#include <linux/kallsyms.h>
#include <linux/percpu.h>

#define KHIDE_RELEASE   "6.1.75-android14-11-g4d9e0d1b2c3f"
#define KHIDE_VERSION   "#1 SMP PREEMPT Fri Sep 20 11:42:07 UTC 2024"

/*
 * `uname -m` must agree with the ELF headers, because that is the cheapest cross-check
 * there is. Every userspace binary on this image is EM_X86_64 (e_machine 0x3e) —
 * /system/bin/sh, toybox, app_process64, linker64, libc.so, libm.so all confirm it — while
 * the stock kernel reports aarch64 because MuMu's own patch says so.
 *
 * Reporting aarch64 was therefore counterproductive: it contradicted every binary on the
 * system. A detector reading one ELF header learns more than it would from any of the
 * values above. The honest fix is to report the architecture the userspace actually is,
 * and let `uname` stop being the thing that disagrees.
 */
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
	pr_info("khide: uname -> %s / %s\n", KHIDE_RELEASE, KHIDE_MACHINE);
}

static void khide_restore_uname(void)
{
	if (khide_uts_done)
		memcpy(&init_uts_ns.name, &khide_saved_uts, sizeof(khide_saved_uts));
}

/* --- clocksource --------------------------------------------------------- */

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

	/* A wrong offset here overwrites `read`, a function pointer. */
	if (!*namep || (unsigned long)*namep < 0xffff000000000000UL)
		return;

	khide_saved_cs_name = *namep;
	*namep = KHIDE_CLOCKSRC;
	khide_cs_done = true;
	pr_info("khide: clocksource -> %s\n", KHIDE_CLOCKSRC);
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

/* enum dmi_field, include/linux/mod_devicetable.h. Not in name order. */
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
	pr_info("khide: %zu DMI fields overridden\n", ARRAY_SIZE(khide_dmi));
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

/* Measured on this image; see the header comment. */
#define KHIDE_VENDOR_OFF  0x88
#define KHIDE_MODEL_OFF   0x98
#define KHIDE_CAP_OFF     0x28
#define KHIDE_CAP_WORD    4          /* X86_FEATURE_HYPERVISOR = 4*32+31 */
#define KHIDE_CAP_BIT     31
#define KHIDE_VMMCALL_WORD 8         /* X86_FEATURE_VMMCALL = 8*32+15 */
#define KHIDE_VMMCALL_BIT  15

/*
 * The first four bytes of the struct are `__u8 x86; __u8 x86_vendor; __u8 x86_model;
 * __u8 x86_stepping;` — family, vendor id, model, stepping. They are patched alongside the
 * strings because `cpu family: 26` is an AMD Zen value and printing it next to
 * "GenuineIntel" is a contradiction inside one screen of output.
 *
 * Values for the Celeron N5105 named below: family 6, model 156 (0x9c), stepping 0.
 */
#define KHIDE_CPU_FAMILY   6
#define KHIDE_CPU_MODEL    0x9c
#define KHIDE_CPU_STEPPING 0

/*
 * What `model name` should print.
 *
 * `uname -m` reports x86_64 because that is what every binary on the image is, so cpuinfo
 * must stay x86-shaped too — an ARM model string under an x86 `vendor_id` would be a new
 * contradiction. What has to go is the *host* part name: "AMD Ryzen 9 9955HX" is a desktop
 * CPU and no Android device has one. A generic mobile-class x86 model keeps the format
 * consistent without naming the machine the emulator runs on.
 */
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
	 * original is one of the four below; after patching it is "GenuineIntel", which is
	 * also in the list, so the same probe works in both directions. */
	vid = per + KHIDE_VENDOR_OFF;
	if (memcmp(vid, "AuthenticAMD", 12) && memcmp(vid, "GenuineIntel", 12) &&
	    memcmp(vid, "CentaurHauls", 12) && memcmp(vid, "HygonGenuine", 12)) {
		pr_info("khide: cpu%d vendor offset moved, skipping\n", cpu);
		return;
	}

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

	/* family / model / stepping, so the numbers agree with the vendor string. */
	{
		u8 *head = (u8 *)per;

		head[0] = KHIDE_CPU_FAMILY;
		head[2] = KHIDE_CPU_MODEL;
		head[3] = KHIDE_CPU_STEPPING;
	}

	/* Clear only the two bits that name the hypervisor:
	 *   word 4 bit 31  X86_FEATURE_HYPERVISOR  -> the `hypervisor` flag
	 *   word 8 bit 15  X86_FEATURE_VMMCALL     -> the `vmmcall` flag
	 *
	 * `vmmcall` on its own is an AMD SVM instruction and a real AMD CPU reports it; what
	 * a check keys on is the pair — vmmcall present while hypervisor is absent is the
	 * normal hardware case, and hypervisor present at all is the VM case. Clearing both
	 * is what makes the two lines read like a handset. Nothing else in the capability
	 * words is touched, because the kernel reads them for real decisions. */
	cap[KHIDE_CAP_WORD] &= ~(1u << KHIDE_CAP_BIT);
	cap[KHIDE_VMMCALL_WORD] &= ~(1u << KHIDE_VMMCALL_BIT);

	/* The vendor string has to agree with the model string above, or the two lines
	 * contradict each other inside the same file — which is a worse tell than the host
	 * CPU name it replaced. "GenuineIntel" matches the Celeron model. */
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

	if (khide_cpu_count) {
		khide_cpu_done = true;
		pr_info("khide: cpuinfo rewritten on %u cpu(s)\n", khide_cpu_count);
	}
}

static void khide_restore_cpuinfo(void)
{
	int cpu;

	if (!khide_cpu_done)
		return;

	for_each_online_cpu(cpu)
		khide_fix_one_cpu(cpu, false);
}

/* --- hide the module itself ---------------------------------------------- */

/*
 * `/proc/modules` walks the global `modules` list, and `/sys/module/<name>` is created by
 * `mod_sysfs_setup`. Unlinking from both is the only way a loaded module stops being
 * visible, because there is no "hidden" flag to set — the kernel simply has no notion of a
 * module that exists but should not be listed.
 *
 * The cost is stated rather than discovered later: once `list_del` has run, `rmmod` cannot
 * find the module, and `module_put` bookkeeping no longer sees it. Unloading becomes
 * impossible from userspace; the module stays resident until reboot. That is the price of
 * invisibility, and it is why the unlinking happens last, after everything else has
 * succeeded.
 *
 * `THIS_MODULE` is the module's own `struct module`, so both operations are local — no
 * symbol lookup, nothing that can be stale.
 */
static bool khide_unlinked;
static struct list_head khide_saved_list;

static void khide_hide_self(void)
{
	typedef void (*teardown_fn)(struct module *);
	teardown_fn teardown;

	if (khide_unlinked)
		return;

	/* `mod_sysfs_teardown` is not exported, so it is found through kallsyms. If it is
	 * missing, the list unlink below still hides the module from /proc/modules — the
	 * check that matters — and only /sys/module/<name> would remain. */
	teardown = (teardown_fn)kallsyms_lookup_name("mod_sysfs_teardown");
	if (teardown)
		teardown(THIS_MODULE);

	/* Unlink from the global module list. `/proc/modules` walks that list with
	 * list_entry(), so an unlinked module simply stops appearing. The original
	 * neighbours are recorded first; undoing it is not attempted, because `rmmod` can
	 * no longer find the module either. */
	khide_saved_list = THIS_MODULE->list;
	list_del(&THIS_MODULE->list);
	khide_unlinked = true;

	pr_info("khide: unlinked from /proc/modules%s\n",
		teardown ? " and /sys/module" : " (sysfs entry left)");
}

/* --- module -------------------------------------------------------------- */

static int __init khide_init(void)
{
	khide_fix_uname();
	khide_fix_clocksource();
	khide_fix_dmi();
	khide_fix_cpuinfo();

	/* Last, because it is the step that makes the module un-unloadable. */
	khide_hide_self();

	pr_info("khide: loaded, no bind mounts involved\n");
	return 0;
}

static void __exit khide_exit(void)
{
	/* Reachable only if the unlink did not happen — once it has, `rmmod` cannot find
	 * this module at all, so this path exists for the failure case. */
	khide_restore_cpuinfo();
	khide_restore_dmi();
	khide_restore_clocksource();
	khide_restore_uname();
	pr_info("khide: unloaded, originals restored\n");
}

module_init(khide_init);
module_exit(khide_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("hide emulator and root traces in kernel data, without bind mounts");
MODULE_AUTHOR("Absurdity233");
MODULE_VERSION("3.2");
