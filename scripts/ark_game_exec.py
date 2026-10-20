"""Debugger inferior launcher: preserve lifetime lock and prevent orphan writers."""
import ctypes
import fcntl
import os
from pathlib import Path
import signal
import sys


def isolate_lab(root, libc):
    """Landlock, no sudo/user namespace: copied MODs cannot write/read production Saved.

    Fail closed when the kernel cannot enforce it. Existing system streams and
    the lifetime lock remain usable. Only lab data, temp and Steam caches may be
    written; broad system directories are read/execute-only.
    """
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError('Landlock ABI 3 required for the data-copy test.')
    class Ruleset(ctypes.Structure):
        _fields_ = [('handled', ctypes.c_uint64)]
    class Beneath(ctypes.Structure):
        _fields_ = [('allowed', ctypes.c_uint64), ('parent', ctypes.c_int)]
    all_access = (1 << 15) - 1
    readonly = (1 << 0) | (1 << 2) | (1 << 3)
    attribute = Ruleset(all_access)
    rules = libc.syscall(444, ctypes.byref(attribute), ctypes.sizeof(attribute), 0)
    if rules < 0:
        raise RuntimeError('Cannot create laboratory filesystem rules.')
    def allow(path, access):
        path = Path(path).resolve()
        if not path.exists():
            return
        if not path.is_dir():
            access &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = Beneath(access, fd)
            if libc.syscall(445, rules, 1, ctypes.byref(rule), 0) != 0:
                raise RuntimeError('Cannot install laboratory filesystem rule.')
        finally:
            os.close(fd)
    allow(root, all_access)
    for path in ('/usr', '/lib', '/lib64', '/etc', '/proc', '/sys', '/dev', '/run'):
        allow(path, readonly)
    for path in ('/tmp', '/dev/shm', '/dev/null'):
        allow(path, all_access)
    code_root = Path(__file__).resolve().parents[1]
    for suffix in ('.steam', '.steam/sdk64', '.steam/sdk32', '.local/share/Steam'):
        path = Path.home() / suffix
        allow(path, readonly)
        if not path.resolve().is_relative_to(code_root):
            allow(path, all_access)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, rules, 0) != 0:
        raise RuntimeError('Cannot restrict laboratory filesystem.')
    os.close(rules)


def main():
    root = Path(sys.argv[1]).resolve()
    os.umask(0o077)
    fd = os.open(root / '.server.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        return 70
    if (root / 'lab.json').is_file():
        isolate_lab(root, libc)
    binary = root / 'server/ShooterGame/Binaries/Linux/ShooterGameServer'
    os.execv(str(binary), [str(binary), *sys.argv[2:]])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(70)
