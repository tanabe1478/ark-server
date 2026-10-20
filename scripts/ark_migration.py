"""Verified Aquatica package import. No archive scripts, downloads or automatic start.

Only an already extracted package is accepted. Preview is read-only; apply is called
by the confirmed controller under its control/backup locks and our server lock.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid

if __package__:
    from . import ark_config as config
    from . import ark_recovery as recovery
else:
    import ark_config as config
    import ark_recovery as recovery

PENDING = Path('config/migration.pending.json')
SAVED = Path('server/ShooterGame/Saved')
MODS = Path('server/ShooterGame/Content/Mods')
MAP = Path('server/ShooterGame/Content/Maps/Abyss/Aquatica.umap')
SNAPSHOT_ID = r'migration-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}'


class MigrationInvalidError(config.ConfigError):
    pass


class MigrationLimitError(config.ConfigError):
    pass


class Budget:
    def __init__(self, seconds=8, byte_limit=32 * 1024**3, entry_limit=50000):
        self.deadline = time.monotonic() + seconds
        self.remaining, self.entries = byte_limit, entry_limit

    def check(self, size=0, entry=False):
        self.remaining -= size
        self.entries -= int(entry)
        if time.monotonic() > self.deadline or self.remaining < 0 or self.entries < 0:
            raise MigrationLimitError('Migration inspection exceeded its time, byte or entry limit.')


def validate_source(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or not value.isprintable() or not Path(value).is_absolute() or '..' in Path(value).parts:
        raise MigrationInvalidError('Select an absolute extracted-package directory.')
    return Path(value)


def safe_path(path):
    # Check intermediate components too, not just the last file's O_NOFOLLOW flag.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise MigrationInvalidError('Symbolic-link components are not supported.')
    return path


def relative(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or not value.isprintable() or '\\' in value:
        raise MigrationInvalidError('Unsafe package path.')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value:
        raise MigrationInvalidError('Unsafe package path.')
    return Path(value)


def unique_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MigrationInvalidError('Duplicate manifest keys.')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, RecursionError, UnicodeError):
        raise MigrationInvalidError('Invalid migration manifest.') from None


def file_hash(path, budget, destination=None):
    budget.check(entry=True)
    fd = os.open(safe_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise MigrationInvalidError('Only regular package/data files are supported.')
        digest = hashlib.sha256()
        output = None
        try:
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output = destination.open('xb')
                os.fchmod(output.fileno(), 0o600)
            while chunk := source.read(1024 * 1024):
                budget.check(len(chunk))
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
            fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if fingerprint(before) != fingerprint(os.fstat(source.fileno())) or fingerprint(before) != fingerprint(path.stat(follow_symlinks=False)):
                raise MigrationInvalidError('A migration file changed while it was read.')
            budget.check()
            return digest.hexdigest(), before.st_size
        finally:
            if output is not None:
                output.close()


def walk(path, budget, depth=0):
    if depth > 64:
        raise MigrationLimitError('Migration directory depth exceeds the limit.')
    # Bound enumeration itself, before sorting/recursing; do not materialize an
    # untrusted rglob tree and only check its size after allocating it.
    children = []
    with os.scandir(path) as entries:
        for entry in entries:
            budget.check(entry=True)
            item = Path(entry.path)
            if entry.is_symlink():
                raise MigrationInvalidError('Symbolic links are not supported.')
            children.append((item, entry.is_dir(follow_symlinks=False)))
    for item, directory in sorted(children):
        yield item
        if directory:
            yield from walk(item, budget, depth + 1)


def inventory(path, budget):
    safe_path(path)
    if not path.exists():
        return None
    if path.is_file():
        digest, size = file_hash(path, budget)
        return {'': [digest, size]}
    if not path.is_dir():
        raise MigrationInvalidError('Only regular files and directories are supported.')
    rows = {'.': ['directory', 0]}
    for item in walk(path, budget):
        safe_path(item)
        name = item.relative_to(path).as_posix()
        relative(name)
        if item.is_dir():
            rows[name] = ['directory', 0]
        else:
            digest, size = file_hash(item, budget)
            rows[name] = [digest, size]
    return rows


def target_state(root, units, budget):
    return {unit.as_posix(): inventory(root / unit, budget) for unit in units}


def validate_snapshot(value):
    if not isinstance(value, str) or re.fullmatch(SNAPSHOT_ID, value) is None:
        raise MigrationInvalidError('Select an installed private migration snapshot by name.')
    return value


def recovery_credentials(root, snapshot, budget):
    """Read only a chosen installed snapshot, never infer or rotate credentials."""
    folder = safe_path(root / 'backups' / validate_snapshot(snapshot))
    paths = [folder / 'manifest.json', folder / 'previous' / config.INI_PATH,
             folder / 'previous' / config.SETTINGS_PATH, folder / 'previous' / config.GAME_INI_PATH]
    data = []
    for path in paths:
        safe_path(path)
        raw = recovery._read(path) if path.exists() else None
        budget.check(len(raw) if raw is not None else 0, entry=True)
        data.append(raw)
    manifest, ini, runtime, game = data
    if manifest is None or ini is None or runtime is None:
        raise MigrationInvalidError('The selected migration snapshot is incomplete.')
    record = unique_json(manifest)
    if not isinstance(record, dict) or record.get('status') != 'installed' or record.get('automatic_restart') is not False or not isinstance(record.get('units'), dict) or any(record['units'].get(str(unit)) is not True for unit in (SAVED, config.SETTINGS_PATH)):
        raise MigrationInvalidError('Only a completed migration with retired Saved and settings can supply credentials.')
    config.editable_from_bytes(ini, runtime, game)  # Includes private credential validation.
    parsed = config.parse_ini(config.decode_ini(ini)[0])
    signature = hashlib.sha256()
    for raw in data:
        signature.update(b'missing\x00' if raw is None else str(len(raw)).encode() + b'\x00' + raw)
    return parsed, signature.hexdigest()


def inspect(root, package_path, *, budget=None, recovery_snapshot=None, service_record=None):
    budget = Budget() if budget is None else budget
    root, package = Path(root).absolute(), safe_path(validate_source(package_path))
    safe_path(root)
    if package == root or root in package.parents or package in root.parents:
        raise MigrationInvalidError('The package must be separate from the running installation.')
    if (root / PENDING).exists() or (root / PENDING).is_symlink():
        raise config.ConfigRecoveryError('An incomplete migration requires manual recovery.')
    manifest_data = recovery._read(safe_path(package / 'manifest.json'), 4 * 1024 * 1024)
    manifest = unique_json(manifest_data)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('package_sha256'), dict) or not isinstance(manifest.get('mods'), list):
        raise MigrationInvalidError('Unsupported migration package manifest.')
    hashes = manifest['package_sha256']
    if len(hashes) > 25000:
        raise MigrationLimitError('Too many migration manifest entries.')
    for name, digest in hashes.items():
        relative(name)
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise MigrationInvalidError('Invalid package checksum.')
    try:
        ids = [item['id'] for item in manifest['mods']]
    except (TypeError, KeyError):
        raise MigrationInvalidError('Invalid package MOD inventory.') from None
    if not 1 <= len(ids) <= 32 or any(not isinstance(item, str) or not re.fullmatch(config.MOD_ID, item) or int(item) > 2**64 - 1 for item in ids) or len(set(ids)) != len(ids):
        raise MigrationInvalidError('Invalid package MOD inventory.')
    units = [SAVED, config.SETTINGS_PATH, *[unit for item in ids for unit in (MODS / item, MODS / (item + '.mod'))]]
    sources, file_count, byte_count = {}, 0, 0
    for unit in units:
        if unit == config.SETTINGS_PATH:
            continue
        rows = inventory(package / unit, budget)
        if not rows:
            raise MigrationInvalidError('A required migration unit is missing.')
        sources[unit.as_posix()] = rows
        for name, (digest, size) in rows.items():
            if digest == 'directory':
                continue
            key = (unit / name).as_posix() if name else unit.as_posix()
            if hashes.get(key) != digest:
                raise MigrationInvalidError('An import file is missing from the manifest or has changed.')
            file_count += 1
            byte_count += size
    # Manifest entries for the selected units cannot silently disappear from the tree.
    actual = {((Path(unit) / name).as_posix() if name else unit)
              for unit, rows in sources.items() for name, row in rows.items() if row[0] != 'directory'}
    expected = {name for name in hashes if any(name == unit.as_posix() or name.startswith(unit.as_posix() + '/') for unit in units if unit != config.SETTINGS_PATH)}
    if actual != expected:
        raise MigrationInvalidError('The migration file inventory is incomplete.')
    save_rows = sources[SAVED.as_posix()]
    if save_rows.get('SavedArks/Aquatica.ark', ['', 0])[1] <= 0:
        raise MigrationInvalidError('Aquatica world data is missing.')
    characters = sum(bool(re.fullmatch(r'SavedArks/[0-9]{17}\.arkprofile', name)) and row[1] > 0 for name, row in save_rows.items())
    tribes = sum(name.startswith('SavedArks/') and name.endswith('.arktribe') and row[1] > 0 for name, row in save_rows.items())
    if not characters or not tribes or manifest.get('character_count') != characters:
        raise MigrationInvalidError('The character or tribe inventory is incomplete.')
    allowlist = recovery._read(safe_path(package / SAVED / 'AllowedCheaterSteamIDs.txt'), 4096).decode('utf-8-sig').splitlines()
    if not allowlist or len(allowlist) > 64 or any(not re.fullmatch(r'[0-9]{17}', item) for item in allowlist):
        raise MigrationInvalidError('The source administrator allowlist is missing or invalid.')
    for item in ids:
        if sources[(MODS / (item + '.mod')).as_posix()].get('', ['', 0])[1] <= 0 or len(sources[(MODS / item).as_posix()]) <= 1:
            raise MigrationInvalidError('MOD content or metadata is empty.')
    if not safe_path(root / MAP).is_file() or (root / MAP).stat().st_size == 0 or not safe_path(root / 'server/ShooterGame/Binaries/Linux/ShooterGameServer').is_file():
        raise MigrationInvalidError('Install the Aquatica Linux server and map first.')
    current = target_state(root, units, budget)
    credential_signature = None
    if recovery_snapshot is None:
        before = config.load_editable_settings(root)
        old_text, _ = config.decode_ini(recovery._read(safe_path(root / config.INI_PATH)))
        old = config.parse_ini(old_text)
    else:
        if not isinstance(service_record, dict):
            raise MigrationInvalidError('Recovery requires a verified stopped-service record.')
        old, credential_signature = recovery_credentials(root, recovery_snapshot, budget)
        # Invalid current credentials must not become silently trusted. Report
        # non-secret runtime fields and explicitly unknown INI-derived values.
        before = {key: '未取得' for key in (*config.DEFAULTS, *config.RULES, 'active_mods')}
        before.update(config.load_settings(root))
    source_ini = recovery._read(safe_path(package / config.INI_PATH))
    if hashlib.sha256(source_ini).hexdigest() != hashes[config.INI_PATH.as_posix()]:
        raise MigrationInvalidError('The source INI changed during inspection.')
    source_text, _ = config.decode_ini(source_ini)
    parsed = config.parse_ini(source_text)
    runtime = {**config.load_settings(root), 'map': 'Aquatica',
               'session_name': config._ini_value(parsed, 'SessionSettings', 'SessionName', before['session_name'])}
    try:
        runtime['max_players'] = int(config._ini_value(parsed, '/Script/Engine.GameSession', 'MaxPlayers', str(before['max_players'])))
    except ValueError:
        raise MigrationInvalidError('Invalid maximum player count.') from None
    config.validate_settings(runtime)
    # A server-only package can omit this engine-owned section. Without its
    # schema version, Unreal may reset the entire INI during initialization,
    # dropping credentials and ActiveMods even though our preflight passed.
    updated_text = config.merge_ini(source_text, {
        'ServerSettings': {
            'ServerPassword': config._ini_value(old, 'ServerSettings', 'ServerPassword', ''),
            'ServerAdminPassword': config._ini_value(old, 'ServerSettings', 'ServerAdminPassword', ''),
            'RCONEnabled': 'False',
        },
        config.GAME_USER_SETTINGS_SECTION: {'Version': config.GAME_USER_SETTINGS_VERSION},
    })
    config.validate_ini_initialization(updated_text)
    updated_ini = updated_text.encode('utf-8')
    runtime_bytes = (json.dumps(runtime, ensure_ascii=False, indent=2) + '\n').encode()
    game = recovery._read(safe_path(package / config.GAME_INI_PATH))
    if hashlib.sha256(game).hexdigest() != hashes[config.GAME_INI_PATH.as_posix()]:
        raise MigrationInvalidError('Game.ini changed during inspection.')
    after = config.editable_from_bytes(updated_ini, runtime_bytes, game)
    if after['active_mods'] != ids:
        raise MigrationInvalidError('The package MOD order does not match ActiveMods.')
    signature = {'package': str(package), 'manifest': hashlib.sha256(manifest_data).hexdigest(),
                 'sources': sources, 'target': current,
                 'ini': hashlib.sha256(updated_ini).hexdigest(), 'runtime': hashlib.sha256(runtime_bytes).hexdigest()}
    if recovery_snapshot is not None:
        signature['recovery'] = {'snapshot': recovery_snapshot, 'credential_revision': credential_signature, 'service': service_record}
    revision = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    plan = {'revision': revision, 'changed': True, 'settings': after,
            'changes': {key: {'before': before[key], 'after': after[key]} for key in sorted(after) if before[key] != after[key]},
            'files': [unit.as_posix() for unit in units], 'requires_stopped_server': True, 'automatic_restart': False,
            'migration': {'package_path': str(package), 'characters': characters, 'tribes': tribes,
                          'file_count': file_count, 'bytes': byte_count, 'credentials_preserved': True,
                          'scripts_executed': False, 'linux_mod_compatibility_verified': False}}
    if recovery_snapshot is not None:
        world_name = 'SavedArks/Aquatica.ark'
        previous_world = (current[SAVED.as_posix()] or {}).get(world_name)
        plan['restores_secrets'] = True
        plan['migration']['credentials_preserved'] = False
        plan['recovery'] = {'snapshot': recovery_snapshot, 'credentials_restored': True,
                            'current_saved_retired': True, 'service_before': service_record,
                            'clears_service_failure': False, 'game_user_settings_version': int(config.GAME_USER_SETTINGS_VERSION),
                            'world_before': {'sha256': previous_world[0], 'bytes': previous_world[1]} if previous_world else None,
                            'world_after': {'sha256': save_rows[world_name][0], 'bytes': save_rows[world_name][1]}}
    return plan, units, sources, current, updated_ini, runtime_bytes


def plan_migration(root, package_path):
    """Return only the safe summary. Credentials/INI content never leave this module."""
    return inspect(root, package_path)[0]


def plan_recovery(root, package_path, snapshot, service_record):
    return inspect(root, package_path, recovery_snapshot=snapshot, service_record=service_record)[0]


def apply_migration(root, package_path, expected_revision, verify_stopped, *, recovery_snapshot=None, service_record=None):
    root = Path(root)
    with config.server_lock(root):
        budget = Budget(seconds=300, byte_limit=128 * 1024**3, entry_limit=250000)
        options = {'recovery_snapshot': recovery_snapshot, 'service_record': service_record}
        plan, units, sources, current, ini, runtime = inspect(root, package_path, budget=budget, **options)
        if plan['revision'] != expected_revision:
            raise config.StaleConfigError('Migration source or destination changed after preview.')
        verify_stopped()
        if shutil.disk_usage(root).free < plan['migration']['bytes'] + 256 * 1024**2:
            raise MigrationLimitError('Insufficient space to stage the migration safely.')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        snapshot = root / 'backups' / f'migration-{stamp}-{uuid.uuid4().hex}'
        safe_path(snapshot)
        snapshot.mkdir(parents=True, mode=0o700)
        incoming, previous = snapshot / 'incoming', snapshot / 'previous'
        incoming.mkdir(mode=0o700)
        previous.mkdir(mode=0o700)
        for unit in units:
            destination = incoming / unit
            if unit == config.SETTINGS_PATH:
                config.atomic_write(destination, runtime)
                continue
            for name, (digest, _) in sources[unit.as_posix()].items():
                target = destination / name if name not in ('', '.') else destination
                if digest == 'directory':
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    source = Path(package_path) / unit / name if name else Path(package_path) / unit
                    copied, _ = file_hash(source, budget, target)
                    verified, _ = file_hash(target, budget)
                    if copied != digest or verified != digest:
                        raise config.StaleConfigError('Migration source or staged copy changed while staging.')
        config.atomic_write(incoming / config.INI_PATH, ini)
        # Staging never touches production; recheck its full contents immediately before commit.
        if target_state(root, units, budget) != current:
            raise config.StaleConfigError('Destination changed while staging the migration.')
        if recovery_snapshot is not None and inspect(root, package_path, budget=budget, **options)[0]['revision'] != plan['revision']:
            raise config.StaleConfigError('The recovery package, snapshot or destination changed while staging.')
        verify_stopped()
        record = {'revision': plan['revision'], 'units': {unit.as_posix(): current[unit.as_posix()] is not None for unit in units},
                  'status': 'prepared', 'automatic_restart': False, 'package_path': plan['migration']['package_path'],
                  'recovery': plan.get('recovery')}
        config.atomic_write(snapshot / 'manifest.json', json.dumps(record).encode())
        for unit in units:
            (previous / unit).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in sorted((item for item in snapshot.rglob('*') if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            config.sync_directory(directory)
        for directory in (snapshot, snapshot.parent, root):
            config.sync_directory(directory)
        pending_data = json.dumps({'snapshot': str(snapshot.relative_to(root))}).encode()
        config.atomic_write(root / PENDING, pending_data)
        changed = []
        try:
            for unit in units:
                destination, backup = root / unit, previous / unit
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                entry = {'unit': unit, 'moved': False, 'installed': False}
                changed.append(entry)
                if current[unit.as_posix()] is not None:
                    destination.rename(backup)
                    entry['moved'] = True
                    config.sync_directory(destination.parent)
                    config.sync_directory(backup.parent)
                (incoming / unit).rename(destination)
                entry['installed'] = True
                config.sync_directory(destination.parent)
                config.sync_directory((incoming / unit).parent)
            record['status'] = 'installed'
            config.atomic_write(snapshot / 'manifest.json', json.dumps(record).encode())
            (root / PENDING).unlink()
            config.sync_directory((root / PENDING).parent)
        except Exception:
            try:
                # If the final directory sync failed after unlink, restore the start guard.
                config.atomic_write(root / PENDING, pending_data)
                for entry in reversed(changed):
                    unit = entry['unit']
                    if entry['installed']:
                        failed = snapshot / 'failed' / unit
                        failed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        (root / unit).rename(failed)
                    if entry['moved']:
                        (previous / unit).rename(root / unit)
                    config.sync_directory((root / unit).parent)
                record['status'] = 'rolled_back'
                config.atomic_write(snapshot / 'manifest.json', json.dumps(record).encode())
                (root / PENDING).unlink(missing_ok=True)
                config.sync_directory((root / PENDING).parent)
            except Exception:
                raise config.ConfigRecoveryError('Migration rollback failed. Keep the server stopped and recover from its private snapshot.') from None
            raise MigrationInvalidError('Migration failed; the original target units were restored.') from None
        return plan | {'snapshot': str(snapshot.relative_to(root)), 'readiness_verified': False,
                       'restore_tested': False, 'automatic_restart': False}
