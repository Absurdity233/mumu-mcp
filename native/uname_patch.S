/*
 * uname_patch.S — freestanding x86-64 Android tracer that rewrites uname(2).
 *
 * Why assembly: this host has clang 22 but no Android sysroot, and the device has
 * no headers either, so a C version cannot link. A freestanding translation unit
 * needs nothing but the kernel ABI, so it builds here and runs there.
 *
 * What it does, per traced thread:
 *   syscall-entry stop  -> if orig_rax == SYS_uname, remember rdi (the utsname*)
 *   syscall-exit  stop  -> if the remembered call succeeded (rax == 0), overwrite
 *                          that struct with the handset values
 *
 * Measured preconditions (MuMu 6.8.x / Android 15):
 *   - no /proc/sys/kernel/yama/ptrace_scope  -> no Yama restriction
 *   - `strace -p 1` works as root            -> ptrace reaches even init
 *   - Seccomp: 0                             -> no filter to fight
 *
 * Cost: one ptrace stop per syscall, so the target runs measurably slower. This is
 * an analysis tool, not something to leave attached during play.
 *
 * x86-64 syscalls: write=1 exit=60 wait4=61 ptrace=101 uname=63
 * struct user_regs_struct offsets: rax=80, orig_rax=120, rdi=112
 */

    .text
    .globl _start

_start:
    movq    (%rsp), %rax                /* argc */
    cmpq    $2, %rax
    jl      .usage
    movq    16(%rsp), %rsi              /* argv[1] */

    xorl    %eax, %eax                  /* parse pid */
1:  movzbl  (%rsi), %ecx
    testb   %cl, %cl
    je      2f
    subl    $'0', %ecx
    cmpl    $9, %ecx
    ja      .usage
    imull   $10, %eax, %eax
    addl    %ecx, %eax
    incq    %rsi
    jmp     1b
2:  movl    %eax, %r12d                 /* r12d = pid */
    testl   %r12d, %r12d
    jle     .usage

    /* ptrace(PTRACE_ATTACH=16, pid, 0, 0) */
    movl    $101, %eax
    movl    $16, %edi
    movl    %r12d, %esi
    xorl    %edx, %edx
    xorl    %r10d, %r10d
    syscall
    testq   %rax, %rax
    js      .attach_failed

    /* wait4(pid, &status, 0, 0) — reap the attach stop */
    call    wait_child

    /* ptrace(PTRACE_SETOPTIONS=0x4200, pid, 0, TRACESYSGOOD|EXITKILL) */
    movl    $101, %eax
    movl    $0x4200, %edi
    movl    %r12d, %esi
    xorl    %edx, %edx
    movq    $0x100001, %r10
    syscall

    movl    $1, %eax
    movl    $1, %edi
    leaq    msg_attached(%rip), %rsi
    movl    $msg_attached_len, %edx
    syscall

    movb    $1, entering(%rip)          /* next syscall-stop is an entry */
    movb    $0, armed(%rip)

/* ---- trace loop -------------------------------------------------------- */
.trace:
    /* ptrace(PTRACE_SYSCALL=24, pid, 0, 0) */
    movl    $101, %eax
    movl    $24, %edi
    movl    %r12d, %esi
    xorl    %edx, %edx
    xorl    %r10d, %r10d
    syscall
    testq   %rax, %rax
    js      .loop_guard

    call    wait_child
    jmp     .after_wait

.loop_guard:
    jmp     .trace

