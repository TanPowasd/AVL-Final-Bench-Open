"""Trusted launcher-only seccomp filter. Install after exec, before user code.

Default EPERM, not a denylist. ctypes and handwritten machine code encounter the
same kernel filter. The syscall names are resolved by the frozen libseccomp.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform


ALLOW = (
    'read', 'write', 'readv', 'writev', 'pread64', 'pwrite64', 'close', 'close_range',
    'openat', 'open', 'fstat', 'newfstatat', 'stat', 'lstat', 'statx', 'lseek',
    'getdents64', 'readlink', 'readlinkat', 'access', 'faccessat', 'faccessat2',
    'mmap', 'mmap2', 'mprotect', 'munmap', 'mremap', 'madvise', 'brk',
    'rt_sigaction', 'rt_sigprocmask', 'rt_sigreturn', 'sigaltstack',
    'clock_gettime', 'clock_nanosleep', 'nanosleep', 'gettimeofday', 'time',
    'getpid', 'getppid', 'gettid', 'getuid', 'geteuid', 'getgid', 'getegid',
    'getresuid', 'getresgid', 'getgroups', 'uname', 'getcwd', 'chdir',
    'getrandom', 'futex', 'futex_time64', 'set_tid_address', 'set_robust_list',
    'rseq', 'arch_prctl', 'sched_yield', 'sched_getaffinity', 'getrusage',
    'getrlimit', 'prlimit64', 'fcntl', 'fcntl64', 'dup', 'dup2', 'dup3',
    'pipe', 'pipe2', 'poll', 'ppoll', 'select', 'pselect6', 'epoll_create',
    'epoll_create1', 'epoll_ctl', 'epoll_wait', 'epoll_pwait', 'epoll_pwait2',
    'eventfd', 'eventfd2', 'ioctl', 'ftruncate', 'truncate', 'fsync', 'fdatasync',
    'mkdir', 'mkdirat', 'unlink', 'unlinkat', 'rename', 'renameat', 'renameat2',
    'utimensat', 'fstatfs', 'statfs', 'umask', 'exit', 'exit_group',
)


class Comparison(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def install(library: str) -> None:
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise RuntimeError('unvalidated seccomp architecture')
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    allow, deny = 0x7fff0000, 0x00050000
    context = lib.seccomp_init(deny | errno.EPERM)
    if not context:
        raise RuntimeError('seccomp_init failed')

    def rule(name, action=allow, comparisons=()):
        syscall = lib.seccomp_syscall_resolve_name(name.encode('ascii'))
        if syscall < 0:
            return
        args = (Comparison * len(comparisons))(*comparisons) if comparisons else None
        result = lib.seccomp_rule_add_array(context, action, syscall, len(comparisons), args)
        if result != 0:
            raise RuntimeError(f'seccomp rule {name} failed: {result}')
    try:
        for name in ALLOW:
            rule(name)
        rule('clone3', deny | errno.ENOSYS)
        thread = 0x00010000
        namespaces = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000
        rule('clone', comparisons=(Comparison(0, 7, thread | namespaces, thread),))
        rule('kill', comparisons=(Comparison(0, 4, os.getpid(), 0),))
        rule('tgkill', comparisons=(Comparison(0, 4, os.getpid(), 0),))
        if lib.seccomp_load(context) != 0:
            raise RuntimeError('seccomp_load failed')
    finally:
        lib.seccomp_release(context)
