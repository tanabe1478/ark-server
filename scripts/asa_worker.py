"""One ASA invocation inside the persistent Compose controller.

Starts a private virtual display (ASA creates a window during startup and hangs
silently without one), then the sandboxed Proton game. It records the actual
wait result; stopping is requested over RCON by the controller, never by signal.
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

from . import ark_config, ark_game as game, ark_shutdown as n

DISPLAY = ':99'
READY_LINE = 'Server has completed startup and is now advertising for join'
LOG = Path('server/ShooterGame/Saved/Logs/ShooterGame.log')


def process_matches(pid, supervisor):
    try:
        path = Path('/proc') / str(pid)
        values = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines() if ':' in line)
        return (type(pid) is int and pid > 0 and int(values['PPid']) == supervisor
                and path.stat().st_uid == os.getuid()
                and b'/opt/proton/proton' in (path / 'cmdline').read_bytes().split(b'\0'))
    except (OSError, ValueError, KeyError):
        return False


def log_text(root, since_ns):
    """The current game log, only if this invocation rewrote it."""
    path = root / LOG
    try:
        if path.stat().st_mtime_ns < since_ns:
            return ''
        with path.open('rb') as stream:
            return stream.read(64 * 1024**2).decode('utf-8', errors='replace')
    except OSError:
        return ''


def check(root):
    from .ark_process import descriptor
    live = n.private_json(root / n.DIRECTORY / 'live.json')
    if (live.get('game') != 'asa' or live.get('deployment_id') != descriptor(root)['deployment_id']
            or live.get('boot_id') != n.boot_id()):
        raise n.ShutdownError('ASA worker deployment changed.')
    value = n.private_json(root / n.DIRECTORY / 'runtime' / (live['run_id'] + '.json'))
    if (value.get('phase') != 'running' or value.get('invocation_id') != live['invocation_id']
            or not process_matches(value.get('pid'), live['supervisor_pid'])):
        raise n.ShutdownError('ASA game process is unavailable.')
    return value


def ready(root, value):
    return READY_LINE in log_text(root, value['started_ns'])


def environment(root):
    home = Path('/run/ark/home')
    (home / 'Steam').mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / game.COMPAT).mkdir(mode=0o700, exist_ok=True)
    return os.environ | {'HOME': str(home), 'DISPLAY': DISPLAY,
        'STEAM_COMPAT_DATA_PATH': str(root / game.COMPAT), 'STEAM_COMPAT_CLIENT_INSTALL_PATH': str(home / 'Steam'),
        # Without the app id Proton skips its fixes; Xalia only fails on a headless display.
        'SteamAppId': str(game.APP_ID), 'SteamGameId': str(game.APP_ID), 'PROTON_USE_XALIA': '0', 'WINEDEBUG': '-all'}


def flags(root, private):
    settings = ark_config.load_settings(root)
    result = [f"-WinLiveMaxPlayers={settings['max_players']}", '-NoBattlEye', '-servergamelog', '-log']
    mods = ark_config.load_editable_settings(root)['active_mods']
    if mods:
        # Load order matters (stacking MODs first); IDs are validated as numeric.
        result.append('-mods=' + ','.join(mods))
    if private:
        result.append('-MultiHome=127.0.0.1')
    return result


def main():
    from .ark_process import descriptor
    os.umask(0o077)
    root = n.CODE_ROOT
    expected = int(os.environ['ARK_PARENT_PID'])
    libc = ctypes.CDLL(None, use_errno=True)
    if os.getppid() != expected or libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != expected:
        return 70
    config = descriptor(root)
    n.policy(root) or (_ for _ in ()).throw(n.ShutdownError('A reviewed game profile is required.'))
    invocation = os.environ.get('INVOCATION_ID', '')
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        return 78
    directory = root / n.DIRECTORY
    run = uuid.uuid4().hex
    live = {'run_id': run, 'supervisor_pid': os.getpid(), 'boot_id': n.boot_id(), 'game': 'asa',
            'invocation_id': invocation, 'deployment_id': config['deployment_id']}
    n.save(directory / 'live.json', live)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, signal.SIG_IGN)  # The controller stops ASA with RCON DoExit.
    env = environment(root)
    path = directory / 'runtime' / (run + '.json')
    display = subprocess.Popen(['/usr/bin/Xvfb', DISPLAY, '-screen', '0', '1024x768x16', '-nolisten', 'tcp'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        started = time.time_ns()
        with (directory / ('game-' + run + '.txt')).open('wb') as output:
            child = subprocess.Popen(['/usr/bin/python3', '-m', 'scripts.asa_exec', str(root),
                                      ark_config.launch_url(root), *flags(root, os.environ.get('ARK_PRIVATE_BOOTSTRAP') == '1')],
                                     cwd=n.CODE_ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
            record = {'run_id': run, 'invocation_id': invocation, 'phase': 'running', 'pid': child.pid, 'started_ns': started}
            n.save(path, record)
            code = child.wait()
        n.save(path, record | {'phase': 'exited', 'exit_signal': -code if code < 0 else None,
                               'exit_code': code if code >= 0 else None})
    finally:
        display.terminate()
        try:
            display.wait(timeout=10)
        except subprocess.TimeoutExpired:
            display.kill()
    return code if 0 <= code <= 255 else 70


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('ASA game invocation failed; inspect private evidence.', file=sys.stderr)
        raise SystemExit(70)
