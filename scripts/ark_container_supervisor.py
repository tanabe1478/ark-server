"""Container-local native observer. Tini retains signal exit as 128+signal.

No systemd is emulated inside the image. The host validates the real unit and
container; this process verifies local PIDs/tracer and a host-runner lease.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from . import ark_config
from . import ark_shutdown as native
from .ark_docker import load, STATE, CONTAINER_ROOT


def process_matches(pid, root, tracer, preparing=False):
    if native.process_matches(pid, root, tracer):
        return True
    if not preparing or type(pid) is not int or pid <= 0:
        return False
    try:
        path = Path('/proc') / str(pid)
        values = dict(line.split(':',1) for line in (path / 'status').read_text().splitlines() if ':' in line)
        return ((path / 'exe').resolve() == root / native.BINARY and path.stat().st_uid == os.getuid()
                and int(values['TracerPid']) == 0 and int(values['PPid']) == tracer)
    except (OSError, ValueError, KeyError):
        return False


def check(root, preparing=False):
    live = native.private_json(root / native.DIRECTORY / 'live.json')
    config = load(root)
    if live.get('container_deployment') != config['deployment_id'] or live.get('boot_id') != native.boot_id():
        raise native.ShutdownError('Wrong container observer.')
    observed = native.private_json(root / native.DIRECTORY / 'runtime' / (live['run_id'] + '.json'))
    if (observed.get('phase') not in (('running','preparing') if preparing else ('running',))
            or observed.get('invocation_id') != live['invocation_id']
            or not process_matches(observed.get('pid'), root, live['debugger_pid'], preparing)):
        raise native.ShutdownError('Unverified container game/tracer.')
    return {'ok':True, 'run_id':live['run_id'], 'pid':observed['pid']}


def main():
    os.umask(0o077)
    root = Path(CONTAINER_ROOT)
    if sys.argv[1:] in (['check'], ['health'], ['process']):
        result = check(root, preparing=sys.argv[1:] == ['process'])
        if sys.argv[1:] == ['health']:
            from .query import query_server
            settings = ark_config.load_settings(root)
            ark_config.launch_url(root)
            game = query_server('127.0.0.1', settings['query_port'], timeout=2)
            if (game.get('map') != settings['map'] or game.get('max_players') != settings['max_players']
                    or game.get('password_required') is not True):
                return 1
        print(json.dumps(result))
        return 0
    config = load(root)
    native.policy(root) or (_ for _ in ()).throw(native.ShutdownError('Native observation is not approved.'))
    home = Path('/run/ark/home')
    sdk = home / '.steam/sdk64'
    sdk.mkdir(mode=0o700, parents=True, exist_ok=True)
    (sdk / 'steamclient.so').symlink_to(root / 'server/linux64/steamclient.so')
    invocation = os.environ.get('INVOCATION_ID', '')
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        return 78
    run = uuid.uuid4().hex
    live = {'run_id':run, 'supervisor_pid':os.getpid(), 'boot_id':native.boot_id(),
            'invocation_id':invocation, 'container_deployment':config['deployment_id']}
    directory = root / native.DIRECTORY
    native.save(directory / 'live.json', live)
    pending = []
    def requested(number, frame):
        pending.append(number)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, requested)
    environment = os.environ | {'ARK_GUARD_ROOT':str(root), 'ARK_GUARD_RUN_ID':run,
                               'ARK_GUARD_CODE_ROOT':str(root)}
    debugger_output = (directory / ('debugger-' + run + '.txt')).open('wb')
    process = subprocess.Popen(['/usr/bin/gdb', '-q', '-nx', '--batch', '-x',
        str(root / 'scripts/container_trace.gdb')], cwd=(root / native.BINARY).parent,
        env=environment, stdout=debugger_output, stderr=subprocess.STDOUT)
    live['debugger_pid'] = process.pid
    native.save(directory / 'live.json', live)
    observation = directory / 'runtime' / (run + '.json')
    forwarded = False
    while process.poll() is None:
        try:
            lease = native.private_json(root / STATE / 'lease.json')
            valid = (lease.get('invocation_id') == invocation and lease.get('boot_id') == native.boot_id()
                     and type(lease.get('monotonic')) in (int,float) and 0 <= time.monotonic() - lease['monotonic'] <= 20)
        except (OSError, ValueError):
            valid = False
        if not valid and not pending:
            native.save(root / STATE / 'lease-fault.json', {'run_id':run, 'invocation_id':invocation,
                                                        'reason':'host_runner_lease_lost'})
            pending.append(signal.SIGTERM)
        if pending and not forwarded:
            try:
                value = native.private_json(observation)
                if process_matches(value.get('pid'), root, process.pid, preparing=True):
                    os.kill(value['pid'], pending[0])
                    forwarded = True
            except (OSError, ValueError):
                pass
        time.sleep(.1)
    observed = native.private_json(observation)
    if process.returncode != 0 or observed.get('phase') != 'exited':
        return 70
    actual_signal = observed.get('exit_signal')
    if type(actual_signal) is int and 1 <= actual_signal < signal.NSIG:
        signal.signal(actual_signal, signal.SIG_DFL)
        os.kill(os.getpid(), actual_signal)
        return 70
    value = observed.get('exit_code')
    return value if type(value) is int and 0 <= value <= 255 else 70


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('Container native observation failed; inspect private evidence.', file=sys.stderr)
        sys.exit(70)
