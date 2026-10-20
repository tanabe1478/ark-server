"""Runs INSIDE GDB. Fixed native observation points; no exit interception/repair."""
import os
from pathlib import Path
import time

from . import ark_shutdown as guard


def install():
    import gdb
    root = Path(os.environ['ARK_GUARD_ROOT'])
    run_id = os.environ['ARK_GUARD_RUN_ID']
    invocation = os.environ.get('INVOCATION_ID', '')
    path = root / guard.DIRECTORY / 'runtime' / (run_id + '.json')
    world = root / guard.SAVED / 'SavedArks' / (guard.ark_config.load_settings(root)['map'] + '.ark')
    record = {'run_id': run_id, 'invocation_id': invocation, 'phase': 'starting', 'pid': None}

    def write():
        guard.save(path, record)

    def started(event):
        pid = gdb.selected_inferior().pid
        if pid:
            record.update(phase='running', pid=pid)
            write()
    gdb.events.new_thread.connect(started)

    def point():
        intent = guard.private_json(root / guard.DIRECTORY / 'intent.json')
        if (intent.get('run_id') != run_id or intent.get('game_pid') != gdb.selected_inferior().pid
                or intent.get('invocation_id') != invocation or intent.get('boot_id') != guard.boot_id()):
            return None
        return {'nonce': intent['nonce'], 'monotonic': time.monotonic(),
                'thread': gdb.selected_thread().global_num,
                'game_mode': int(gdb.parse_and_eval('$r14'))}

    class NativePoint(gdb.Breakpoint):
        def __init__(self, address, kind):
            super().__init__('*' + hex(address), internal=True)
            self.kind = kind
        def stop(self):
            try:
                value = point()
                if value is not None:
                    if self.kind == 'abort':
                        frame = gdb.newest_frame()
                        value.update(pc=int(frame.pc()), caller=int(frame.older().pc()))
                    elif self.kind == 'save_complete':
                        info = world.stat()
                        value.update(world_bytes=info.st_size, world_mtime_ns=info.st_mtime_ns)
                    record[self.kind] = value
                    write()
            except FileNotFoundError:
                pass  # A periodic save without an armed stop is not shutdown evidence.
            except Exception:
                # Poison qualification, but never suppress a game instruction or signal.
                record['observation_fault'] = True
                try:
                    write()
                except Exception:
                    pass
            return False

    armed = False
    def executable_loaded(event):
        nonlocal armed
        if not armed and Path(event.new_objfile.filename).resolve() == root / guard.BINARY:
            points = guard.native_points(root)
            NativePoint(points['save_begin'], 'save_begin')
            NativePoint(points['save_complete'], 'save_complete')
            NativePoint(points['force_abort'], 'abort')
            armed = True
    gdb.events.new_objfile.connect(executable_loaded)
    write()
    global finish
    def finish():
        signal_value = gdb.parse_and_eval('$_exitsignal')
        code_value = gdb.parse_and_eval('$_exitcode')
        record.update(phase='exited', exited_monotonic=time.monotonic(),
                      exit_signal=None if signal_value.type.code == gdb.TYPE_CODE_VOID else int(signal_value),
                      exit_code=None if code_value.type.code == gdb.TYPE_CODE_VOID else int(code_value))
        write()
