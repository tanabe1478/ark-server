"""Compose process backend: no host systemd and no Docker socket.

The controller container stays alive while the game is stopped. Schema 4
receipts bind the actual child wait result to native save/exit proof. They do
NOT invent a systemd exit or claim that the still-running container exited.
"""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_control import ControlError
from .ark_docker import verify_files
from .query import query_server

CONFIG = Path('config/process-runtime.json')
STATE = Path('logs/process/state.json')
LAUNCH = Path('logs/process/launch.json')
BOOTSTRAP = Path('config/process-bootstrap.json')
TRANSITION = Path('config/process-transition.json')
QUALIFICATION = Path('logs/process/production-qualified.json')
FILES = tuple(sorted(set(n.OBSERVER_FILES) | {
    'ark_config.py', 'ark_control.py', 'ark_automation.py', 'ark_parent_exec.py',
    'ark_container_exec.py', 'ark_container_observer.py', 'container_trace.gdb',
    'ark_process.py', 'ark_process_worker.py', 'ark_spawn.py', 'compose_profiles.py', 'compose_integrity.py', 'compose_server.py', 'compose_platform.py',
}))


def oom_kills():
    try:
        values=dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
        value=int(values['oom_kill'])
        return value if value>=0 else None
    except (OSError,ValueError,KeyError):
        return None


def contract():
    return {name: n.digest(n.CODE_ROOT / 'scripts' / name) for name in FILES}


def build_identity():
    value = n.private_json(n.CODE_ROOT / 'build-identity.json')
    if (set(value) != {'schema', 'build_id', 'os_packages_sha256'} or type(value.get('schema')) is not int or value['schema'] != 1
            or re.fullmatch('[0-9a-f]{32}', value.get('build_id', '')) is None
            or re.fullmatch('[0-9a-f]{64}', value.get('os_packages_sha256', '')) is None):
        raise n.ShutdownError('Runtime image identity is unavailable.')
    return value


def descriptor(root):
    value = n.private_json(root / CONFIG)
    if (set(value) != {'schema', 'deployment_id', 'source_contract', 'image_build'}
            or type(value['schema']) is not int or value['schema'] != 1
            or re.fullmatch('[0-9a-f]{32}', value.get('deployment_id', '')) is None
            or value['source_contract'] != contract() or value['image_build'] != build_identity()):
        raise n.ShutdownError('Compose runtime contract changed.')
    return value


def bootstrap(root):
    """Explicit setup only; never called as a recovery shortcut at server boot."""
    if (root / LAUNCH).exists() or (root / STATE).exists() or (root / BOOTSTRAP).exists():
        raise n.ShutdownError('Existing invocation or bootstrap must not be overwritten.')
    policy = n.install_policy(root)
    runtime = {'schema': 1, 'deployment_id': uuid.uuid4().hex, 'source_contract': contract(), 'image_build': build_identity()}
    n.save(root / CONFIG, runtime)
    folder = root / 'backups' / ('compose-bootstrap-' + uuid.uuid4().hex)
    files = n.capture(root / n.SAVED, folder)
    n.save(root / BOOTSTRAP, {'schema': 1, 'purpose': 'explicit_initial_compose_setup',
        'runtime': runtime, 'native_policy': policy, 'configuration': n.configuration(root),
        'files': files, 'in_game_content_verified': False})
    return runtime


