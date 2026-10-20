"""Compose controller + observed game. No Docker socket, host bus or TCP API."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import sys
import threading
import time
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_process import ProcessBackend, descriptor, QUALIFICATION
from .ark_automation import Automation
from .ark_bot_config import BotConfig
from .ark_control import Controller, ControlError, response, exclusive_lock
from .ark_control_ipc import Server, Dispatch, decode, LIMIT

PENDING = Path('logs/updates/pending.json')
DESIRED = Path('logs/process/desired.json')
BOT_CONFIG = Path('/run/ark-bot-config/discord-bot.json')
BOT_SOCKET = Path('/run/ark-control/control.sock')
ADMIN_SOCKET = Path('/run/ark-admin/control.sock')


def settings(root=n.CODE_ROOT):
    if (root/'config/compose-config.pending.json').exists():
        raise n.ShutdownError('Interrupted owner configuration requires explicit recovery.')
    value = n.private_json(root / 'config/compose.json')
    expected = {'schema', 'auto_start', 'auto_stop', 'update_interval_seconds', 'update_game',
                'update_mods', 'update_images', 'source_repository', 'minimum_free_memory_bytes'}
    if (set(value) != expected or type(value['schema']) is not int or value['schema'] != 1
            or any(type(value[k]) is not bool for k in ('auto_start', 'auto_stop', 'update_game', 'update_mods', 'update_images'))
            or type(value['update_interval_seconds']) is not int or not 300 <= value['update_interval_seconds'] <= 86400
            or type(value['minimum_free_memory_bytes']) is not int or value['minimum_free_memory_bytes'] < 1024**3
            or value['source_repository'] != 'tanabe1478/ark-server'):
        raise n.ShutdownError('Compose settings are invalid.')
    return value


def base_bot(root=n.CODE_ROOT):
    value = n.private_json(root / 'config/discord-request.json')
    return BotConfig(**value)


def desired(root, value=None):
    path = root / DESIRED
    if value is not None:
        n.save(path, {'schema': 1, 'running': bool(value)})
    if path.exists():
        record = n.private_json(path)
        if set(record) != {'schema', 'running'} or record['schema'] != 1 or type(record['running']) is not bool:
            raise n.ShutdownError('Invalid desired state.')
        return record['running']
    return settings(root)['auto_start']


class ComposeAutomation(Automation):
    closing = False

    def problem(self):
        if self.closing or (self.root / PENDING).exists() or (self.root/'logs/automation/platform-fault.json').exists():
            return 'automation_blocked'
        return super().problem()

    def execute(self, action, *args, **kwargs):
        result = super().execute(action, *args, **kwargs)
        if action in ('start', 'restart') and result['ok']:
            desired(self.root, True)
        return result

    def idle_check(self):
        result = super().idle_check()
        if result.get('ok') and result.get('data', {}).get('changed'):
            desired(self.root, False)
        return result


class AdminDispatch:
    """Separate socket/volume from the Bot; no shell, paths or token handling."""
    def __init__(self, automation):
        self.a = automation
        self.root = automation.root

    def __call__(self, value):
        if value == {'method': 'status'}:
            return self.a.status()
        if set(value) == {'method'} and value['method'] in ('start', 'stop', 'restart'):
            if value['method'] == 'stop':
                with self.a._exclusive():
                    if self.a.closing or (self.root/PENDING).exists():
                        raise ControlError('automation_blocked')
                    # Owner-only guarded stop remains available after a failed
                    # startup. It NEVER clears the original automation latch.
                    result = self.a.controller.execute('stop', confirmed=True)
                    if result['ok']:
                        desired(self.root, False)
                    return result
            return self.a.execute(value['method'], confirmed=value['method'] == 'restart')
        if set(value) != {'method', 'job'} or value['method'] not in ('prepare_update', 'abort_update', 'cancel_update', 'commit_update'):
            raise ControlError('invalid_request')
        job = value['job']
        if not isinstance(job, str) or re.fullmatch('[0-9a-f]{32}', job) is None:
            raise ControlError('invalid_request')
        with self.a._exclusive():
            path = self.root / PENDING
            if value['method'] == 'prepare_update':
                if self.a.problem():
                    raise ControlError('automation_blocked')
                was_running = self.a.controller.backend.state()['ActiveState'] == 'active'
                pending = {'schema': 1, 'job': job, 'phase': 'preparing', 'was_running': was_running,
                           'desired_running': desired(self.root), 'configuration': n.configuration(self.root)}
                n.save(path, pending)
                if was_running:
                    result = self.a.controller.execute('stop.idle',
                        {'minimum_uptime_seconds': self.a.config.startup_grace_seconds}, confirmed=True)
                    if not result['ok']:
                        return result  # Retain the pending intent on unknown/failed stops.
                    if not result['data']['changed']:
                        pending['phase'] = 'deferred'
                        n.save(self.root / 'logs/updates/history' / (job + '.json'), pending)
                        path.unlink()
                        c.sync_directory(path.parent)
                        return result
                state = self.a.controller.backend.state()
                self.a.controller.verify_startable_stopped(state)
                folder = self.root / 'backups' / ('update-input-' + job)
                with c.server_lock(self.root):
                    files = n.capture(self.root / n.SAVED, folder)
                pending.update(phase='prepared', previous_state=state, files=files,
                               runtime=descriptor(self.root), native_policy=n.policy(self.root))
                n.save(path, pending)
                return {'ok': True, 'data': {'changed': True, 'job': job, 'was_running': was_running}}
            pending = n.private_json(path)
            if pending.get('job') != job or pending.get('phase') not in ('prepared', 'applying'):
                raise ControlError('outcome_unknown')
            from .ark_docker import verify_files
            if value['method'] in ('abort_update','cancel_update'):
                # A failed CANDIDATE can resume the unchanged old release. This
                # never restores or rolls back Saved, nor clears a runtime fault.
                if pending['phase'] != 'prepared' or descriptor(self.root) != pending['runtime']:
                    raise ControlError('outcome_unknown')
                verify_files(self.root, pending['files'])
                self.a.controller.verify_startable_stopped(self.a.controller.backend.state())
                pending['phase'] = 'candidate_cancelled' if value['method']=='cancel_update' else 'candidate_rejected'
            else:
                self.a.controller.verify_startable_stopped(self.a.controller.backend.state())
                pending['phase'] = 'committed'
            n.save(self.root / 'logs/updates/history' / (job + '.json'), pending)
            path.unlink()
            c.sync_directory(path.parent)
            resume = pending['was_running'] and value['method']!='cancel_update'
        # Re-enter through normal durable automation, outside its exclusive
        # lock. Never replay a partially transmitted start after a disconnect.
        if resume:
            return self.a.execute('start')
        return {'ok': True, 'data': {'changed': True, 'running': False}}


class IsolatedServer(Server):
    def __init__(self, path, dispatch, admin=False):
        self.admin = admin
        super().__init__(path, dispatch)

    def verify_request(self, request, address):
        pid, uid, gid = struct.unpack('3i', request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        # Cross-container peers in sibling PID namespaces appear as PID 0.
        # The game lives in OUR namespace and may not forge a local caller.
        # Read-only health checks are handled by a distinct local health file.
        return uid == os.getuid() and pid == 0


def call(value, path=ADMIN_SOCKET, timeout=1500):
    sent = False
    try:
        info = path.lstat()
        import stat
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Invalid management socket.')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(timeout)
            stream.connect(str(path))
            sent = True
            stream.sendall(json.dumps(value, separators=(',', ':')).encode() + b'\n')
            with stream.makefile('rb') as incoming:
                raw = incoming.readline(LIMIT + 1)
            if not raw.endswith(b'\n') or len(raw) > LIMIT:
                raise ValueError('Incomplete response.')
            return decode(raw)
    except (OSError, ValueError):
        return response(None, None, code='outcome_unknown' if sent else 'service_unavailable')


def rootless():
    rows = [tuple(map(int, line.split())) for line in Path('/proc/self/uid_map').read_text().splitlines()]
    if os.getuid() != 0 or not any(inside == 0 and outside > 0 and count == 1 for inside, outside, count in rows):
        raise n.ShutdownError('A rootless Docker user namespace is required.')
    import ctypes
    if os.uname().machine!='x86_64' or ctypes.CDLL(None,use_errno=True).syscall(444,None,0,1)<3:
        raise n.ShutdownError('x86-64 Linux with Landlock ABI 3 or newer is required.')
    from .ark_process import oom_kills
    if oom_kills() is None:
        raise n.ShutdownError('Readable cgroup v2 OOM evidence is required.')


def serve():
    rootless()
    root = n.CODE_ROOT
    config = settings(root)
    base = base_bot(root)
    backend = ProcessBackend(root)
    controller = Controller(root, backend=backend)
    automation = ComposeAutomation(base, controller)
    def effective():
        verified = backend.production_qualified()
        automation.config = replace(base, shutdown_verified=verified,
            auto_stop_enabled=config['auto_stop'] and verified and base.notification_channel_id is not None)
        n.save(BOT_CONFIG, automation.config.__dict__)
    from .compose_platform import active as platform_active,state as platform_state
    generation=platform_active()
    backend.platform_generation=generation
    backend.on_qualified = effective
    effective()
    closed = threading.Event()
    lifecycle_fault=[]
    def closing(number, frame):
        closed.set()
        try:
            from .compose_lifecycle import controller_stopping
            controller_stopping(root)
        except (OSError,ValueError):lifecycle_fault.append(True)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, closing)
    # Acquire BEFORE touching socket paths: a second controller must not unlink
    # a live receiver's socket even when it subsequently loses the lock.
    with exclusive_lock(root / '.controller.lock'):
        for path in (BOT_SOCKET, ADMIN_SOCKET):
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.parent.chmod(0o700)
            if path.exists():
                import stat
                if not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != os.getuid():
                    raise n.ShutdownError('Unsafe stale IPC entry.')
                path.unlink()
        bot = IsolatedServer(BOT_SOCKET, Dispatch(automation))
        admin = IsolatedServer(ADMIN_SOCKET, AdminDispatch(automation), admin=True)
        servers = [bot, admin]
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        health=root/'logs/process/broker-health.json'
        identity={'pid':os.getpid(),'boot_id':n.boot_id(),'hostname':socket.gethostname(),'platform_generation':generation,'broker_run_id':uuid.uuid4().hex}
        n.save(health,identity|{'monotonic':time.monotonic(),'ready':False,'closing':False})
        startup = None
        if desired(root) and not (root / PENDING).exists():
            startup = threading.Thread(target=lambda: automation.execute('start'), daemon=False)
            startup.start()
        health = root / 'logs/process/broker-health.json'
        print('Compose controller ready; Unix IPC only; Discord networking belongs to the Bot.', flush=True)
        while not closed.wait(.5):
            try:
                if platform_state(generation)['status']=='closing':closing(None,None);break
            except (OSError,ValueError):
                lifecycle_fault.append(True)
                n.save(root/'logs/automation/platform-fault.json',{'generation':generation,'reason':'unknown_platform_lease','automatic_retry_performed':False})
                closing(None,None);break
            backend._poll()
            state = backend.state()
            usable = (not desired(root) or (root / PENDING).exists()
                      or state['ActiveState'] == 'active' and state['ObserverReady'] and not state['PrivateBootstrap'])
            n.save(health,identity|{'monotonic':time.monotonic(),'ready':bool(usable),'closing':False})
        automation.closing = True
        n.save(health,identity|{'ready':False,'closing':True})
        for server in servers:
            server.shutdown()
        for server in servers:
            server.server_close()  # Drain accepted operations, never replay them.
        if startup is not None:
            startup.join(timeout=1400)
            if startup.is_alive():
                return 70
        state = backend.state()
        def drained(startable):
            current=backend.state()
            if type(current.get('MainPID')) is int and current['MainPID']==0 and current['ActiveState']!='active':
                with c.server_lock(root):
                    n.save(root/'logs/process/platform-drained.json',identity|{'stopped':True,'startable_verified':startable})
        if state['ActiveState'] == 'active':
            result = controller.execute('stop', confirmed=True)
            if not result['ok']:
                drained(False);return 70
            state = backend.state()
        try:controller.verify_startable_stopped(state)
        except Exception:
            drained(False);raise
        # Writer exit is separate from qualification. An unknown SIGSEGV can
        # drain infrastructure with result 70, but NEVER permits another start.
        if lifecycle_fault:drained(False);return 70
        drained(True)
        return 134 if state.get('SupervisorReturnCode') == -6 else 0


def health():
    value = n.private_json(n.CODE_ROOT / 'logs/process/broker-health.json')
    return (value.get('ready') is True and value.get('closing') is False and value.get('boot_id') == n.boot_id()
            and type(value.get('monotonic')) in (int, float) and 0 <= time.monotonic() - value['monotonic'] < 10
            and type(value.get('pid')) is int and Path('/proc/' + str(value['pid'])).exists())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('serve', 'health', 'status', 'start', 'stop', 'restart'))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'serve':
            return serve()
        if args.action == 'health':
            return 0 if health() else 1
        result = call({'method': args.action})
        print(json.dumps(result))
        return 0 if result['ok'] else 1
    except Exception:
        print('Compose control failed; inspect private local evidence. No retry.', file=sys.stderr)
        return 70


if __name__ == '__main__':
    raise SystemExit(main())
