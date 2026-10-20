"""Evidence-qualified stopping. Never changes systemd's failed/SIGABRT result.

Only the pinned, experimentally inspected Linux build is supported. A native
save-completion observation is NOT a proof of every piece of in-game content.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid

from . import ark_config

BINARY = Path('server/ShooterGame/Binaries/Linux/ShooterGameServer')
SAVED = Path('server/ShooterGame/Saved')
POLICY = Path('config/shutdown-policy.json')
DIRECTORY = Path('logs/shutdown')
PROFILE = 'ase-linux-361.7-21241282'
BINARY_SHA256 = '7e7ded49e0c658e74199801d79630bd33da407d3e468e039837bf61a9de7c520'
SAVE_BEGIN = 0xe64909
SAVE_COMPLETE = 0xe65a49
FORCE_ABORT = 0x1b03aa2
EXIT_CALLER = 0x8202a9
CODE_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_FILES = ('ark_shutdown.py', 'ark_supervisor.py', 'ark_game_exec.py', 'shutdown_observer.py', 'shutdown_trace.gdb')


class ShutdownError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ShutdownError('A regular file is required.')
        for data in iter(lambda: f.read(2 * 1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def private_json(path, limit=131072):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise ShutdownError('Private evidence is unavailable or invalid.')
        raw = f.read(limit + 1)
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ShutdownError('Duplicate evidence field.')
            result[k] = v
        return result
    result = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ShutdownError('Evidence must be an object.')
    return result


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    ark_config.atomic_write(path, (json.dumps(value, separators=(',', ':')) + '\n').encode())


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def contract():
    return {name: digest(CODE_ROOT / 'scripts' / name) for name in OBSERVER_FILES}


def install_policy(root):
    root = Path(root)
    if digest(root / BINARY) != BINARY_SHA256:
        raise ShutdownError('Unreviewed server build; qualification is disabled.')
    value = {'schema': 1, 'profile': PROFILE, 'binary_sha256': BINARY_SHA256,
             'observer_contract': contract(), 'enabled': True}
    save(root / POLICY, value)
    return value


def policy(root):
    path = Path(root) / POLICY
    if not path.exists() and not path.is_symlink():
        return None
    value = private_json(path)
    if (set(value) != {'schema', 'profile', 'binary_sha256', 'observer_contract', 'enabled'}
            or type(value['schema']) is not int or value['schema'] != 1 or value['enabled'] is not True
            or value['profile'] != PROFILE or value['binary_sha256'] != BINARY_SHA256
            or value['observer_contract'] != contract() or digest(Path(root) / BINARY) != BINARY_SHA256):
        raise ShutdownError('Shutdown evidence contract changed; revalidation is required.')
    return value


def configuration(root):
    return {str(path): digest(Path(root) / path) for path in
            (ark_config.SETTINGS_PATH, ark_config.INI_PATH, ark_config.GAME_INI_PATH)
            if (Path(root) / path).exists()}


def inventory(root):
    """Reject symlinks and special files; retain names privately, never in replies."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ShutdownError('Saved data is unavailable.')
    result = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(parent) / name
            mode = p.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ShutdownError('Unsafe Saved entry.')
        for name in files:
            p = Path(parent) / name
            s = p.stat()
            result[str(p.relative_to(root))] = {'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns}
    if len(result) > 20000:
        raise ShutdownError('Saved inventory exceeds limit.')
    return result


def capture(source, destination):
    before = inventory(source)
    destination.mkdir(mode=0o700, parents=True)
    def copy(src, dst):
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise ShutdownError('Saved source is not a regular file.')
            out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, 'wb') as output:
                shutil.copyfileobj(incoming, output, 2*1024*1024)
                output.flush()
                os.fsync(output.fileno())
    shutil.copytree(source, destination / 'Saved', copy_function=copy, symlinks=True)
    inventory(destination / 'Saved')
    after = inventory(source)
    if before != after:
        raise ShutdownError('Saved changed during capture.')
    entries = {}
    for name, details in after.items():
        copied, current = digest(destination / 'Saved' / name), digest(source / name)
        if copied != current:
            raise ShutdownError('Saved changed during capture.')
        entries[name] = details | {'sha256': current}
    for folder, dirs, files in os.walk(destination):
        Path(folder).chmod(0o700)
    save(destination / 'manifest.json', {'files': entries, 'consistent_game_content_proven': False})
    return entries


