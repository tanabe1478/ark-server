"""ASA inferior: lifetime lock/PDEATHSIG and Landlock, then Proton runs the Windows server.

The game may read its files and Proton, and write only Saved, its Wine prefix
and temp space; it cannot write proof, configuration or controller IPC.
"""
import ctypes
import fcntl
import os
from pathlib import Path
import signal
import sys

from . import ark_game as game
from .ark_container_exec import restrict_files


def main():
    root = Path(sys.argv[1]).resolve()
    os.umask(0o077)
    lock = Path(os.environ.get('ARK_LOCK_DIRECTORY') or root) / '.server.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        return 70
    restrict_files(root, libc, readable=(game.PROTON,),
                   writable=(root / game.COMPAT, *(root / path for path in game.RUNTIME_WRITABLE)))
    proton = game.PROTON / 'proton'
    os.execv('/usr/bin/python3', ['/usr/bin/python3', str(proton), 'run', str(root / game.BINARY), *sys.argv[2:]])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(70)
