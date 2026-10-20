"""Narrow rootless-Docker transport; no socket is mounted in the game container.

The real systemd and Docker exits are retained. A container exit 134 is NOT a
clean exit and only qualifies with the independently observed game SIGABRT/save.
No update, pull, arbitrary exec, restart policy, RCON or shell in normal operation.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import time
import uuid

from . import ark_config
from . import ark_shutdown as native
from .ark_control import SystemdBackend, ControlError, GAME_UNIT

CONFIG = Path('config/docker-runtime.json')
STATE = Path('logs/docker')
CONTAINER_ROOT = '/opt/ark'
SOURCE_FILES = tuple('scripts/' + n for n in native.OBSERVER_FILES) + (
    'scripts/ark_config.py', 'scripts/ark_docker.py', 'scripts/ark_docker_runner.py',
    'scripts/ark_container_supervisor.py', 'scripts/ark_container_exec.py',
    'scripts/ark_container_observer.py', 'scripts/container_trace.gdb',
    'deploy/docker/Dockerfile.runtime',
)


def source_contract():
    return {p: native.digest(native.CODE_ROOT / p) for p in SOURCE_FILES}


def contract_id(contract):
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load(root):
    pending = Path(root) / 'config/docker-cutover.pending.json'
    if pending.exists() or pending.is_symlink():
        raise native.ShutdownError('An interrupted Docker cutover requires explicit recovery.')
    value = native.private_json(Path(root) / CONFIG)
    if (set(value) != {'schema', 'deployment_id', 'container_name', 'image', 'source_contract', 'bind_address'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not re.fullmatch('[0-9a-f]{32}', value['deployment_id'])
            or not re.fullmatch('ark-[a-z0-9][a-z0-9-]{0,47}', value['container_name'])
            or not re.fullmatch('sha256:[0-9a-f]{64}', value['image'])
            or value['source_contract'] != source_contract() or value['bind_address'] not in ('127.0.0.1','0.0.0.0')):
        raise native.ShutdownError('Docker runtime contract changed; revalidation required.')
    return value


class Engine:
    def __init__(self):
        self.endpoint = f'unix:///run/user/{os.getuid()}/ark-docker.sock'

    def call(self, *args, timeout=30):
        environment = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'XDG_RUNTIME_DIR') if k in os.environ}
        try:
            result = subprocess.run(['/usr/bin/docker', '--host', self.endpoint, *args],
                capture_output=True, text=True, timeout=timeout, env=environment)
        except subprocess.TimeoutExpired:
            raise ControlError('outcome_unknown') from None
        except OSError:
            raise ControlError('service_unavailable') from None
        if result.returncode:
            raise ControlError('operation_failed')
        return result.stdout

    def trusted(self):
        info = json.loads(self.call('info', '--format', '{{json .SecurityOptions}}'))
        if 'name=rootless' not in info or not any(x.startswith('name=seccomp,') for x in info):
            raise native.ShutdownError('Only a rootless engine is permitted.')
        path = Path(self.endpoint.removeprefix('unix://'))
        s = path.lstat()
        if not stat.S_ISSOCK(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o007:
            raise native.ShutdownError('Untrusted Docker endpoint.')

    def inspect(self, name):
        # Do not interpret an arbitrary inspect failure as "no container".
        ids = self.call('container', 'ls', '--all', '--no-trunc', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}').splitlines()
        if not ids:
            return None
        if len(ids) != 1 or not re.fullmatch('[0-9a-f]{64}', ids[0]):
            raise native.ShutdownError('Ambiguous Docker instance.')
        return json.loads(self.call('inspect', ids[0]))[0]


def validate_instance(root, config, inspected):
    labels = inspected.get('Config', {}).get('Labels') or {}
    host = inspected.get('HostConfig', {})
    if (inspected.get('Image') != config['image']
            or labels.get('dev.ark.deployment') != config['deployment_id']
            or not re.fullmatch('[0-9a-f]{32}', labels.get('dev.ark.invocation', ''))
            or labels.get('dev.ark.contract') != contract_id(config['source_contract'])
            or host.get('RestartPolicy', {}).get('Name') != 'no'
            or host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True
            or host.get('Init') is not True or host.get('CapAdd')
            or host.get('CapDrop') != ['ALL']
            or host.get('SecurityOpt') != ['no-new-privileges']
            or host.get('NetworkMode') != 'bridge' or host.get('PidMode') not in ('',None)
            or host.get('IpcMode') != 'private' or host.get('Devices') or host.get('DeviceRequests')
            or inspected.get('Config', {}).get('StopSignal') != 'SIGINT'):
        raise native.ShutdownError('Docker instance does not match the reviewed runtime.')
    settings = ark_config.load_settings(root)
    ports = (settings['game_port'], settings['game_port'] + 1, settings['query_port'])
    bindings = {str(p)+'/udp':[{'HostIp':config['bind_address'],'HostPort':str(p)}] for p in ports}
    if host.get('PortBindings') != bindings or any(m.get('Type') != 'bind' for m in inspected.get('Mounts', [])):
        raise native.ShutdownError('Unexpected network or data exposure.')
    expected = mounts(root)
    actual = {m['Destination']: (m['Source'], m['RW']) for m in inspected.get('Mounts', []) if m.get('Type') == 'bind'}
    if actual != {target: (str(source), writable) for source, target, writable in expected}:
        raise native.ShutdownError('Unexpected Docker bind mounts.')
    return labels['dev.ark.invocation']


def mounts(root):
    root = Path(root).resolve()
    return [(root / 'server', CONTAINER_ROOT + '/server', False),
            (root / native.SAVED, CONTAINER_ROOT + '/' + str(native.SAVED), True),
            (root / 'config/server.json', CONTAINER_ROOT + '/config/server.json', False),
            (root / native.POLICY, CONTAINER_ROOT + '/' + str(native.POLICY), False),
            (root / CONFIG, CONTAINER_ROOT + '/' + str(CONFIG), False),
            (root / native.DIRECTORY, CONTAINER_ROOT + '/' + str(native.DIRECTORY), True),
            (root / STATE, CONTAINER_ROOT + '/' + str(STATE), True),
            (root / '.server.lock', CONTAINER_ROOT + '/.server.lock', True)]


def record_fields(inspected):
    if inspected is None:
        return {'Runtime': 'docker', 'ContainerStatus': 'absent'}
    state = inspected['State']
    return {'Runtime': 'docker', 'ContainerId': inspected['Id'], 'ContainerImage': inspected['Image'],
            'ContainerStatus': state['Status'], 'ContainerExitCode': state['ExitCode'],
            'ContainerOOMKilled': state['OOMKilled'], 'ContainerStartedAt': state['StartedAt'],
            'ContainerFinishedAt': state['FinishedAt']}


def exited_as_expected(state):
    return (state.get('ActiveState') == 'failed' and state.get('Result') == 'exit-code'
            and type(state.get('MainPID')) is int and state['MainPID'] == 0
            and type(state.get('ExecMainStatus')) is int and state['ExecMainStatus'] == 134
            and type(state.get('ExecMainCode')) is int and state['ExecMainCode'] == 1 and state.get('ContainerStatus') == 'exited'
            and type(state.get('ContainerExitCode')) is int and state['ContainerExitCode'] == 134
            and state.get('ContainerOOMKilled') is False)


def verify_files(root, entries):
    actual = native.inventory(Path(root) / native.SAVED)
    if set(actual) != set(entries):
        raise native.ShutdownError('Saved inventory changed.')
    for name, value in entries.items():
        if actual[name]['bytes'] != value['bytes'] or native.digest(Path(root) / native.SAVED / name) != value['sha256']:
            raise native.ShutdownError('Saved bytes changed.')


class DockerBackend(SystemdBackend):
    def __init__(self, root):
        self.root = Path(root)
        self.engine = Engine()

    def instance(self):
        config = load(self.root)
        self.engine.trusted()
        obj = self.engine.inspect(config['container_name'])
        if obj is not None:
            validate_instance(self.root, config, obj)
        return obj

    def state(self, unit=GAME_UNIT):
        raw = super().state(unit)
        if unit != GAME_UNIT:
            return raw
        obj = self.instance()
        if obj is not None and obj['State']['Running']:
            if obj['Config']['Labels']['dev.ark.invocation'] != raw.get('InvocationID'):
                raise ControlError('outcome_unknown')
            if raw['ActiveState'] not in ('active', 'activating', 'deactivating'):
                raise ControlError('outcome_unknown')
        observer_ready = False
        if obj is not None and obj['State']['Running']:
            try:
                live, observed = self.shutdown_guard().observation()
                observer_ready = (observed.get('phase') == 'running' and live.get('invocation_id') == raw['InvocationID'])
            except (OSError, ValueError, KeyError):
                pass
        return raw | record_fields(obj) | {'ObserverReady':observer_ready}

    def metrics(self):
        value = super().metrics()
        value['service_memory'], value['cpu_usage_ns'] = None, None
        obj = self.instance()
        if obj is not None and obj['State']['Running']:
            data = json.loads(self.engine.call('stats', '--no-stream', '--format', '{{json .}}', obj['Id'], timeout=15))
            match = re.fullmatch(r'([0-9.]+)([kMGT]?i?B)', data['MemUsage'].split('/')[0].strip())
            if match:
                unit = match[2]
                powers = {'B':1, 'kB':1000, 'MB':1000**2, 'GB':1000**3,
                          'KiB':1024, 'MiB':1024**2, 'GiB':1024**3, 'TiB':1024**4}
                if unit in powers:
                    value['service_memory'] = int(float(match[1]) * powers[unit])
        return value

    def shutdown_guard(self):
        return DockerShutdownGuard(self.root, self)

    def operate(self, action):
        if action == 'start':
            state = self.state()
            self.shutdown_guard().verify_startable(state)
            entries = native.inventory(self.root / native.SAVED)
            files = {name: item | {'sha256': native.digest(self.root / native.SAVED / name)} for name, item in entries.items()}
            native.save(self.root / STATE / 'start-permit.json', {'schema':1, 'phase':'issued', 'nonce':uuid.uuid4().hex,
                'boot_id':native.boot_id(), 'runtime':load(self.root), 'configuration':native.configuration(self.root), 'files':files})
        elif action != 'stop':
            raise ControlError('invalid_request')  # No raw restart/backup/update in this transport.
        super().operate(action)


class DockerShutdownGuard:
    def __init__(self, root, backend=None):
        self.root = Path(root)
        self.backend = backend or DockerBackend(root)
        self.directory = self.root / native.DIRECTORY

    def observation(self):
        live = native.private_json(self.directory / 'live.json')
        if not re.fullmatch('[0-9a-f]{32}', live.get('run_id', '')):
            raise native.ShutdownError('Invalid Docker observation.')
        fault = self.root / STATE / 'lease-fault.json'
        if fault.exists() and native.private_json(fault).get('run_id') == live['run_id']:
            raise native.ShutdownError('Host runner lease was lost.')
        return live, native.private_json(self.directory / 'runtime' / (live['run_id'] + '.json'))

    def verify_stopped(self, state):
        config = load(self.root)
        value = native.private_json(self.directory / 'last-stop.json')
        if (type(value.get('schema')) is not int or value['schema'] != 2 or value.get('runtime') != config or value.get('service_record') != state
                or value.get('clean_exit') is not False or value.get('warning') != 'expected_native_abort'
                or not exited_as_expected(state) or value.get('configuration') != native.configuration(self.root)
                or value.get('native_policy') != native.policy(self.root)):
            raise native.ShutdownError('Docker shutdown receipt does not match.')
        snapshot = value.get('snapshot', '')
        if not re.fullmatch('backups/docker-shutdown-[0-9a-f]{32}', snapshot):
            raise native.ShutdownError('Invalid Docker preservation path.')
        folder = self.root / snapshot
        if native.private_json(folder / 'receipt.json') != value:
            raise native.ShutdownError('Docker receipt mismatch.')
        live, observed = self.observation()
        if (value['observation'] != observed or value['intent']['run_id'] != live['run_id']
                or value['intent']['invocation_id'] != state['InvocationID']
                or live.get('container_deployment') != config['deployment_id']):
            raise native.ShutdownError('Docker observer instance changed.')
        before = native.private_json(folder / 'before/manifest.json', 8*1024*1024)['files']
        after = native.private_json(folder / 'after/manifest.json', 8*1024*1024)['files']
        if not native.observed_proof(observed, value['intent'], value['world'], before, after):
            raise native.ShutdownError('Native save/exit proof incomplete.')
        verify_files(self.root, after)
        return {'clean_exit':False, 'warning':'expected_native_abort', 'snapshot':snapshot,
                'save_completion_observed':True, 'in_game_content_verified':False}

    def verify_startable(self, state):
        current_policy = native.policy(self.root)
        if current_policy is None or type(state.get('MainPID')) is not int or state['MainPID'] != 0 or state.get('ActiveState') not in ('inactive','failed'):
            raise native.ShutdownError('A pinned policy and stopped service are required.')
        obj = self.backend.instance()
        if obj is None:
            # Only a separately preserved/approved cutover permits a FIRST launch.
            # A consumed permit/ledger is never erased to re-use this permission.
            if (self.root / STATE / 'launch.json').exists():
                raise native.ShutdownError('A missing previous container has unknown outcome.')
            bootstrap = native.private_json(self.root / STATE / 'bootstrap.json')
            if (type(bootstrap.get('schema')) is not int or bootstrap['schema'] != 1
                    or bootstrap.get('clean_exit') is not False or bootstrap.get('purpose') != 'explicit_preserved_runtime_cutover'
                    or bootstrap.get('runtime') != load(self.root)):
                raise native.ShutdownError('Cutover review changed.')
            native_live = native.private_json(self.directory/'live.json') if (self.directory/'live.json').exists() else None
            if (bootstrap['configuration'] != native.configuration(self.root) or bootstrap.get('native_policy') != current_policy
                    or bootstrap.get('native_live') != native_live):
                raise native.ShutdownError('Cutover configuration or native instance changed.')
            verify_files(self.root, bootstrap['files'])
            return {'clean_exit':False, 'warning':'reviewed_runtime_cutover', 'in_game_content_verified':False}
        if obj['State']['Running'] or obj['State']['Status'] != 'exited':
            raise native.ShutdownError('Game is not safely stopped.')
        # Unit factory state after reboot cannot replace the historical proof.
        receipt = native.private_json(self.directory / 'last-stop.json')
        record = receipt['service_record']
        if record_fields(obj) != {k: record[k] for k in record_fields(obj)}:
            raise native.ShutdownError('Stopped container does not match previous receipt.')
        return self.verify_stopped(record)

    def stop(self, backend, recheck=None):
        config = load(self.root)
        native.policy(self.root) or (_ for _ in ()).throw(native.ShutdownError('Missing native policy.'))
        state = backend.state()
        obj = backend.instance()
        live, observed = self.observation()
        if (obj is None or not obj['State']['Running'] or state.get('ActiveState') != 'active'
                or state.get('Result') != 'success' or live.get('boot_id') != native.boot_id()
                or live.get('invocation_id') != state.get('InvocationID')
                or live.get('container_deployment') != config['deployment_id']
                or observed.get('phase') != 'running' or observed.get('run_id') != live['run_id']):
            raise native.ShutdownError('Docker observer does not match current service.')
        checked = json.loads(backend.engine.call('exec', obj['Id'], '/usr/bin/python3', '-m',
                            'scripts.ark_container_supervisor', 'check', timeout=15))
        if checked != {'ok':True, 'run_id':live['run_id'], 'pid':observed['pid']}:
            raise native.ShutdownError('Container PID/tracer verification failed.')
        previous = self.directory / 'intent.json'
        if previous.exists() and native.private_json(previous).get('run_id') == live['run_id']:
            raise native.ShutdownError('Stop intent already exists; do not replay.')
        entries = native.inventory(self.root / native.SAVED)
        if shutil.disk_usage(self.root).free < sum(v['bytes'] for v in entries.values()) * 3 + 1024**3:
            raise native.ShutdownError('Insufficient preservation space.')
        nonce = uuid.uuid4().hex
        folder = self.root / 'backups' / ('docker-shutdown-' + nonce)
        before = native.capture(self.root / native.SAVED, folder / 'before')
        protected = native.configuration(self.root)
        ark_config.launch_url(self.root)
        if recheck is not None:
            reason = recheck()
            if reason is not None:
                return {'changed':False, 'reason':reason}
        if backend.state() != state:
            raise native.ShutdownError('Container changed during preservation.')
        intent = {'run_id':live['run_id'], 'nonce':nonce, 'game_pid':observed['pid'],
                  'supervisor_pid':live['supervisor_pid'], 'invocation_id':state['InvocationID'],
                  'boot_id':native.boot_id(), 'monotonic':time.monotonic(), 'container_id':obj['Id']}
        native.save(folder / 'intent.json', intent)
        native.save(self.directory / 'intent.json', intent)
        backend.operate('stop')
        stopped = backend.state()
        if stopped.get('MainPID') != 0 or stopped.get('ContainerStatus') != 'exited':
            raise native.ShutdownError('Container termination is unknown.')
        with ark_config.server_lock(self.root):
            after = native.capture(self.root / native.SAVED, folder / 'after')
            live, observed = self.observation()
            world = 'SavedArks/' + ark_config.load_settings(self.root)['map'] + '.ark'
            if (not exited_as_expected(stopped) or stopped.get('InvocationID') != state['InvocationID']
                    or stopped.get('ContainerId') != state['ContainerId'] or protected != native.configuration(self.root)
                    or not native.observed_proof(observed, intent, world, before, after)):
                raise native.ShutdownError('Unexpected Docker exit or incomplete save proof.')
            value = {'schema':2, 'warning':'expected_native_abort', 'clean_exit':False, 'runtime':config,
                'native_policy':native.policy(self.root), 'boot_id':native.boot_id(), 'service_record':stopped,
                'snapshot':str(folder.relative_to(self.root)), 'configuration':protected, 'world':world,
                'observation':observed, 'intent':intent, 'in_game_content_verified':False}
            native.save(folder / 'receipt.json', value)
            native.save(self.directory / 'last-stop.json', value)
        return {'changed':True, 'service':stopped, 'readiness_verified':False,
                'shutdown':self.verify_stopped(stopped), 'warnings':['expected_native_abort']}


def create_args(root, config, invocation):
    settings = ark_config.load_settings(root)
    args = ['create', '--name', config['container_name'], '--init', '--restart=no', '--read-only', '--network=bridge',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--stop-signal=SIGINT', '--stop-timeout=150',
            '--log-driver=local', '--log-opt=max-size=10m', '--log-opt=max-file=3',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=512m', '--tmpfs=/run/ark:rw,nosuid,nodev,size=128m',
            '--shm-size=256m', '--ulimit=core=0', '--pids-limit=512',
            '--label=dev.ark.deployment=' + config['deployment_id'],
            '--label=dev.ark.invocation=' + invocation,
            '--label=dev.ark.contract=' + contract_id(config['source_contract']),
            '--env=INVOCATION_ID=' + invocation]
    for port in (settings['game_port'], settings['game_port'] + 1, settings['query_port']):
        args += ['--publish', f'{config["bind_address"]}:{port}:{port}/udp']
    for source, target, writable in mounts(root):
        if ',' in str(source) or source.is_symlink():
            raise native.ShutdownError('Unsafe mount source.')
        args += ['--mount', f'type=bind,src={source},dst={target}' + ('' if writable else ',readonly')]
    return args + [config['image']]


def install_descriptor(root, image, name, bind_address='0.0.0.0'):
    root = Path(root)
    engine = Engine(); engine.trusted()
    contract = source_contract()
    inspected = json.loads(engine.call('image', 'inspect', image))[0]
    if inspected['Config'].get('Labels', {}).get('dev.ark.source-contract') != contract_id(contract):
        raise native.ShutdownError('Image was not built from the current reviewed sources.')
    value = {'schema':1, 'deployment_id':uuid.uuid4().hex, 'container_name':name,
             'image':inspected['Id'], 'source_contract':contract, 'bind_address':bind_address}
    native.save(root / CONFIG, value)
    return value


def approve_cutover(root, runtime, receipt):
    """Called locally only after independent Legacy/Docker full stopped verification."""
    root = Path(root)
    approved_policy = native.policy(root)
    if approved_policy is None:
        raise native.ShutdownError('A pinned native profile is required before cutover approval.')
    entries = native.inventory(root / native.SAVED)
    files = {name:item | {'sha256':native.digest(root / native.SAVED / name)} for name,item in entries.items()}
    live_path = root / native.DIRECTORY / 'live.json'
    native.save(root / STATE / 'bootstrap.json', {'schema':1, 'runtime':runtime,
        'native_policy':approved_policy,'native_live':native.private_json(live_path) if live_path.exists() else None,
        'configuration':native.configuration(root), 'files':files, 'reviewed_stop_receipt':receipt,
        'clean_exit':False, 'purpose':'explicit_preserved_runtime_cutover'})
