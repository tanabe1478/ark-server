"""ASA process backend and save/exit proof.

ASE's proof watches native save points with gdb; a Windows server under Proton
offers none. ASA's proof is instead: RCON SaveWorld answers "World Saved", the
game log gains a new "World Save Complete", the world file is rewritten, then
RCON DoExit and an actual exit code 0 with no OOM kill. Any gap blocks startup.
"""
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from . import ark_config as c, ark_game as game, ark_shutdown as n, asa_rcon as rcon
from .ark_control import ControlError
from .ark_docker import verify_files
from .ark_process import ProcessBackend, ProcessShutdownGuard
from .query import QueryError

PROFILE = 'asa-proton-v1'
SAVE_LINE = 'World Save Complete'
MANIFEST = Path('server/steamapps/appmanifest_2430930.acf')
_digests = {}


def binary_digest(path):
    # The server binary is large; rehash only when it changes on disk.
    info = path.stat()
    key = (str(path), info.st_size, info.st_mtime_ns, info.st_ino)
    if key not in _digests:
        _digests.clear()
        _digests[key] = n.digest(path)
    return _digests[key]


def expected(root, observer_contract):
    root = Path(root)
    build = re.search(r'"buildid"\s+"(\d+)"', (root / MANIFEST).read_text(errors='replace'))
    version = (game.PROTON / 'version').read_text().strip()
    if build is None or not version or len(version) > 128:
        raise n.ShutdownError('ASA build or Proton identity is unavailable.')
    return {'schema': 1, 'enabled': True, 'game': 'asa', 'profile': PROFILE,
            'binary_sha256': binary_digest(root / game.BINARY), 'build_id': build.group(1),
            'proton': version, 'observer_contract': observer_contract, 'native_points': {}}


class AsaBackend(ProcessBackend):
    WORKER = 'scripts.asa_worker'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_timeout = 1200  # A first Astraeos start took about 3.5 minutes.
        self.exit_timeout = 300

    def observer_check(self):
        from .asa_worker import check
        return check(self.root)

    def _ready(self):
        from .asa_worker import ready
        deadline = self.clock() + self.ready_timeout
        while self.clock() < deadline:
            state = self.state()
            if state['ActiveState'] != 'active':
                raise n.ShutdownError('Game exited before readiness.')
            try:
                if state['ObserverReady'] and ready(self.root, self.observer_check()) and rcon.players(self.root) == []:
                    self.verify_readiness()
                    return
            except (OSError, ValueError):
                pass
            self.sleep(5)
        # Never kill/restart merely because readiness is unavailable.
        raise n.ShutdownError('Readiness timeout; the invocation is not replayed.')

    def operate(self, action):
        if action != 'stop':
            return super().operate(action)
        if self.child is None or self.child.poll() is not None:
            raise ControlError('not_running')
        try:
            reply = rcon.command('DoExit', rcon.admin_password(self.root), timeout=10)
        except (OSError, QueryError):
            raise ControlError('outcome_unknown') from None
        if not reply.startswith('Exiting'):
            raise ControlError('outcome_unknown')
        try:
            self.child.wait(timeout=self.exit_timeout)
        except subprocess.TimeoutExpired:
            raise ControlError('outcome_unknown') from None
        self._poll()


