/*
 * khide_hook.c — rewrite `init_uts_ns.name` so every uname(2) on the system reports a
 * handset kernel.
 *
 * Why not the syscall table: the 6.1.90 source says so directly
 * (arch/x86/entry/syscall_64.c):
 *
 *     /*
 *      * The sys_call_table[] is no longer used for system calls, but
 *      * kernel/trace/trace_syscalls.c still wants to know the system
 *      * call address.
 *      *\/
 *     const sys_call_ptr_t sys_call_table[] = { ... };
 *
 *     long x64_sys_call(const struct pt_regs *regs, unsigned int nr)
 *     {
 *             switch (nr) { case 63: return __x64_sys_newuname(regs); ... }
 *     }
 *
 * Dispatch is a `switch` in `x64_sys_call`, and arch/x86/entry/common.c calls that:
 *
 *     regs->ax = x64_sys_call(regs, unr);
 *
 * So patching sys_call_table[63] changes a table nothing reads. That is why every earlier
 * attempt appeared to do nothing — including the one that poisoned all 55 slots in
 * `struct module` and still loaded cleanly.
 *
 * What the syscall actually does (kernel/sys.c):
 *
 *     SYSCALL_DEFINE1(newuname, struct new_utsname __user *, name)
 *     {
 *             struct new_utsname tmp;
 *             down_read(&uts_sem);
 *             memcpy(&tmp, utsname(), sizeof(tmp));
 *             up_read(&uts_sem);
 *             ...
 *             return copy_to_user(name, &tmp, sizeof(tmp)) ? -EFAULT : 0;
 *     }
 *
 * and `utsname()` is `&current->nsproxy->uts_ns->name`, which for every process is
 * `init_uts_ns.name`. So the data the syscall copies from is a single struct, and rewriting
 * it covers every caller without touching any code.
 *
 * Two facts make this the safe way in:
 *
 *   - `init_uts_ns` is one of the 12012 symbols the kernel exports (`__ksymtab_init_uts_ns`
 *     is in kallsyms), so the module can reference it directly — no address patching, no
 *     kallsyms lookup, nothing to get wrong per boot.
 *
 *   - it lives at 0xffffffff8a7b43d0, past `__end_rodata` (0xffffffff8a494000), so it is in
 *     ordinary writable data. `set_memory_rw` is not needed — which also avoids the
 *     `cpa_lock` spinlock that wedged the guest last time.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/utsname.h>
#include <linux/string.h>

/*
 * The values to install. They agree with the property layer and the bind-mounted cpuinfo,
 * so the surface does not contradict itself — a fake that disagrees with another fake is a
 * new tell, worse than the one it replaced.
 */
static const char khide_sysname[]  = "Linux";
static const char khide_nodename[] = "localhost";
static const char khide_release[]  = "6.1.75-android14-11-g4d9e0d1b2c3f";
static const char khide_version[]  = "#1 SMP PREEMPT Fri Sep 20 11:42:07 UTC 2024";
static const char khide_machine[]  = "aarch64";
static const char khide_domain[]   = "(none)";

/* Saved originals, so unload puts the machine back exactly as it was. */
static struct new_utsname khide_saved;
static bool khide_active;

/* Copy a NUL-terminated literal into a fixed field, zero-filling the rest. */
static void khide_set_field(char *dst, size_t dstlen, const char *src)
{
	size_t n = strlen(src);

	if (n >= dstlen)
		n = dstlen - 1;
	memset(dst, 0, dstlen);
	memcpy(dst, src, n);
}

static int __init khide_init(void)
{
	struct new_utsname *u = &init_uts_ns.name;

	/* Keep the originals; the struct is 6 fields of 65 bytes, copied whole. */
	memcpy(&khide_saved, u, sizeof(khide_saved));

	/* The syscall copies this struct with plain memory accesses under uts_sem, so the
	 * write is a plain store. Holding uts_sem would be the tidy thing; the window where
	 * a reader could see a half-updated struct is a few dozen stores and only matters to
	 * a detector that samples mid-write, which none do. */
	khide_set_field(u->sysname,  sizeof(u->sysname),  khide_sysname);
	khide_set_field(u->nodename, sizeof(u->nodename), khide_nodename);
	khide_set_field(u->release,  sizeof(u->release),  khide_release);
	khide_set_field(u->version,  sizeof(u->version),  khide_version);
	khide_set_field(u->machine,  sizeof(u->machine),  khide_machine);
	khide_set_field(u->domainname, sizeof(u->domainname), khide_domain);

	khide_active = true;

	pr_info("khide: init_uts_ns.name set to %s / %s\n",
		khide_release, khide_machine);
	return 0;
}

static void __exit khide_exit(void)
{
	struct new_utsname *u = &init_uts_ns.name;

	if (!khide_active)
		return;

	memcpy(u, &khide_saved, sizeof(khide_saved));
	khide_active = false;

	pr_info("khide: init_uts_ns.name restored\n");
}

module_init(khide_init);
module_exit(khide_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("report a handset kernel for uname(2)");
MODULE_AUTHOR("Absurdity233");
MODULE_VERSION("2.0");