class ProcessBackend:
    def __init__(self, root=n.CODE_ROOT, *, on_qualified=None, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
        from .ark_spawn import PersistentSpawner
        self.root, self.on_qualified, self.popen, self.clock, self.sleep = Path(root), on_qualified, PersistentSpawner(popen), clock, sleep
        self.child = None
        self.required_ini = None
        self.required_progress = None
        self.platform_generation = None
        self.lock = threading.RLock()
        self.ready_timeout = 600

    def _poll(self):
        with self.lock:
            if self.child is not None and self.child.poll() is not None:
                value = n.private_json(self.root / STATE)
                if value['phase'] != 'exited':
                    code = self.child.returncode
                    count=oom_kills();baseline=value.get('oom_kill_baseline')
                    killed=count>baseline if type(count) is int and type(baseline) is int and count>=baseline else None
                    value.update(phase='exited', supervisor_returncode=code, oom_killed=killed,
                                 exited_monotonic=self.clock())
                    n.save(self.root / STATE, value)

    def state(self):
        with self.lock:
            return self._state_locked()

    def _state_locked(self):
        self._poll()
        path = self.root / STATE
        if not path.exists():
            return {'ActiveState': 'inactive', 'Result': 'not-launched', 'MainPID': 0,
                    'InvocationID': '', 'Runtime': 'compose-process', 'ObserverReady': False,
                    'SupervisorReturnCode': None, 'PrivateBootstrap': False, 'OOMKilled': False}
        value = n.private_json(path)
        active = self.child is not None and self.child.poll() is None
        code = value.get('supervisor_returncode')
        known_exit = value.get('phase') == 'exited' and type(code) is int
        ready = False
        if active:
            try:
                from .ark_process_worker import check
                check(self.root)
                ready = True
            except (OSError, ValueError, KeyError):
                pass
        killed=value.get('oom_killed')
        if active:
            count,baseline=oom_kills(),value.get('oom_kill_baseline')
            killed=count>baseline if type(count) is int and type(baseline) is int and count>=baseline else None
        return {'ActiveState': 'active' if active else ('inactive' if known_exit and code == 0 else 'failed'),
                'Result': 'success' if active or known_exit and code == 0 else ('signal' if known_exit and code < 0 else 'unknown'),
                'MainPID': self.child.pid if active else 0, 'InvocationID': value.get('invocation_id', ''),
                'Runtime': 'compose-process', 'ObserverReady': ready,
                'SupervisorReturnCode': code if known_exit else None,
                'PrivateBootstrap': value.get('private_bootstrap') is True, 'OOMKilled': killed}

    def metrics(self):
        state = self.state()
        value = n.private_json(self.root / STATE) if (self.root / STATE).exists() else {}
        memory = None
        try:
            memory = int(Path('/sys/fs/cgroup/memory.current').read_text())
        except (OSError, ValueError):
            pass
        started = value.get('started_monotonic')
        uptime = max(0, self.clock() - started) if (state['MainPID'] and value.get('boot_id') == n.boot_id()
                    and type(started) in (int, float)) else None
        return {'pid': state['MainPID'], 'uptime_seconds': uptime, 'service_memory': memory,
                'restarts': 0, 'exit_status': state['SupervisorReturnCode'], 'cpu_usage_ns': None}

    def backup_timer(self):
        return {'known': True, 'state': 'inactive', 'enabled': False}

    def shutdown_guard(self):
        return ProcessShutdownGuard(self.root, self)

    def _launch(self, private=False):
        # Polling must not attribute the OLD child's exit to a new durable
        # launch intent while its Popen is still being created.
        with self.lock:
            return self._launch_locked(private)

    def _launch_locked(self, private=False):
        if self.platform_generation is not None:
            from .compose_platform import active
            try:active(self.platform_generation)
            except (OSError,ValueError):raise ControlError('automation_blocked') from None
        self.shutdown_guard().verify_startable(self.state())
        c.launch_url(self.root)
        from .compose_integrity import capture, progress
        self.required_ini = capture(self.root)
        self.required_progress = progress(self.root)
        with c.server_lock(self.root):
            pass
        invocation = uuid.uuid4().hex
        launch = {'schema': 1, 'invocation_id': invocation, 'boot_id': n.boot_id(),
                  'runtime': descriptor(self.root), 'issued_monotonic': self.clock()}
        # Durable intent before spawn: disappearance/unknown outcomes block replay.
        n.save(self.root / LAUNCH, launch)
        n.save(self.root / STATE, {'phase': 'starting', 'invocation_id': invocation,
            'boot_id': n.boot_id(), 'started_monotonic': self.clock(),
            'supervisor_returncode': None, 'private_bootstrap': private, 'oom_kill_baseline': oom_kills()})
        environment = os.environ | {'INVOCATION_ID': invocation, 'ARK_PARENT_PID': str(os.getpid()),
                                    'ARK_COMPOSE': '1', 'ARK_PRIVATE_BOOTSTRAP': '1' if private else '0'}
        output = (self.root / 'logs/process' / ('worker-' + invocation + '.txt')).open('wb')
        try:
            self.child = self.popen(['/usr/bin/python3', '-m', 'scripts.ark_process_worker'],
                cwd=n.CODE_ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            output.close()

    def verify_readiness(self):
        from .compose_integrity import verify
        if self.required_ini is None or self.required_progress is None or self.state().get('OOMKilled') is not False:
            raise n.ShutdownError('Current configuration or resource evidence is unavailable.')
        verify(self.root, self.required_ini)

    def _ready(self):
        deadline = self.clock() + self.ready_timeout
        while self.clock() < deadline:
            state = self.state()
            if state['ActiveState'] != 'active':
                raise n.ShutdownError('Game exited before readiness.')
            try:
                settings = c.load_settings(self.root)
                info = query_server('127.0.0.1', settings['query_port'], timeout=2)
                if (state['ObserverReady'] and info.get('map') == settings['map']
                        and info.get('max_players') == settings['max_players']
                        and info.get('password_required') is True and type(info.get('players')) is int and info['players']==0):
                    self.verify_readiness()
                    return
            except (OSError, ValueError):
                pass
            self.sleep(1)
        # Never kill/restart merely because readiness is unavailable.
        raise n.ShutdownError('Readiness timeout; the invocation is not replayed.')

    def production_qualified(self):
        try:
            value = n.private_json(self.root / QUALIFICATION)
            return (type(value.get('schema')) is int and value['schema'] == 1 and value.get('runtime') == descriptor(self.root)
                    and value.get('native_policy') is not None and value['native_policy'] == n.policy(self.root))
        except (OSError, ValueError):
            return False

    def operate(self, action):
        if action == 'start':
            if not self.production_qualified():
                # First start of a fresh runtime: no public UDP binding until a
                # REAL save/exit has been qualified on this installation.
                try:
                    self._launch(private=True)
                    self._ready()
                    self.shutdown_guard().stop(self)
                    from .compose_integrity import verify_progress
                    verify_progress(self.root,self.required_progress)
                except (OSError, ValueError, ControlError):
                    # A partial private bootstrap may still be alive. Keep its
                    # durable launch and block, never accept an active/no-op as
                    # evidence that public startup completed.
                    raise ControlError('outcome_unknown') from None
                n.save(self.root / QUALIFICATION, {'schema': 1, 'runtime': descriptor(self.root),
                    'native_policy': n.policy(self.root), 'receipt': n.private_json(self.root / n.DIRECTORY / 'last-stop.json'),
                    'in_game_content_verified': False})
                if self.on_qualified is not None:
                    self.on_qualified()
            self._launch(private=False)
        elif action == 'stop':
            if self.child is None or self.child.poll() is not None:
                raise ControlError('not_running')
            self.child.send_signal(signal.SIGINT)
            try:
                self.child.wait(timeout=180)
            except subprocess.TimeoutExpired:
                raise ControlError('outcome_unknown') from None
            self._poll()
        else:
            raise ControlError('invalid_request')


class ProcessShutdownGuard:
    def __init__(self, root, backend):
        self.root, self.backend = Path(root), backend
        self.directory = self.root / n.DIRECTORY

    def _current(self):
        return descriptor(self.root), n.policy(self.root), n.configuration(self.root)

    def verify_stopped(self, state):
        try:
            return self._verify_receipt(state)
        except (OSError, ValueError):
            # A never-launched installation or a qualified one-shot runtime
            # transition is startable, but is NOT a fresh native clean exit.
            if not (self.root / LAUNCH).exists():
                return self.verify_startable(state)
            runtime, policy, config = self._current()
            return self.verify_transition(state, runtime, policy, config)

    def _verify_receipt(self, state):
        runtime, policy, config = self._current()
        value = n.private_json(self.directory / 'last-stop.json')
        if (type(value.get('schema')) is not int or value['schema'] != 4
                or value.get('clean_exit') is not False or value.get('warning') != 'expected_native_abort'
                or value.get('runtime') != runtime or value.get('native_policy') != policy
                or value.get('configuration') != config or value.get('service_record') != state
                or type(state.get('MainPID')) is not int or state['MainPID'] != 0
                or state.get('Runtime') != 'compose-process' or state.get('Result') != 'signal' or state.get('OOMKilled') is not False
                or type(state.get('SupervisorReturnCode')) is not int or state['SupervisorReturnCode'] != -6):
            raise n.ShutdownError('Compose save/exit receipt does not match.')
        snapshot = value.get('snapshot', '')
        if not re.fullmatch('backups/process-shutdown-[0-9a-f]{32}', snapshot):
            raise n.ShutdownError('Invalid preservation path.')
        folder = self.root / snapshot
        if n.private_json(folder / 'receipt.json') != value:
            raise n.ShutdownError('Preserved receipt differs.')
        live = n.private_json(self.directory / 'live.json')
        observed = n.private_json(self.directory / 'runtime' / (live['run_id'] + '.json'))
        if (value['observation'] != observed or value['intent']['run_id'] != live['run_id']
                or value['intent']['invocation_id'] != state['InvocationID']
                or live.get('deployment_id') != runtime['deployment_id']):
            raise n.ShutdownError('Native invocation changed.')
        before = n.private_json(folder / 'before/manifest.json', 8 * 1024**2)['files']
        after = n.private_json(folder / 'after/manifest.json', 8 * 1024**2)['files']
        if not n.observed_proof(observed, value['intent'], value['world'], before, after, policy['native_points']):
            raise n.ShutdownError('Native save/exit proof is incomplete.')
        verify_files(self.root, after)
        return {'clean_exit': False, 'warning': 'expected_native_abort', 'snapshot': snapshot,
                'save_completion_observed': True, 'in_game_content_verified': False}

    def verify_startable(self, state):
        if (type(state.get('MainPID')) is not int or state['MainPID'] != 0
                or state.get('Runtime')!='compose-process' or state.get('ActiveState') not in ('inactive', 'failed')):
            raise n.ShutdownError('A stopped game is required.')
        runtime, policy, config = self._current()
        if policy is None:
            raise n.ShutdownError('A reviewed game profile is required.')
        if not (self.root / LAUNCH).exists():
            if (self.root / TRANSITION).exists():
                return self.verify_transition(state, runtime, policy, config)
            value = n.private_json(self.root / BOOTSTRAP)
            if (type(value.get('schema')) is not int or value['schema'] != 1
                    or value.get('purpose') != 'explicit_initial_compose_setup'
                    or value.get('runtime') != runtime or value.get('native_policy') != policy
                    or value.get('configuration') != config or (self.root / STATE).exists()):
                raise n.ShutdownError('First-start approval changed.')
            verify_files(self.root, value['files'])
            return {'clean_exit': False, 'warning': 'reviewed_initial_setup'}
        try:
            return self.verify_stopped(state)
        except (OSError, ValueError):
            return self.verify_transition(state, runtime, policy, config)

    def verify_transition(self, state, runtime, policy, config):
        # An explicit one-shot transition, never permission to ignore a crash.
        value = n.private_json(self.root / TRANSITION)
        if (type(state.get('MainPID')) is not int or state['MainPID']!=0
                or state.get('Runtime')!='compose-process' or state.get('ActiveState') not in ('inactive','failed')
                or type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('runtime') != runtime or value.get('native_policy') != policy
                or value.get('configuration') != config or value.get('previous_state') != state
                or not ((value.get('purpose')=='qualified_update' and value.get('qualification',{}).get('two_cycles_passed') is True)
                        or (value.get('purpose')=='owner_configuration' and value.get('explicit_owner_confirmation') is True
                            and value.get('previous_runtime')==runtime and value.get('previous_native_policy')==policy))):
            raise n.ShutdownError('Update transition does not match.')
        if (self.root / LAUNCH).exists():
            launch = n.private_json(self.root / LAUNCH)
            if launch.get('invocation_id') != state.get('InvocationID'):
                raise n.ShutdownError('Update transition has already been consumed.')
        elif state.get('InvocationID') != '' or (self.root / STATE).exists():
            raise n.ShutdownError('Missing launch evidence is not an initial setup.')
        verify_files(self.root, value['files'])
        return {'clean_exit': False, 'warning': 'owner_configuration_change' if value['purpose']=='owner_configuration' else 'qualified_runtime_update'}

    def stop(self, backend, recheck=None):
        from .ark_process_worker import check
        runtime, policy, config = self._current()
        state = backend.state()
        observed = check(self.root)
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
        before = n.capture(self.root / n.SAVED, folder / 'before')
        if recheck is not None:
            reason = recheck()
            if reason is not None:
                return {'changed': False, 'reason': reason}
        if backend.state() != state:
            raise n.ShutdownError('Process changed during preservation.')
        intent = {'run_id': live['run_id'], 'nonce': nonce, 'game_pid': observed['pid'],
                  'supervisor_pid': live['supervisor_pid'], 'invocation_id': state['InvocationID'],
                  'boot_id': n.boot_id(), 'monotonic': time.monotonic()}
        n.save(folder / 'intent.json', intent)
        n.save(self.directory / 'intent.json', intent)
        backend.operate('stop')
        stopped = backend.state()
        with c.server_lock(self.root):
            after = n.capture(self.root / n.SAVED, folder / 'after')
            observed = n.private_json(self.directory / 'runtime' / (live['run_id'] + '.json'))
            world = 'SavedArks/' + c.load_settings(self.root)['map'] + '.ark'
            if (stopped.get('OOMKilled') is not False or stopped['SupervisorReturnCode'] != -6 or type(stopped['SupervisorReturnCode']) is not int
                    or stopped['MainPID'] != 0 or stopped['InvocationID'] != state['InvocationID']
                    or n.configuration(self.root) != config
                    or not n.observed_proof(observed, intent, world, before, after, policy['native_points'])):
                raise n.ShutdownError('Unexpected process exit or incomplete save evidence.')
            value = {'schema': 4, 'clean_exit': False, 'warning': 'expected_native_abort',
                     'runtime': runtime, 'native_policy': policy, 'configuration': config,
                     'service_record': stopped, 'snapshot': str(folder.relative_to(self.root)),
                     'world': world, 'intent': intent, 'observation': observed,
                     'in_game_content_verified': False}
            n.save(folder / 'receipt.json', value)
            n.save(self.directory / 'last-stop.json', value)
        return {'changed': True, 'service': stopped, 'shutdown': self.verify_stopped(stopped),
                'readiness_verified': False, 'warnings': ['expected_native_abort']}
