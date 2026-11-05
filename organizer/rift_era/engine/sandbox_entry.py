"""Trusted sandbox entrypoint, mounted read-only as /trusted/entry.py.

Only this file and seccomp_filter.py are mounted; organizer state is not present.
The cgroup descriptor is consumed and closed before the program can run.
"""

import os
import resource
import runpy
import sys


def main():
    cgroup_fd, ack_fd, release_fd = map(int, sys.argv[1:4])
    seccomp_library = sys.argv[4]
    # '0' identifies THIS process even inside a PID namespace. The bwrap monitor
    # stays outside the strategy leaf and does not consume its five-task quota.
    os.write(cgroup_fd, b'0\n')
    os.close(cgroup_fd)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    sys.path.insert(0, '/trusted')
    from seccomp_filter import install
    install(seccomp_library)
    os.write(ack_fd, b'SANDBOX_READY\n')
    os.close(ack_fd)
    # Parent attests kernel state while this trusted process is still alive.
    # User exit/OOM after release is a strategy fault, not a launch-time fault.
    if os.read(release_fd, 1) != b'1':
        raise RuntimeError('sandbox parent did not authorize execution after attestation')
    os.close(release_fd)
    sys.path.remove('/trusted')
    sys.path.insert(0, '/submission')
    sys.argv = ['/submission/main.py']
    os.chdir('/work')
    runpy.run_path('/submission/main.py', run_name='__main__')


if __name__ == '__main__':
    main()