def process_matches(pid, root, tracer_pid):
    if type(pid) is not int or pid <= 0 or type(tracer_pid) is not int or tracer_pid <= 0:
        return False
    try:
        status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
        return (Path(f'/proc/{pid}/exe').resolve() == Path(root) / BINARY
                and int(status['TracerPid'].strip()) == tracer_pid
                and Path(f'/proc/{pid}').stat().st_uid == os.getuid())
    except (OSError, ValueError, KeyError):
        return False


def observed_proof(observation, intent, world, before, after):
    """Pure, fail-closed predicate; no process effects or exception text exposed."""
    try:
        begin, complete, abort = (observation[k] for k in ('save_begin', 'save_complete', 'abort'))
        events = [intent['monotonic'], begin['monotonic'], complete['monotonic'], abort['monotonic'], observation['exited_monotonic']]
        import math
        if not all(type(x) in (int, float) and math.isfinite(x) and x >= 0 for x in events):
            return False
        if events != sorted(events) or events[-1] - events[0] > 180:
            return False
        if observation.get('observation_fault'):
            return False
        if any(type(point.get(k)) is not int or point[k] <= 0 for point in (begin, complete) for k in ('thread', 'game_mode')):
            return False
        if not all(isinstance(intent.get(k), str) and re.fullmatch('[0-9a-f]{32}', intent[k]) for k in ('run_id', 'nonce', 'invocation_id')):
            return False
        if type(intent.get('game_pid')) is not int or intent['game_pid'] <= 0:
            return False
        if not (observation['phase'] == 'exited' and type(observation['exit_signal']) is int
                and observation['exit_signal'] == 6 and observation['exit_code'] is None
                and observation['run_id'] == intent['run_id'] and observation['pid'] == intent['game_pid']
                and observation['invocation_id'] == intent['invocation_id']):
            return False
        if not all(point['nonce'] == intent['nonce'] for point in (begin, complete, abort)):
            return False
        if begin['thread'] != complete['thread'] or begin['game_mode'] != complete['game_mode']:
            return False
        if type(abort['pc']) is not int or abort['pc'] != FORCE_ABORT or type(abort['caller']) is not int or abort['caller'] != EXIT_CALLER:
            return False
        item = after[world]
        old = before.get(world)
        if item['bytes'] < 4096 or item['bytes'] != complete['world_bytes'] or item['mtime_ns'] != complete['world_mtime_ns']:
            return False
        if old is not None and item['mtime_ns'] == old['mtime_ns']:
            return False
        profiles = lambda entries: {k for k in entries if k.endswith(('.arkprofile', '.arktribe'))}
        if not profiles(before) <= profiles(after) or any(after[k]['bytes'] == 0 for k in profiles(after)):
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


