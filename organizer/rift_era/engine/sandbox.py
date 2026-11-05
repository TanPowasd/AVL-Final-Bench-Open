"""Linux bwrap + delegated cgroup v2 + post-exec seccomp strategy launcher."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ctypes
import fcntl
import os
from pathlib import Path
import platform
import selectors
import shutil
import subprocess
import sys
import sysconfig
import time
import uuid

from .determinism import fingerprint
from .errors import InfrastructureFault


@dataclass(frozen=True)
class RuntimeMounts:
    python: Path
    stdlib: Path
    libraries: tuple[Path, ...]
    seccomp: Path
    digest: str


def runtime_mounts() -> RuntimeMounts:
    """Inspect only trusted host runtime binaries, never submission .so files."""
    executable = Path(sys.executable).resolve()
    stdlib = Path(sysconfig.get_path('stdlib')).resolve()
    seccomp_candidates = tuple(Path(p) for p in ('/lib/x86_64-linux-gnu/libseccomp.so.2',
        '/usr/lib/x86_64-linux-gnu/libseccomp.so.2', '/lib/aarch64-linux-gnu/libseccomp.so.2',
        '/usr/lib/aarch64-linux-gnu/libseccomp.so.2'))
    seccomp = next((p for p in seccomp_candidates if p.is_file()), None)
    if seccomp is None:
        raise InfrastructureFault('libseccomp not found for the frozen runtime')
    dependencies = {seccomp}
    binaries = [executable, seccomp, *sorted((stdlib / 'lib-dynload').glob('*.so'))]
    for binary in binaries:
        result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise InfrastructureFault(f'cannot inspect runtime dependencies: {binary.name}')
        for line in result.stdout.splitlines():
            if 'not found' in line:
                raise InfrastructureFault('runtime has an unresolved shared library')
            parts = line.strip().split()
            path = parts[2] if len(parts) > 2 and parts[1] == '=>' else parts[0] if parts else ''
            if path.startswith('/'):
                dependencies.add(Path(path))
    files = [executable, *sorted(dependencies), *sorted(p for p in stdlib.rglob('*')
             if p.is_file() and 'site-packages' not in p.parts and '__pycache__' not in p.parts)]
    digest = fingerprint({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    return RuntimeMounts(executable, stdlib, tuple(sorted(dependencies)), seccomp, digest)


class SandboxProcess:
    def __init__(self, process: subprocess.Popen, group: Path, runtime_hash: str, *, input_capacity_bytes: int = 0):
        self.process, self.group, self.runtime_hash = process, group, runtime_hash
        self.closed = False
        self.input_capacity_bytes = input_capacity_bytes

    def pause(self) -> None:
        """Stop every strategy thread between charged windows, in the kernel."""
        if self.closed or self.process.poll() is not None:
            return
        (self.group / 'cgroup.freeze').write_text('1')
        deadline = time.monotonic() + 5
        while True:
            values = dict(row.split() for row in (self.group / 'cgroup.events').read_text().splitlines())
            if values.get('frozen') == '1' or values.get('populated') == '0':
                return
            if time.monotonic() >= deadline:
                raise InfrastructureFault('strategy cgroup freezer did not acknowledge pause')
            time.sleep(.001)

    def resume(self) -> None:
        if self.closed:
            raise InfrastructureFault('closed sandbox cannot resume')
        (self.group / 'cgroup.freeze').write_text('0')

    def close(self) -> None:
        if self.closed:
            return
        try:
            (self.group / 'cgroup.kill').write_text('1')
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=10)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream:
                    stream.close()
            try:
                self.group.rmdir()
            except OSError as exc:
                raise InfrastructureFault('strategy cgroup did not become empty') from exc
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Sandbox:
    LIMITS = {'memory.max': '1073741824', 'memory.swap.max': '0',
              'cpu.max': '100000 100000', 'pids.max': '5', 'memory.oom.group': '1'}

    def __init__(self, delegated_cgroup: Path, mounts: RuntimeMounts | None = None):
        self.root = delegated_cgroup.resolve()
        self.mounts = mounts

    def preflight(self) -> None:
        if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64'):
            raise InfrastructureFault('formal sandbox requires a validated Linux architecture')
        if shutil.which('bwrap') is None:
            raise InfrastructureFault('bubblewrap is unavailable')
        if not (self.root / 'cgroup.controllers').is_file():
            raise InfrastructureFault('no delegated cgroup v2 root')
        libc = ctypes.CDLL(None, use_errno=True)
        libc.statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        libc.statfs.restype = ctypes.c_int
        info = ctypes.create_string_buffer(512)
        if libc.statfs(os.fsencode(self.root), info) != 0 or ctypes.c_long.from_buffer(info).value != 0x63677270:
            raise InfrastructureFault('delegated path is not a kernel cgroup v2 filesystem')
        if os.statvfs(self.root).f_flag & os.ST_RDONLY or not os.access(self.root, os.W_OK):
            raise InfrastructureFault('cgroup v2 delegation is read-only or not writable')
        controllers = set((self.root / 'cgroup.subtree_control').read_text().split())
        if not {'cpu', 'memory', 'pids'} <= controllers:
            raise InfrastructureFault('delegated root must enable cpu, memory and pids controllers')

    def start(self, submission: Path, *, start_paused: bool = False, input_capacity_bytes: int = 0) -> SandboxProcess:
        self.preflight()
        submission = submission.resolve()
        if not (submission / 'main.py').is_file():
            raise InfrastructureFault('submission must be safely extracted before sandbox launch')
        runtime = self.mounts or runtime_mounts()
        group = self.root / f'rift-{uuid.uuid4().hex}'
        process = None
        descriptors = []
        try:
            group.mkdir(mode=0o700)
            for name, value in self.LIMITS.items():
                (group / name).write_text(value)
                if (group / name).read_text().strip() != value:
                    raise InfrastructureFault(f'cgroup limit did not take effect: {name}')
            if not (group / 'cgroup.kill').exists():
                raise InfrastructureFault('cgroup.kill is required for complete cleanup')
            enter_fd = os.open(group / 'cgroup.procs', os.O_WRONLY)
            ack_read, ack_write = os.pipe()
            release_read, release_write = os.pipe()
            descriptors.extend((enter_fd, ack_read, ack_write, release_read, release_write))
            trusted = Path(__file__).resolve().parent
            args = ['bwrap', '--unshare-user', '--unshare-pid', '--unshare-net', '--unshare-ipc',
                    '--unshare-uts', '--as-pid-1', '--die-with-parent', '--new-session',
                    '--cap-drop', 'ALL', '--clearenv', '--setenv', 'PATH', str(runtime.python.parent),
                    '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', '--setenv', 'PYTHONHASHSEED', '0',
                    '--ro-bind', str(runtime.python), str(runtime.python),
                    '--ro-bind', str(runtime.stdlib), str(runtime.stdlib)]
            for library in runtime.libraries:
                args += ['--ro-bind', str(library), str(library)]
            for excluded in ('site-packages', 'dist-packages'):
                destination = runtime.stdlib / excluded
                if destination.exists():
                    args += ['--tmpfs', str(destination), '--remount-ro', str(destination)]
            args += ['--ro-bind', str(submission), '/submission',
                     '--ro-bind', str(trusted / 'sandbox_entry.py'), '/trusted/entry.py',
                     '--ro-bind', str(trusted / 'seccomp_filter.py'), '/trusted/seccomp_filter.py',
                     '--proc', '/proc', '--remount-ro', '/proc', '--dev', '/dev',
                     '--size', str(64 * 1024**2), '--tmpfs', '/work', '--dir', '/work/tmp',
                     '--symlink', '/work/tmp', '/tmp', '--remount-ro', '/', '--chdir', '/work',
                     # -S disables site imports. The environment is explicitly
                     # cleared and the script directory is trusted/read-only;
                     # avoid -I, which would ignore the fixed PYTHONHASHSEED.
                     str(runtime.python), '-S', '/trusted/entry.py', str(enter_fd),
                     str(ack_write), str(release_read), str(runtime.seccomp)]
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(enter_fd, ack_write, release_read), bufsize=0,
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            if input_capacity_bytes:
                try:
                    capacity = fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, input_capacity_bytes)
                except OSError as exc:
                    raise InfrastructureFault('host cannot reserve the bounded complete-snapshot input pipe') from exc
                if capacity < input_capacity_bytes:
                    raise InfrastructureFault('strategy input pipe reservation was smaller than requested')
                input_capacity_bytes = capacity
            os.close(enter_fd)
            descriptors.remove(enter_fd)
            os.close(ack_write)
            descriptors.remove(ack_write)
            os.close(release_read)
            descriptors.remove(release_read)
            with selectors.DefaultSelector() as selector:
                selector.register(ack_read, selectors.EVENT_READ)
                if not selector.select(15):
                    raise InfrastructureFault('sandbox trusted-launcher handshake timed out')
                if os.read(ack_read, 64) != b'SANDBOX_READY\n':
                    raise InfrastructureFault('sandbox setup failed before user code')
            tasks = (group / 'cgroup.procs').read_text().split()
            if len(tasks) != 1:
                raise InfrastructureFault('strategy cgroup must contain exactly one initial process')
            status = Path(f'/proc/{tasks[0]}/status').read_text()
            if 'Seccomp:\t2' not in status or 'NoNewPrivs:\t1' not in status:
                raise InfrastructureFault('kernel did not install the final seccomp boundary')
            managed = SandboxProcess(process, group, runtime.digest, input_capacity_bytes=input_capacity_bytes)
            if start_paused:
                managed.pause()
            os.write(release_write, b'1')
            return managed
        except Exception:
            if process is not None:
                try:
                    if (group / 'cgroup.kill').exists():
                        (group / 'cgroup.kill').write_text('1')
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream:
                            stream.close()
            if group.exists():
                group.rmdir()
            raise
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
