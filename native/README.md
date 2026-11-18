# native/

The binaries that reach `uname(2)`, and why they are written the way they are.

`uname` is a syscall, not a file. Bind-mounting `/proc/version` changes nothing
(verified), and `/proc/sys/kernel/osrelease` is read-only. So this last emulator tell has
to be closed at the syscall layer, from inside a process.

Two approaches were built; one works here.

| | `uname_patch` (ptrace) — ships | `uname_hook.h` (LD_PRELOAD) — header only |
|---|---|---|
| covers a target that calls the syscall directly | yes | no |
| covers a target with its own loader | yes | no |
| overhead | one ptrace stop per syscall | none |

`uname_hook.h` keeps only the expected values: the LD_PRELOAD variant needs a real
`dlopen`, so it cannot be built freestanding, and the tracer covers everything it cannot.

## Build

There is no Android sysroot on this host and no headers on the device, so a normal C
build cannot link. The tracer is freestanding assembly:

```bash
clang --target=x86_64-linux-android26 -c -o uname_patch.o uname_patch.S
clang --target=x86_64-linux-android26 -nostdlib -static -o uname_patch uname_patch.o
```

`uname_target` is built the same way. It exists only for testing: `uname` from a shell
always forks, so the syscall lands in a child the tracer never attached to. `uname_target`
calls the syscall in its own thread, which is what makes a rewrite observable.

The device runs x86-64 userspace (`/system/bin/toybox` is an x86-64 ELF), so
`x86_64-linux-android26` is the right target even though `uname -m` claims aarch64.

## Which syscall actually runs

Measured with two kprobes side by side over three `uname` invocations:

```
__x64_sys_newuname   3 hits
__x64_sys_uname      0 hits
```

**`uname(2)` dispatches to `__x64_sys_newuname`.** Several earlier builds patched
`sys_call_table[63]` assuming it held `__x64_sys_uname`; reading the slot with a kprobe
confirmed it holds `0xffffffff...20520`, the `newuname` address. Hooking the wrong symbol
is why those builds looked inert.

## Three bugs this took, each found by measurement

1. **Wait-status decoding.** This kernel reports `0x2343` for a syscall stop, whose low
   byte is `0x43`. The standard `(status & 0x7f) == 0x7f` test never fired, so the tracer
   looped forever seeing nothing. The code now accepts both forms.

2. **`PTRACE_POKEDATA` argument order.** The signature is
   `(request, pid, addr, data)` in `rdi/rsi/rdx/r10`; the first version put the address in
   `r10` and the data in `rdx`, so every write went somewhere invalid and failed
   *silently*. Checking the return value should have caught it — the `/proc/<pid>/mem`
   read-back is what did.

3. **utsname field padding.** Fields are 65 bytes. Getting one `.space` off by one shifts
   every later field, which showed up as a correct `release` and an empty `machine`.
   `tests/test_uname.py` now asserts each field adds up to 65.

## Verified result

```
before: 6.1.90-perf+|aarch64
after : 6.1.75-android14-11-g4d9e0d1b2c3f|aarch64
```

Confirmed twice: through the target's own output, and by reading the struct bytes out of
the tracee's memory with `dd if=/proc/<pid>/mem`.

## The kernel-module route, and why it is not here

A kernel module patching `sys_call_table[63]` would make this system-wide instead of
per-process. It was built and did not work; the notes are kept because the failures are
informative, and because anyone repeating this deserves to skip them.

What loads: a **zero-import** module. `CONFIG_MODVERSIONS=y` with no `Module.symvers`
means any imported symbol needs a CRC that cannot be produced, but a module importing
nothing has an empty `__versions` and passes. `-mcmodel=large` is required —
`-mcmodel=kernel` leaves `R_X86_64_32S` relocations and the loader rejects the file with
`overflow in relocation type 10 ... likely not compiled with -mcmodel=kernel`.

What does not work: getting the module's `init_module` to take effect. The symptoms, all
measured:

* `finit_module` returns 0, the module appears in `lsmod`, `initstate` reads `live`;
* `do_init_module` **is** called once, with a correctly-wired `struct module`
  (`this_module` at the expected address, its init field pointing into `.init.text`);
* `.init.text` **is** freed afterwards, which only happens after init runs;
* yet `sys_call_table[63]` still holds the original `__x64_sys_newuname`, and the module's
  own `.data` — whose patched sentinels were read back and confirmed correct at runtime —
  is untouched.

Those four facts cannot all be true under the upstream `load_module` code path, which is
the signature of a vendor kernel doing something its source does not describe. Ruled out
one at a time, each by measurement: section placement, entry-point symbol form, `__versions`
content and size, `.note.Linux` (KMI marker), `name=` in `.modinfo`, `retpoline=Y`,
`module_layout` CRC, non-empty `.text`, presence of `.rela.init.text`, and every plausible
`struct module` offset (0x50, 0x68, 0x70 — poisoning each still let the module load).

With `panic_on_oops=0` these failures leave the module resident and silent, which is what
made them look like "init never ran" for a long time.

**If this is ever picked up again:** it needs either this vendor's kernel source, or one
third-party `.ko` that demonstrably runs its init on this image to diff against. Without
one of those, the per-process tracer is the ceiling.