"""Set a Linux parent-death chain before exec, without preexec_fn in threads."""
import ctypes
import os
import signal
import sys


def main():
    expected = int(sys.argv[1])
    if expected <= 1 or os.getppid() != expected:
        return 70
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != expected:
        return 70
    os.execv(sys.argv[2], sys.argv[2:])


if __name__ == '__main__':
    raise SystemExit(main())
