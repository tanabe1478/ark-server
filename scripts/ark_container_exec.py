"""Inferior lifetime lock/PDEATHSIG and Landlock: game cannot write proof/config."""
import ctypes
import fcntl
import os
from pathlib import Path
import signal
import sys


def restrict_files(root, libc):
    # ABI 3 includes truncate protection; fail closed if unavailable.
    if libc.syscall(444, 0, 0, 1) < 3:
        raise RuntimeError('Landlock ABI 3 is required.')
    class Ruleset(ctypes.Structure):
        _fields_ = [('handled', ctypes.c_uint64)]
    class Beneath(ctypes.Structure):
        _fields_ = [('allowed', ctypes.c_uint64), ('parent', ctypes.c_int)]
    access = (1 << 15) - 1
    readonly = (1 << 0) | (1 << 2) | (1 << 3)
    attr = Ruleset(access)
    rules = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if rules < 0:
        raise RuntimeError('Cannot create game filesystem rules.')
    def allow(path, rights):
        p = Path(path)
        if not p.exists():
            return
        if not p.is_dir():
            rights &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
        fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = Beneath(rights, fd)
            if libc.syscall(445, rules, 1, ctypes.byref(rule), 0):
                raise RuntimeError('Cannot install game filesystem rule.')
        finally:
            os.close(fd)
    if os.environ.get('ARK_COMPOSE') == '1':
        # The controller shares this PID namespace, not its readable files or
        # IPC authority. The game gets libraries, assets and its own temp home.
        for path in ('/usr', '/lib', '/lib64', '/etc', '/proc', '/sys', '/dev', root / 'server'):
            allow(path, readonly)
    else:
        allow('/', readonly)
    for path in (root / 'server/ShooterGame/Saved', '/tmp', '/dev/shm', '/dev/null', '/run/ark/home'):
        allow(path, access)
    if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, rules, 0):
        raise RuntimeError('Cannot restrict game filesystem.')
    os.close(rules)


def main():
    root = Path(sys.argv[1]).resolve()
    os.umask(0o077)
    if os.environ.get('ARK_LOCK_DIRECTORY'):
        lock = Path(os.environ['ARK_LOCK_DIRECTORY']) / '.server.lock'
    else:
        lock = root / '.server.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        return 70
    restrict_files(root, libc)
    binary = root / 'server/ShooterGame/Binaries/Linux/ShooterGameServer'
    os.execv(binary, [str(binary), *sys.argv[2:]])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(70)
