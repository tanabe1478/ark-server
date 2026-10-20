"""One observed game invocation inside the persistent Compose controller.

The supervisor preserves the game's actual OS signal. The controller remains
alive after a guarded stop; it does not turn SIGABRT into a clean game exit.
"""
import ctypes
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from . import ark_config, ark_shutdown as n


def check(root, preparing=False):
    from .ark_container_supervisor import process_matches
    from .ark_process import descriptor
    live = n.private_json(root / n.DIRECTORY / 'live.json')
    if (live.get('deployment_id') != descriptor(root)['deployment_id']
            or live.get('boot_id') != n.boot_id()):
        raise n.ShutdownError('Observer deployment changed.')
    value = n.private_json(root / n.DIRECTORY / 'runtime' / (live['run_id'] + '.json'))
    if (value.get('phase') not in (('preparing', 'running') if preparing else ('running',))
            or value.get('invocation_id') != live['invocation_id']
            or not process_matches(value.get('pid'), root, live['debugger_pid'], preparing)):
        raise n.ShutdownError('Game process or native observer is unavailable.')
    return value


def main():
    from .ark_process import descriptor
    os.umask(0o077)
    root = n.CODE_ROOT
    if sys.argv[1:] in (['check'], ['process']):
        check(root, sys.argv[1:] == ['process'])
        return 0
    expected = int(os.environ['ARK_PARENT_PID'])
    libc = ctypes.CDLL(None, use_errno=True)
    if os.getppid() != expected or libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != expected:
        return 70
    config = descriptor(root)
    n.policy(root) or (_ for _ in ()).throw(n.ShutdownError('A reviewed native profile is required.'))
    invocation = os.environ.get('INVOCATION_ID', '')
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        return 78
    home = Path('/run/ark/home')
    sdk = home / '.steam/sdk64'
    sdk.mkdir(mode=0o700, parents=True, exist_ok=True)
    link = sdk / 'steamclient.so'
    if not link.exists() and not link.is_symlink():
        link.symlink_to(root / 'server/linux64/steamclient.so')
    directory = root / n.DIRECTORY
    run = uuid.uuid4().hex
    live = {'run_id': run, 'supervisor_pid': os.getpid(), 'boot_id': n.boot_id(),
            'invocation_id': invocation, 'deployment_id': config['deployment_id']}
    n.save(directory / 'live.json', live)
    pending = []
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda number, frame: pending.append(number))
    environment = os.environ | {'ARK_GUARD_ROOT': str(root), 'ARK_GUARD_RUN_ID': run,
                               'ARK_GUARD_CODE_ROOT': str(n.CODE_ROOT)}
    with (directory / ('debugger-' + run + '.txt')).open('wb') as output:
        child = subprocess.Popen(['/usr/bin/python3', '-m', 'scripts.ark_parent_exec', str(os.getpid()),
                                  '/usr/bin/gdb', '-q', '-nx', '--batch', '-x',
                                  str(n.CODE_ROOT / 'scripts/container_trace.gdb')], cwd=n.CODE_ROOT,
                                 env=environment, stdout=output, stderr=subprocess.STDOUT)
        live['debugger_pid'] = child.pid
        n.save(directory / 'live.json', live)
        forwarded = False
        while child.poll() is None:
            if pending and not forwarded:
                try:
                    observed = check(root, preparing=True)
                    os.kill(observed['pid'], pending[0])
                    forwarded = True
                except (OSError, ValueError):
                    pass
            time.sleep(.1)
    observed = n.private_json(directory / 'runtime' / (run + '.json'))
    if child.returncode != 0 or observed.get('phase') != 'exited':
        return 70
    actual = observed.get('exit_signal')
    if type(actual) is int and 1 <= actual < signal.NSIG:
        signal.signal(actual, signal.SIG_DFL)
        os.kill(os.getpid(), actual)
        return 70
    code = observed.get('exit_code')
    return code if type(code) is int and 0 <= code <= 255 else 70


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Observed game invocation failed; inspect private evidence.', file=sys.stderr)
        raise SystemExit(70)