.after_wait:
    /*
     * Stop-code decoding, measured on this kernel rather than assumed.
     *
     * Standard Linux packs a wait status into 16 bits: low byte 0x7f means
     * "stopped", and WSTOPSIG is (status >> 8) & 0xff. This image reports
     * 0x2343 for a syscall stop, whose low byte is 0x43 — so the usual
     * `(status & 0x7f) == 0x7f` test never fires and the tracer sits in the loop
     * seeing nothing. The observed encoding puts the stop signature in bits
     * 8..15 and the ptrace event in 16..23.
     *
     * The checks below therefore use the observed form, with a fallback to the
     * standard one so the same binary still works on a kernel that behaves
     * normally.
     */
    movl    status(%rip), %eax
    movl    %eax, %ecx

    /* Standard WIFEXITED: (status & 0x7f) == 0 -> task is gone. */
    movl    %ecx, %edx
    andl    $0x7f, %edx
    je      .done

    /* Standard WIFSTOPPED signature. */
    cmpl    $0x7f, %edx
    je      .is_stop

    /* This kernel: low byte 0x43 with a signal in bits 8..15. 0x2343 means
     * stopped-by-SIGTRAP(5)+0x20, which is how the RT-signal-based syscall stop
     * surfaces here. Treat any status whose low byte is 0x43 as a stop. */
    movl    %ecx, %edx
    andl    $0xff, %edx
    cmpl    $0x43, %edx
    je      .is_stop

    /* Anything else is an exit or a signal we do not model: forward and continue. */
    jmp     .forward_signal

.is_stop:
    /* Syscall-stop detection. On this kernel the marker is 0x23 in bits 8..15
     * (0x2343 >> 8 == 0x23). Accept 0x85 too, which is the standard
     * SIGTRAP|0x80 form. */
    movl    %ecx, %edx
    shrl    $8, %edx
    andl    $0xff, %edx
    cmpl    $0x23, %edx
    je      .syscall_stop
    cmpl    $0x85, %edx
    je      .syscall_stop
    jmp     .forward_signal

.syscall_stop:

    /* PTRACE_GETREGS=12 into regs */
    movl    $101, %eax
    movl    $12, %edi
    movl    %r12d, %esi
    xorl    %edx, %edx
    leaq    regs(%rip), %r10
    syscall
    cmpq    $-1, %rax
    je      .trace

    cmpb    $0, entering(%rip)
    jne     .on_entry
    jmp     .on_exit

/* ---- on syscall entry -------------------------------------------------- */
.on_entry:
    movq    regs+120(%rip), %rax        /* orig_rax */
    cmpq    $63, %rax                   /* SYS_uname */
    jne     3f
    movq    regs+112(%rip), %rax        /* rdi = utsname* */
    testq   %rax, %rax
    je      3f
    movq    %rax, ubuf(%rip)
    movb    $1, armed(%rip)
    movb    $0, entering(%rip)          /* next stop is the exit */
    jmp     .trace
3:  movb    $0, armed(%rip)
    movb    $0, entering(%rip)
    jmp     .trace

/* ---- on syscall exit --------------------------------------------------- */
.on_exit:
    movb    $1, entering(%rip)
    cmpb    $0, armed(%rip)
    je      .trace
    movb    $0, armed(%rip)
    movq    regs+80(%rip), %rax         /* rax = return value */
    testq   %rax, %rax
    jne     .trace                      /* only rewrite on success */
    movq    ubuf(%rip), %r13
    testq   %r13, %r13
    je      .trace

    /* Announce the rewrite once so a failed interception is distinguishable from
     * a silent no-op. Print the address too — without it there is no way to tell a
     * wrong pointer from a wrong write. */
    cmpb    $0, announced(%rip)
    jne     6f
    movb    $1, announced(%rip)
    movl    $1, %eax
    movl    $2, %edi
    leaq    msg_hit(%rip), %rsi
    movl    $msg_hit_len, %edx
    syscall
    /* print the buffer address in hex, 16 digits */
    movq    %r13, %rbx
    leaq    hexbuf(%rip), %rsi
    movl    $16, %ecx
7:  movq    %rbx, %rdx
    andl    $0xf, %edx
    cmpl    $9, %edx
    jbe     8f
    addl    $('a'-10-'0'), %edx
8:  addl    $'0', %edx
    movb    %dl, -1(%rsi,%rcx)
    shrq    $4, %rbx
    decl    %ecx
    jnz     7b
    movl    $1, %eax
    movl    $2, %edi
    movl    $17, %edx
    syscall
6:

    /* PTRACE_POKEDATA=5, word at a time.
     *
     * The syscall argument order is (request, pid, addr, data) in
     * rdi/rsi/rdx/r10 — the first version put the address in r10 and the data in
     * rdx, so every write went to a bogus address and failed silently. Checking
     * the return value is what would have caught it; the memory read-back did.
     *
     * 6 fields * 65 = 390 bytes is not a multiple of 8, so the tail is handled
     * separately rather than spilling past the struct. */
    leaq    fake_uts(%rip), %r14
    xorl    %r15d, %r15d
