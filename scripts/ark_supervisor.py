"""Local native observer bridge. A game SIGABRT remains a failed/SIGABRT unit.

systemd MUST use KillMode=mixed: only this bridge receives the initial signal;
it forwards it once to the validated game, not to the debugger. Timeout kills
remain failures, and Restart=no prevents spontaneous replay of unsafe exits.
"""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import ark_config
from scripts import ark_shutdown as guard


def main():
    os.umask(0o077)
    root = Path(sys.argv[1]).resolve()
    guard.policy(root) or (_ for _ in ()).throw(guard.ShutdownError('Observer is not approved.'))
    url = ark_config.launch_url(root)
    run_id = uuid.uuid4().hex
    directory = root / guard.DIRECTORY
    live = {'run_id': run_id, 'supervisor_pid': os.getpid(), 'boot_id': guard.boot_id(),
            'invocation_id': os.environ.get('INVOCATION_ID', '')}
    if not re.fullmatch('[0-9a-f]{32}', live['invocation_id']):
        return 78
    cgroup = Path('/proc/self/cgroup').read_text().strip().split('/')[-1]
    if not re.fullmatch(r'ark-[A-Za-z0-9_-]+\.service', cgroup):
        return 78
    properties = subprocess.check_output(['/usr/bin/systemctl', '--user', 'show', cgroup,
        '-p', 'KillMode', '-p', 'Restart', '-p', 'KillSignal'], text=True, timeout=10)
    required = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
    if required != {'KillMode': 'mixed', 'Restart': 'no', 'KillSignal': '2'}:
        return 78
    guard.save(directory / 'live.json', live)
    pending = []
    def requested(number, frame):
        pending.append(number)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, requested)
    environment = os.environ | {'ARK_GUARD_ROOT': str(root), 'ARK_GUARD_RUN_ID': run_id,
                                 'ARK_GUARD_CODE_ROOT': str(guard.CODE_ROOT)}
    binary = root / guard.BINARY
    flags = ['-server', '-log', '-NoCrashDialog']
    # Isolated copies explicitly request loopback main-game binding, never a production override.
    if (root / 'lab.json').is_file():
        flags.append('-MultiHome=127.0.0.1')
    process = subprocess.Popen(['/usr/bin/gdb', '-q', '-nx', '--batch', '-x',
                                str(guard.CODE_ROOT / 'scripts/shutdown_trace.gdb'), '--args',
                                '/usr/bin/python3', str(guard.CODE_ROOT / 'scripts/ark_game_exec.py'),
                                str(root), url, *flags], cwd=binary.parent, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    live['debugger_pid'] = process.pid
    guard.save(directory / 'live.json', live)
    observation_path = directory / 'runtime' / (run_id + '.json')
    forwarded = False
    while process.poll() is None:
        if pending and not forwarded:
            try:
                observed = guard.private_json(observation_path)
                pid = observed.get('pid')
                if guard.process_matches(pid, root, process.pid):
                    os.kill(pid, pending[0])
                    forwarded = True
            except (OSError, ValueError):
                pass  # Never send a signal to an unverified PID.
        time.sleep(.1)
    observed = guard.private_json(observation_path)
    if process.returncode != 0 or observed.get('phase') != 'exited':
        return 70
    actual_signal = observed.get('exit_signal')
    if type(actual_signal) is int and 1 <= actual_signal < signal.NSIG:
        # Preserve the real abnormal result. This is NOT SuccessExitStatus or exit(0).
        signal.signal(actual_signal, signal.SIG_DFL)
        os.kill(os.getpid(), actual_signal)
        return 70
    code = observed.get('exit_code')
    return code if type(code) is int and 0 <= code <= 255 else 70


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('Native observation failed; shutdown is NOT qualified. Inspect private evidence.', file=sys.stderr)
        sys.exit(70)
