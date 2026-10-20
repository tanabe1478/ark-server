"""Owner-only local IPC for the containerized Bot, never a TCP/HTTP listener.

Only status/start/confirmed restart and rate-limited idle checks are accepted.
The Bot gets neither Docker socket, systemd bus, Saved, passwords nor evidence.
No control request is retried after a broken connection.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import threading
import time
import uuid

from .ark_automation import Automation, INTERVAL_SECONDS
from .ark_bot_config import BotConfig
from .ark_control import Actor, ControlError, Controller, response

LIMIT = 65536


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate IPC field.')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('IPC object required.')
    return value


class RemoteAutomation:
    def __init__(self, config, path, root):
        self.config, self.path, self.root = config, Path(path), Path(root)

    def validate(self, action, params, request_id, confirmed):
        if action not in ('status', 'start', 'restart'):
            raise ControlError('invalid_request')
        Controller(self.root).validate(action, params, request_id, confirmed)

    def call(self, value):
        sent = False
        try:
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ControlError('service_unavailable')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1400)
                client.connect(str(self.path))
                raw = json.dumps(value, separators=(',', ':')).encode() + b'\n'
                if len(raw) > LIMIT:
                    raise ControlError('invalid_request')
                sent = True
                client.sendall(raw)
                with client.makefile('rb') as stream:
                    result = stream.readline(LIMIT + 1)
                if not result.endswith(b'\n') or len(result) > LIMIT:
                    raise ValueError('Incomplete IPC response.')
                return decode(result)
        except (OSError, ValueError, ControlError):
            return response(value.get('request_id'), value.get('action'),
                            code='outcome_unknown' if sent else 'service_unavailable')

    def execute(self, action, params=None, *, request_id=None, confirmed=False, actor=None):
        request_id = request_id or uuid.uuid4().hex
        params = {} if params is None else params
        try:
            self.validate(action, params, request_id, confirmed)
        except ControlError as e:
            return response(request_id, action, code=e.code)
        return self.call({'method':'execute', 'action':action, 'params':params,
            'request_id':request_id, 'confirmed':confirmed, 'actor':asdict(actor) if actor is not None else None})

    def idle_check(self):
        return self.call({'method':'idle_check'})


class Dispatch:
    def __init__(self, automation, clock=time.monotonic):
        self.automation, self.clock = automation, clock
        self.last_idle = clock()
        self.idle_lock = threading.Lock()

    def __call__(self, value):
        if value == {'method':'idle_check'}:
            with self.idle_lock:
                if self.clock() - self.last_idle < INTERVAL_SECONDS:
                    return {'ok':True, 'data':{'changed':False, 'reason':'interval_not_due'}}
                self.last_idle = self.clock()
            return self.automation.idle_check()
        if set(value) != {'method', 'action', 'params', 'request_id', 'confirmed', 'actor'} or value['method'] != 'execute':
            raise ControlError('invalid_request')
        action = value['action']
        if action not in ('status','start','restart') or type(value['confirmed']) is not bool:
            raise ControlError('invalid_request')
        actor = None if value['actor'] is None else Actor(**value['actor'])
        if action != 'status':
            if (actor is None or actor.source != 'discord' or actor.guild_id != self.automation.config.guild_id):
                raise ControlError('forbidden')
        return self.automation.execute(action, value['params'], request_id=value['request_id'],
                                       confirmed=value['confirmed'], actor=actor)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 16
    def __init__(self, path, dispatch):
        self.dispatch, self.capacity = dispatch, threading.BoundedSemaphore(16)
        super().__init__(str(path), Handler)
        Path(path).chmod(0o600)
    def handle_error(self, request, address):
        pass  # Never log payloads, errors containing paths, IDs or secrets.


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        if not self.server.capacity.acquire(False):
            return
        try:
            pid, uid, gid = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            self.request.settimeout(10)
            raw = self.rfile.readline(LIMIT + 1)
            if len(raw) > LIMIT or not raw.endswith(b'\n'):
                return
            try:
                result = self.server.dispatch(decode(raw))
            except ControlError as e:
                result = response(None, None, code=e.code)
            except Exception:
                result = response(None, None, code='invalid_request')
            raw = json.dumps(result, separators=(',', ':')).encode() + b'\n'
            if len(raw) > LIMIT:
                return
            try:
                self.wfile.write(raw)
            except OSError:
                pass  # Accepted game operation remains recorded; no retry.
        finally:
            self.server.capacity.release()


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    directory = Path(f'/run/user/{os.getuid()}/ark-control')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Private IPC directory required.')
    path = directory / 'control.sock'
    # A separate lifetime lock prevents unlinking another receiver's live socket.
    from .ark_control import exclusive_lock
    with exclusive_lock(directory.parent / 'ark-control.lock'):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Unsafe IPC socket.')
            path.unlink()
        with Server(path, Dispatch(Automation(BotConfig.load(root)))) as server:
            def stop(number, frame):
                threading.Thread(target=server.shutdown, daemon=True).start()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, stop)
            address = os.environ.get('NOTIFY_SOCKET')
            if address:
                if address.startswith('@'):address='\0'+address[1:]
                with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as notifier:
                    notifier.connect(address);notifier.sendall(b'READY=1')
            print('Owner-only ARK IPC ready; no network management listener.', flush=True)
            server.serve_forever(poll_interval=.2)
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Local control IPC failed; inspect private local state.', flush=True)
        raise SystemExit(1)