class AsaShutdownGuard(ProcessShutdownGuard):
    def _verify_receipt(self, state):
        runtime, policy, config = self._current()
        value = n.private_json(self.directory / 'last-stop.json')
        if (type(value.get('schema')) is not int or value['schema'] != 5 or value.get('game') != 'asa'
                or value.get('clean_exit') is not True or value.get('warning') is not None
                or value.get('runtime') != runtime or value.get('native_policy') != policy
                or value.get('configuration') != config or value.get('service_record') != state
                or type(state.get('MainPID')) is not int or state['MainPID'] != 0
                or state.get('Runtime') != 'compose-process' or state.get('ActiveState') != 'inactive'
                or state.get('Result') != 'success' or state.get('OOMKilled') is not False
                or type(state.get('SupervisorReturnCode')) is not int or state['SupervisorReturnCode'] != 0
                or value.get('save', {}).get('confirmed') is not True):
            raise n.ShutdownError('ASA save/exit receipt does not match.')
        snapshot = value.get('snapshot', '')
        if not re.fullmatch('backups/process-shutdown-[0-9a-f]{32}', snapshot):
            raise n.ShutdownError('Invalid preservation path.')
        folder = self.root / snapshot
        if n.private_json(folder / 'receipt.json') != value:
            raise n.ShutdownError('Preserved receipt differs.')
        live = n.private_json(self.directory / 'live.json')
        if (value['intent']['run_id'] != live['run_id'] or value['intent']['invocation_id'] != state['InvocationID']
                or live.get('deployment_id') != runtime['deployment_id']):
            raise n.ShutdownError('ASA invocation changed.')
        verify_files(self.root, n.private_json(folder / 'after/manifest.json', 8 * 1024**2)['files'])
        return {'clean_exit': True, 'warning': None, 'snapshot': snapshot,
                'save_completion_observed': True, 'in_game_content_verified': False}

    def _save(self, world, before):
        """RCON SaveWorld, then a NEW log completion and a rewritten world file."""
        from .asa_worker import log_text
        started = self.backend.observer_check()['started_ns']
        completed = log_text(self.root, started).count(SAVE_LINE)
        reply = rcon.command('SaveWorld', rcon.admin_password(self.root), timeout=120)
        if reply != 'World Saved':
            raise n.ShutdownError('SaveWorld was not acknowledged.')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = n.inventory(self.root / n.SAVED).get(world)
            if (log_text(self.root, started).count(SAVE_LINE) > completed and current is not None
                    and current['bytes'] >= 4096 and (before is None or current['mtime_ns'] > before['mtime_ns'])):
                return {'confirmed': True, 'rcon_reply': reply, 'world': world, 'world_bytes': current['bytes'],
                        'world_mtime_ns': current['mtime_ns']}
            time.sleep(1)
        raise n.ShutdownError('World save completion was not observed.')

    def stop(self, backend, recheck=None):
        runtime, policy, config = self._current()
        state = backend.state()
        live = n.private_json(self.directory / 'live.json')
        if state['ActiveState'] != 'active' or not state['ObserverReady'] or live['invocation_id'] != state['InvocationID']:
            raise n.ShutdownError('Observed process does not match the controller.')
        previous = self.directory / 'intent.json'
        if previous.exists() and n.private_json(previous).get('run_id') == live['run_id']:
            raise n.ShutdownError('Stop intent already exists; never replay it.')
        entries = n.inventory(self.root / n.SAVED)
        if shutil.disk_usage(self.root / 'backups').free < sum(x['bytes'] for x in entries.values()) * 3 + 1024**3:
            raise n.ShutdownError('Insufficient preservation space.')
        nonce = uuid.uuid4().hex
        folder = self.root / 'backups' / ('process-shutdown-' + nonce)
        n.capture(self.root / n.SAVED, folder / 'before')
        if recheck is not None:
            reason = recheck()
            if reason is not None:
                return {'changed': False, 'reason': reason}
        if backend.state() != state:
            raise n.ShutdownError('Process changed during preservation.')
        intent = {'run_id': live['run_id'], 'nonce': nonce, 'game_pid': backend.observer_check()['pid'],
                  'supervisor_pid': live['supervisor_pid'], 'invocation_id': state['InvocationID'],
                  'boot_id': n.boot_id(), 'monotonic': time.monotonic()}
        n.save(folder / 'intent.json', intent)
        n.save(self.directory / 'intent.json', intent)
        world = str(game.world(c.load_settings(self.root)['map']))
        try:
            save = self._save(world, n.inventory(self.root / n.SAVED).get(world))
        except (OSError, QueryError) as error:
            raise n.ShutdownError('World save could not be confirmed.') from error
        backend.operate('stop')
        stopped = backend.state()
        with c.server_lock(self.root):
            after = n.capture(self.root / n.SAVED, folder / 'after')
            final = after.get(world)
            if (stopped.get('OOMKilled') is not False or type(stopped['SupervisorReturnCode']) is not int
                    or stopped['SupervisorReturnCode'] != 0 or stopped['MainPID'] != 0
                    or stopped['InvocationID'] != state['InvocationID']
                    or final is None or final['bytes'] < 4096 or final['mtime_ns'] < save['world_mtime_ns']):
                raise n.ShutdownError('Unexpected process exit or incomplete save evidence.')
            # ASA rewrites all its INIs on exit (adding defaults), so file hashes
            # change. Every pre-start entry and managed value must still be there;
            # the receipt then binds the rewritten files the next start will use.
            from .compose_integrity import verify
            if backend.required_ini is None:
                raise n.ShutdownError('Pre-start configuration evidence is unavailable.')
            try:
                verify(self.root, backend.required_ini)
            except ValueError as error:
                raise n.ShutdownError('ASA changed existing configuration on exit.') from error
            config = n.configuration(self.root)
            value = {'schema': 5, 'game': 'asa', 'clean_exit': True, 'warning': None,
                     'runtime': runtime, 'native_policy': policy, 'configuration': config,
                     'service_record': stopped, 'snapshot': str(folder.relative_to(self.root)),
                     'world': world, 'intent': intent, 'save': save, 'in_game_content_verified': False}
            n.save(folder / 'receipt.json', value)
            n.save(self.directory / 'last-stop.json', value)
        return {'changed': True, 'service': stopped, 'shutdown': self.verify_stopped(stopped),
                'readiness_verified': False, 'warnings': []}


AsaBackend.GUARD = AsaShutdownGuard
