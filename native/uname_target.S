/*
 * uname_target.S — a process that calls uname(2) itself, repeatedly.
 *
 * Exists so the tracer can be tested against a real target: `uname` from a shell
 * always forks, so the syscall happens in a child the tracer never attached to.
 * This program calls the syscall in its own thread of execution and writes the
 * utsname back out, which is what makes a rewrite observable.
 *
 * Writes to /data/local/tmp/uname_target.out, one result per second, in the form:
 *   <release>|<machine>
 *
 * Freestanding: no libc, no headers, so it links on a host with no Android sysroot.
 */
    .text
    .globl _start
_start:
    /* open out file O_WRONLY|O_CREAT|O_TRUNC = 2|0x40|0x200 = 0x242, mode 0644 */
    leaq    path(%rip), %rdi
    movl    $0x242, %esi
    movl    $0644, %edx
    movl    $2, %eax            /* SYS_open */
    syscall
    testq   %rax, %rax
    js      .exit1
    movl    %eax, %r12d         /* fd */

.loop:
    /* uname(&uts) */
    leaq    uts(%rip), %rdi
    movl    $63, %eax
    syscall

    /* write release */
    movl    %r12d, %edi
    leaq    uts+130(%rip), %rsi  /* offsetof(release) = 2*65 */
    xorl    %edx, %edx
1:  cmpb    $0, (%rsi,%rdx)
    je      2f
    incq    %rdx
    jmp     1b
2:  movl    $1, %eax            /* SYS_write */
    syscall

    /* write "|" */
    movl    %r12d, %edi
    leaq    sep(%rip), %rsi
    movl    $1, %edx
    movl    $1, %eax
    syscall

    /* write machine */
    movl    %r12d, %edi
    leaq    uts+260(%rip), %rsi  /* offsetof(machine) = 4*65 */
    xorl    %edx, %edx
3:  cmpb    $0, (%rsi,%rdx)
    je      4f
    incq    %rdx
    jmp     3b
4:  movl    $1, %eax
    syscall

    /* write "\n" */
    movl    %r12d, %edi
    leaq    nl(%rip), %rsi
    movl    $1, %edx
    movl    $1, %eax
    syscall

    /* sleep 1s */
    leaq    ts(%rip), %rdi
    xorl    %esi, %esi
    movl    $35, %eax           /* SYS_nanosleep */
    syscall

    jmp     .loop

.exit1:
    movl    $60, %eax
    movl    $1, %edi
    syscall

    .section .rodata
path:   .asciz "/data/local/tmp/uname_target.out"
sep:    .ascii "|"
nl:     .ascii "\n"

    .bss
    .align 8
uts:    .space 390              /* struct utsname: 6 * 65 */
ts:     .space 16               /* struct timespec {tv_sec, tv_nsec} */
