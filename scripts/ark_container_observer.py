"""Inside GDB: start untraced, then attach as the game's actual parent.

Steam's client bootstrap can issue a debugger-only startup trap in a headless
container. Do NOT swallow that signal. Let normal startup complete without a
tracer, then install the same native save/abort observations before any stop.
An unavailable game is left running; readiness timeouts do not kill/restart it.
"""
import os
from pathlib import Path
import subprocess
import time

from . import ark_config, ark_shutdown as native
from .query import query_server


def run():
    import gdb
    root = Path(os.environ['ARK_GUARD_ROOT'])
    run_id = os.environ['ARK_GUARD_RUN_ID']
    invocation = os.environ['INVOCATION_ID']
    path = root / native.DIRECTORY / 'runtime' / (run_id + '.json')
    game = subprocess.Popen(['/usr/bin/python3', str(root / 'scripts/ark_container_exec.py'),
        str(root), ark_config.launch_url(root), '-server', '-log', '-NoCrashDialog'],
        cwd=(root / native.BINARY).parent)
    native.save(path, {'run_id':run_id, 'invocation_id':invocation, 'phase':'preparing', 'pid':game.pid})
    while True:
        code = game.poll()
        if code is not None:
            # Actual OS wait result; never a qualified stop without native points.
            native.save(path, {'run_id':run_id, 'invocation_id':invocation, 'phase':'exited', 'pid':game.pid,
                'exit_signal':-code if code < 0 else None, 'exit_code':code if code >= 0 else None,
                'exited_monotonic':time.monotonic()})
            return
        try:
            settings = ark_config.load_settings(root)
            info = query_server('127.0.0.1', settings['query_port'], timeout=1)
            ready = (info.get('map') == settings['map'] and info.get('max_players') == settings['max_players']
                     and info.get('password_required') is True)
        except (OSError, ValueError):
            ready = False
        if ready:
            from . import shutdown_observer
            shutdown_observer.install()  # Arm new-objfile before attaching to loaded ELF.
            gdb.execute('attach ' + str(game.pid))
            gdb.execute('continue')
            shutdown_observer.finish()
            return
        time.sleep(1)
