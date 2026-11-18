/* Shared replacement values, so the LD_PRELOAD hook and the ptrace tracer cannot
 * drift apart. Values describe a Galaxy S24 (SM-S9210) on Android 15, matching the
 * property profile in mumu_mcp/fakeprofile.py. */

#ifndef MUMU_UNAME_HOOK_H
#define MUMU_UNAME_HOOK_H

#include <sys/utsname.h>

#define MUMU_SYSNAME     "Linux"
#define MUMU_NODENAME    "localhost"
#define MUMU_RELEASE     "6.1.75-android14-11-g4d9e0d1b2c3f"
#define MUMU_VERSION     "#1 SMP PREEMPT Fri Sep 20 11:42:07 UTC 2024"
#define MUMU_MACHINE     "aarch64"
#define MUMU_DOMAINNAME  "(none)"

/* Writes the fake utsname into `out`. */
void mumu_fake_utsname(struct utsname *out);

#endif /* MUMU_UNAME_HOOK_H */
