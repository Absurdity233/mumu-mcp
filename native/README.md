# native/

The two binaries that reach `uname(2)`, and why they are written the way they are.

`uname` is a syscall, not a file. Bind-mounting `/proc/version` changes nothing
(verified), and `/proc/sys/kernel/osrelease` is read-only. So the last emulator tell
has to be closed at the syscall layer, from inside the process.

Two approaches, different trade-offs:

| | `uname_patch` (ptrace) | `uname_hook.so` (LD_PRELOAD) |
|---|---|---|
| covers a target that calls the syscall directly | yes | no |
| covers a target with its own loader | yes | no |
| overhead | one ptrace stop per syscall | none |
| survives the target clearing the environment | yes | no |

This image's precondition was measured, not assumed: no Yama
(`/proc/sys/kernel/yama/ptrace_scope` does not exist), `Seccomp: 0`, and
`strace -p 1` attaches — so a root tracer can reach anything.

## Build

There is no Android sysroot on this host and no headers on the device, so a normal
C build cannot link. The tracer is therefore freestanding assembly:

```bash
clang --target=x86_64-linux-android26 -c -o uname_patch.o uname_patch.S
clang --target=x86_64-linux-android26 -nostdlib -static -o uname_patch uname_patch.o
```

`uname_target` is built the same way. It exists only for testing: `uname` from a
shell always forks, so the syscall lands in a child the tracer never attached to.
`uname_target` calls the syscall in its own thread and writes the result out, which
is what makes a rewrite observable.

The `LD_PRELOAD` variant keeps only its header (`uname_hook.h`) in the tree: it
needs a real `dlopen`, so it cannot be built freestanding, and the ptrace tracer
covers every case it cannot. If a sysroot is available and the cheaper path is
wanted, implement it against that header — the expected values live there so the two
cannot drift.

The device runs x86-64 userspace (verified: `/system/bin/toybox` is an x86-64 ELF),
so `x86_64-linux-android26` is the right target for anything executed here, even
though `uname -m` claims aarch64.

## Three bugs this took, each found by measurement

1. **Wait-status decoding.** This kernel reports `0x2343` for a syscall stop, whose
   low byte is `0x43`. The standard `(status & 0x7f) == 0x7f` test never fired, so
   the tracer sat in its loop seeing nothing. The code now accepts both forms.

2. **`PTRACE_POKEDATA` argument order.** The signature is
   `(request, pid, addr, data)` in `rdi/rsi/rdx/r10`; the first version put the
   address in `r10` and the data in `rdx`, so every write went somewhere invalid and
   failed *silently*. Checking the return value is what should have caught it — the
   `/proc/<pid>/mem` read-back is what did.

3. **utsname field padding.** Fields are 65 bytes. Getting one `.space` off by one
   shifts every later field, which showed up as a correct `release` and an empty
   `machine`. `tests/test_uname.py` now asserts each field adds up to 65.

## Verified result

```
before: 6.1.90-perf+|aarch64
after : 6.1.75-android14-11-g4d9e0d1b2c3f|aarch64
```

Confirmed twice: once through the target's own output, and once by reading the
struct bytes out of the tracee's memory with `dd if=/proc/<pid>/mem`.