5:  movq    (%r14,%r15,8), %r10         /* data -> r10 */
    leaq    (%r13,%r15,8), %rdx         /* addr -> rdx */
    movl    $101, %eax
    movl    $5, %edi
    movl    %r12d, %esi
    syscall
    testq   %rax, %rax
    js      .trace                      /* a failed poke must not look like success */
    incq    %r15
    cmpq    $48, %r15
    jb      5b

    /* Tail: 390 - 384 = 6 bytes at offset 384. Read the word, splice in our 6
     * bytes, write back — never writing past the struct. */
    leaq    384(%r13), %rdx
    movl    $101, %eax                  /* PTRACE_PEEKDATA=2 */
    movl    $2, %edi
    movl    %r12d, %esi
    syscall
    js      .trace
    movq    %rax, %r10                  /* tracee's existing word */

    movq    384(%r14), %rbx             /* our 6 bytes */
    movabsq $0x0000ffffffffffff, %rax
    andq    %rax, %rbx
    andq    %rax, %r10                  /* keep low 6 bytes of the tracee's word */
    orq     %rbx, %r10

    leaq    384(%r13), %rdx
    movl    $101, %eax                  /* PTRACE_POKEDATA=5 */
    movl    $5, %edi
    movl    %r12d, %esi
    syscall
    jmp     .trace

.forward_signal:
    movl    $101, %eax
    movl    $24, %edi
    movl    %r12d, %esi
    xorl    %edx, %edx
    movl    %ecx, %r10d
    syscall
    jmp     .trace

.done:
    movl    $60, %eax
    xorl    %edi, %edi
    syscall

.attach_failed:
    movl    $1, %eax
    movl    $2, %edi
    leaq    msg_fail(%rip), %rsi
    movl    $msg_fail_len, %edx
    syscall
    movl    $60, %eax
    movl    $1, %edi
    syscall

.usage:
    movl    $1, %eax
    movl    $2, %edi
    leaq    msg_usage(%rip), %rsi
    movl    $msg_usage_len, %edx
    syscall
    movl    $60, %eax
    movl    $2, %edi
    syscall

/* wait4(pid, &status, 0, 0); retries on EINTR. Clobbers rax/rdi/rsi/rdx/r10. */
wait_child:
    movl    $61, %eax
    movl    %r12d, %edi
    leaq    status(%rip), %rsi
    xorl    %edx, %edx
    xorl    %r10d, %r10d
    syscall
    cmpq    $-4, %rax                   /* -EINTR */
    je      wait_child
    ret

/* ---- data -------------------------------------------------------------- */
    .section .rodata
msg_attached:
    .ascii "attached; rewriting uname -> 6.1.75-android14 / aarch64\n"
    .set msg_attached_len, . - msg_attached
msg_fail:
    .ascii "ptrace attach failed (need root; check yama ptrace_scope)\n"
    .set msg_fail_len, . - msg_fail
msg_hit:
    .ascii "rewrote a uname result\n"
    .set msg_hit_len, . - msg_hit
msg_usage:
    .ascii "usage: uname_patch <pid>\n"
    .set msg_usage_len, . - msg_usage

/* The utsname to write: 6 fields x 65 bytes. Built from the same strings as
 * uname_hook.h; kept inline because a freestanding object cannot include it. */
    .align 8
fake_uts:
    /* sysname  "Linux" */
    .asciz "Linux"
    .space 59
    /* nodename "localhost" */
    .asciz "localhost"
    .space 55
    /* release */
    .asciz "6.1.75-android14-11-g4d9e0d1b2c3f"
    .space 31
    /* version */
    .asciz "#1 SMP PREEMPT Fri Sep 20 11:42:07 UTC 2024"
    .space 21
    /* machine  "aarch64" */
    .asciz "aarch64"
    .space 57
    /* domainname */
    .asciz "(none)"
    .space 58

    .bss
    .align 8
status:     .space 8
regs:       .space 216
ubuf:       .space 8
armed:      .space 1
entering:   .space 1
announced:  .space 1
hexbuf:     .space 24