class ShutdownGuard:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / DIRECTORY

    def status_receipt(self, state):
        """Metadata only for status; mutations additionally hash every Saved file."""
        value = private_json(self.directory / 'last-stop.json')
        if (value.get('schema') != 1 or value.get('warning') != 'expected_native_abort'
                or value.get('clean_exit') is not False or value.get('boot_id') != boot_id()
                or value.get('service_record') != state):
            raise ShutdownError('No matching qualified stopped record.')
        return value

    def verify_stopped(self, state):
        approved = policy(self.root)
        if not approved:
            raise ShutdownError('No qualified shutdown policy.')
        value = self.status_receipt(state)
        if value.get('policy') != approved:
            raise ShutdownError('Shutdown proof contract changed.')
        if (state.get('ActiveState') != 'failed' or state.get('Result') != 'core-dump'
                or type(state.get('MainPID')) is not int or state['MainPID'] != 0
                or type(state.get('ExecMainStatus')) is not int or state['ExecMainStatus'] != 6
                or state.get('ExecMainCode') != 3):
            raise ShutdownError('Not the recognized failed service result.')
        if value.get('configuration') != configuration(self.root):
            raise ShutdownError('Configuration changed after stopping.')
        snapshot = value.get('snapshot')
        if not isinstance(snapshot, str) or re.fullmatch(r'backups/shutdown-[0-9a-f]{32}', snapshot) is None:
            raise ShutdownError('Invalid shutdown snapshot.')
        folder = self.root / snapshot
        receipt = private_json(folder / 'receipt.json')
        if receipt != value:
            raise ShutdownError('Shutdown receipt mismatch.')
        before = private_json(folder / 'before/manifest.json', 8*1024*1024)['files']
        after = private_json(folder / 'after/manifest.json', 8*1024*1024)['files']
        if not observed_proof(value['observation'], value['intent'], value['world'], before, after):
            raise ShutdownError('Shutdown proof is incomplete.')
        actual = inventory(self.root / SAVED)
        if set(actual) != set(after):
            raise ShutdownError('Saved files changed after stopping.')
        for name, entry in after.items():
            if actual[name]['bytes'] != entry['bytes'] or digest(self.root / SAVED / name) != entry['sha256']:
                raise ShutdownError('Saved files changed after stopping.')
        return {'clean_exit': False, 'warning': 'expected_native_abort', 'snapshot': snapshot,
                'save_completion_observed': True, 'in_game_content_verified': False}

    def stop(self, backend, recheck=None):
        policy(self.root) or (_ for _ in ()).throw(ShutdownError('No qualified shutdown policy.'))
        state = backend.state()
        live = private_json(self.directory / 'live.json')
        run_id = live.get('run_id')
        if not isinstance(run_id, str) or re.fullmatch('[0-9a-f]{32}', run_id) is None:
            raise ShutdownError('Invalid observer instance.')
        observation_path = self.directory / 'runtime' / (run_id + '.json')
        observation = private_json(observation_path)
        if (state.get('ActiveState') != 'active' or state.get('Result') != 'success'
                or live.get('supervisor_pid') != state.get('MainPID') or live.get('boot_id') != boot_id()
                or live.get('invocation_id') != state.get('InvocationID')
                or observation.get('run_id') != run_id or observation.get('phase') != 'running'
                or type(observation.get('pid')) is not int or observation['pid'] <= 0
                or not process_matches(observation['pid'], self.root, live.get('debugger_pid'))):
            raise ShutdownError('Observer does not match the running game.')
        if (self.directory / 'intent.json').exists():
            old = private_json(self.directory / 'intent.json')
            if old.get('run_id') == run_id:
                raise ShutdownError('A stop intent already exists; do not replay.')
        entries = inventory(self.root / SAVED)
        needed = sum(item['bytes'] for item in entries.values()) * 3 + 1024**3
        if shutil.disk_usage(self.root).free < needed:
            raise ShutdownError('Insufficient preservation space.')
        nonce = uuid.uuid4().hex
        folder = self.root / 'backups' / ('shutdown-' + nonce)
        before = capture(self.root / SAVED, folder / 'before')
        protected = configuration(self.root)
        ark_config.launch_url(self.root)
        if recheck is not None:
            reason = recheck()
            if reason is not None:
                return {'changed': False, 'reason': reason}
        if backend.state() != state:
            raise ShutdownError('Process changed during preservation.')
        intent = {'run_id': run_id, 'nonce': nonce, 'game_pid': observation['pid'],
                  'supervisor_pid': state['MainPID'], 'invocation_id': state['InvocationID'],
                  'boot_id': boot_id(), 'monotonic': time.monotonic()}
        save(folder / 'intent.json', intent)
        save(self.directory / 'intent.json', intent)
        backend.operate('stop')
        after_state = backend.state()
        # No retries, reset-failed, signal reinterpretation or fallback kill here.
        if type(after_state.get('MainPID')) is not int or after_state['MainPID'] != 0:
            raise ShutdownError('Process exit is unknown.')
        with ark_config.server_lock(self.root):
            after = capture(self.root / SAVED, folder / 'after')
            if (after_state.get('ActiveState') != 'failed' or after_state.get('Result') != 'core-dump'
                    or after_state.get('ExecMainStatus') != 6 or after_state.get('ExecMainCode') != 3
                    or after_state.get('InvocationID') != state.get('InvocationID')):
                raise ShutdownError('Unexpected service exit.')
            observation = private_json(observation_path)
            world = 'SavedArks/' + ark_config.load_settings(self.root)['map'] + '.ark'
            if protected != configuration(self.root) or not observed_proof(observation, intent, world, before, after):
                raise ShutdownError('Save or native exit proof is incomplete.')
            receipt = {'schema': 1, 'warning': 'expected_native_abort', 'clean_exit': False,
                       'boot_id': boot_id(), 'policy': policy(self.root), 'snapshot': str(folder.relative_to(self.root)),
                       'service_record': after_state, 'configuration': protected,
                       'world': world, 'observation': observation, 'intent': intent,
                       'in_game_content_verified': False}
            save(folder / 'receipt.json', receipt)
            save(self.directory / 'last-stop.json', receipt)
        return {'changed': True, 'service': after_state, 'readiness_verified': False,
                'shutdown': self.verify_stopped(after_state), 'warnings': ['expected_native_abort']}

    def review_initial_failure(self, state, preserved_folder, infrastructure_changes=()):
        """Explicit local review, never a Bot command or an automatic failure reset.

        The preserved stop record must independently bind all current Saved and
        configuration bytes. A new invocation or any edit invalidates this grant.
        """
        if (state.get('ActiveState') != 'failed' or state.get('MainPID') != 0
                or state.get('Result') != 'core-dump' or state.get('ExecMainStatus') != 6
                or state.get('ExecMainCode') != 3 or not re.fullmatch('[0-9a-f]{32}', state.get('InvocationID', ''))):
            raise ShutdownError('Review requires the exact stopped failure.')
        preserved_folder = Path(preserved_folder)
        if (preserved_folder.parent != self.root / 'backups' or preserved_folder.is_symlink()
                or not preserved_folder.name.startswith('shutdown-validation-')):
            raise ShutdownError('Invalid independent preservation source.')
        manifest = private_json(preserved_folder / 'after/manifest.json', 8*1024*1024)
        report = private_json(preserved_folder / 'report.json')
        files = {}
        for entry in manifest['files']:
            name = entry['path']
            if not isinstance(name, str) or not name.startswith('Saved/') or '..' in Path(name).parts:
                raise ShutdownError('Invalid preservation path.')
            relative = name[len('Saved/'):]
            if digest(self.root / SAVED / relative) != entry['sha256']:
                raise ShutdownError('Preserved Saved no longer matches.')
            files[relative] = {'bytes': entry['bytes'], 'sha256': entry['sha256']}
        if set(inventory(self.root / SAVED)) != set(files):
            raise ShutdownError('Preserved Saved inventory changed.')
        if report['service_after'].get('InvocationID') != state['InvocationID']:
            raise ShutdownError('Preserved service invocation does not match.')
        allowed = {str(Path(p)) for p in infrastructure_changes}
        if any(Path(p).name != 'ark-genesis.service' for p in allowed):
            raise ShutdownError('Only the separately preserved game unit may be upgraded.')
        if any(digest(Path(path)) != sha for path, sha in report['protected_hashes_before'].items() if path not in allowed):
            raise ShutdownError('Protected configuration changed.')
        operation_path = self.root / 'logs/automation/operation.json'
        operation = private_json(operation_path)
        if operation.get('phase') != 'blocked' or operation.get('code') != 'shutdown_failed':
            raise ShutdownError('Review does not match the blocking operation.')
        # Preserve the original latch before a subsequent accepted operation can replace it.
        folder = self.directory / 'reviews' / uuid.uuid4().hex
        save(folder / 'original-operation.json', operation)
        value = {'schema': 1, 'purpose': 'explicit_initial_start_review_not_clean_shutdown',
                 'boot_id': boot_id(), 'service_record': state, 'configuration': configuration(self.root),
                 'policy': policy(self.root), 'files': files, 'operation_sha256': digest(operation_path),
                 'reviewed_infrastructure': {path: digest(Path(path)) for path in allowed},
                 'preservation': str(preserved_folder.relative_to(self.root)),
                 'original_operation_copy': str((folder / 'original-operation.json').relative_to(self.root)),
                 'reviewed_at': time.time()}
        if not value['policy']:
            raise ShutdownError('No approved observation policy.')
        save(folder / 'review.json', value)
        save(self.directory / 'initial-review.json', value)
        return {'reviewed': True, 'clean_exit': False}

    def verify_initial_review(self, state, check_operation=False):
        value = private_json(self.directory / 'initial-review.json', 8*1024*1024)
        if (value.get('schema') != 1 or value.get('purpose') != 'explicit_initial_start_review_not_clean_shutdown'
                or value.get('boot_id') != boot_id() or value.get('service_record') != state
                or value.get('configuration') != configuration(self.root) or value.get('policy') != policy(self.root)
                or not value.get('policy')):
            raise ShutdownError('Initial review no longer matches.')
        if any(digest(Path(path)) != sha for path, sha in value.get('reviewed_infrastructure', {}).items()):
            raise ShutdownError('Reviewed service infrastructure changed.')
        if check_operation and digest(self.root / 'logs/automation/operation.json') != value.get('operation_sha256'):
            raise ShutdownError('Blocking operation changed.')
        actual = inventory(self.root / SAVED)
        if set(actual) != set(value['files']):
            raise ShutdownError('Saved inventory changed after review.')
        for name, entry in value['files'].items():
            if actual[name]['bytes'] != entry['bytes'] or digest(self.root / SAVED / name) != entry['sha256']:
                raise ShutdownError('Saved changed after review.')
        return {'clean_exit': False, 'warning': 'reviewed_previous_failure',
                'snapshot': value['preservation'], 'in_game_content_verified': False}
