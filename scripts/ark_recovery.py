"""Bounded archive checks and rollback of allowlisted non-secret settings only."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import zlib

if __package__:
    from . import ark_config as config
else:
    import ark_config as config

BACKUP_ID = r"genesis-[0-9]{8}-[0-9]{6}\.tar\.gz"
FILES = (config.INI_PATH, config.SETTINGS_PATH, config.GAME_INI_PATH)


class ArchiveInvalidError(config.ConfigError):
    pass


class ArchiveLimitError(config.ConfigError):
    pass


def _read(path, limit=2 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise config.ConfigError('Snapshot entry must be a regular file.')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise config.ConfigError('Configuration snapshot exceeds the safe read limit.')
    return data


def snapshot_files(root, name):
    if not isinstance(name, str) or not re.fullmatch(config.SNAPSHOT_ID, name):
        raise config.ConfigError('Invalid configuration snapshot ID.')
    folder = root / 'backups' / name
    if folder.is_symlink() or not folder.is_dir():
        raise config.ConfigError('Configuration snapshot is unavailable.')
    manifest_bytes = _read(folder / 'manifest.json', 16384)
    manifest = json.loads(manifest_bytes)
    allowed = {str(path) for path in FILES}
    legacy = {str(path) for path in FILES[:2]}
    if not isinstance(manifest, dict) or set(manifest) not in (allowed, legacy) or any(type(value) is not bool for value in manifest.values()) or not manifest[str(config.INI_PATH)]:
        raise config.ConfigError('Unsupported configuration snapshot manifest.')
    data = tuple(_read(folder / path.name) if manifest.get(str(path), False) else None for path in FILES)
    digest = hashlib.sha256(manifest_bytes + config.settings_revision(*data).encode()).hexdigest()
    return data, manifest, digest


def configuration_snapshots(root):
    result = []
    for folder in sorted((root / 'backups').glob('config-*'), reverse=True):
        if not re.fullmatch(config.SNAPSHOT_ID, folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            created = datetime.strptime(folder.name.split('-')[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
            # Inventory never opens INI files. Full validation happens only on preview.
            manifest = json.loads(_read(folder / 'manifest.json', 16384))
            keys = {str(path) for path in FILES}
            valid = isinstance(manifest, dict) and set(manifest) in (keys, {str(path) for path in FILES[:2]}) and all(type(value) is bool for value in manifest.values()) and manifest.get(str(config.INI_PATH), False)
            valid = valid and all(not (folder / path.name).is_symlink() and (folder / path.name).is_file() for path in FILES if manifest.get(str(path), False))
            result.append({'name': folder.name, 'created': created, 'restorable': bool(valid)})
        except (OSError, ValueError, KeyError, RecursionError):
            continue
        if len(result) == 30:
            break
    return result


def plan_restore(root, name):
    data, manifest, digest = snapshot_files(root, name)
    desired = config.editable_from_bytes(*data)
    current = config.load_editable_settings(root)
    # A legacy snapshot that did not include Game.ini cannot roll it back.
    if str(config.GAME_INI_PATH) not in manifest:
        for key, rule in config.RULES.items():
            if rule['file'] == 'Game.ini':
                desired[key] = current[key]
    changes = {key: desired[key] for key in config.EDITABLE_SETTINGS if current[key] != desired[key]}
    preview = config.plan_settings(root, changes or {'session_name': current['session_name']})
    base_revision = preview['revision']
    revision = hashlib.sha256((base_revision + digest).encode()).hexdigest()
    return preview | {'revision': revision, 'snapshot': name, 'restores_secrets': False,
                      'managed_keys_only': True}, changes, base_revision


def restore_settings(root, name, expected_revision):
    preview, changes, base_revision = plan_restore(root, name)
    if preview['revision'] != expected_revision:
        raise config.StaleConfigError('The current configuration or snapshot changed after preview.')
    if not changes:
        return preview | {'changed': False}
    # patch_settings rechecks the live files under the server lifetime lock, creates
    # another private checkpoint, and rolls back all three files on write failure.
    result = config.patch_settings(root, changes, base_revision)
    return result | {'restored_from': name, 'restores_secrets': False, 'managed_keys_only': True}


class LimitedReader:
    def __init__(self, stream, deadline, limit):
        self.stream, self.deadline, self.limit, self.total = stream, deadline, limit, 0

    def read(self, size=-1):
        if time.monotonic() > self.deadline:
            raise ArchiveLimitError('Archive verification time limit reached.')
        data = self.stream.read(size)
        if time.monotonic() > self.deadline:
            raise ArchiveLimitError('Archive verification time limit reached.')
        self.total += len(data)
        if self.total > self.limit:
            raise ArchiveLimitError('Archive exceeds the decompressed inspection limit.')
        return data


def verify_backup(root, name, *, timeout=30, byte_limit=40 * 1024**3, entry_limit=50000):
    """Read/hash/inspect only. NEVER extract files, execute scripts or alter Saved."""
    if not isinstance(name, str) or not re.fullmatch(BACKUP_ID, name):
        raise config.ConfigError('Invalid completed backup filename.')
    path = root / 'backups' / name
    deadline = time.monotonic() + timeout
    expected_world = f"Saved/SavedArks/{config.load_settings(root)['map']}.ark"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as raw:
            before = os.fstat(raw.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ArchiveInvalidError('Archive must be a regular file.')
            if before.st_size > 8 * 1024**3:
                raise ArchiveLimitError('Archive exceeds the compressed inspection limit.')
            digest = hashlib.sha256()
            while chunk := raw.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise ArchiveLimitError('Archive verification time limit reached.')
                digest.update(chunk)
            raw.seek(0)
            names, world, credentials = set(), False, False
            with gzip.GzipFile(fileobj=raw) as zipped:
                reader = LimitedReader(zipped, deadline, byte_limit)
                with tarfile.open(fileobj=reader, mode='r|') as archive:
                    for member in archive:
                        parts = PurePosixPath(member.name).parts
                        if len(names) >= entry_limit or len(member.name) > 1024:
                            raise ArchiveLimitError('Archive has too many entries or a path is too long.')
                        if not parts or parts[0] != 'Saved' or '..' in parts or '\\' in member.name or not member.name.isprintable() or member.sparse is not None or not (member.isfile() or member.isdir()) or (member.isfile() and member.name.endswith('/')):
                            raise ArchiveInvalidError('Archive contains an unsafe entry.')
                        normalized = '/'.join(parts)
                        if normalized in names:
                            raise ArchiveInvalidError('Archive contains duplicate entries.')
                        names.add(normalized)
                        world |= member.isfile() and member.size > 0 and normalized == expected_world
                        credentials |= member.isfile() and member.size > 0 and normalized == 'Saved/Config/LinuxServer/GameUserSettings.ini'
                # Consume gzip through its trailer; tar EOF alone does not prove CRC.
                while reader.read(1024 * 1024):
                    pass
            after = os.fstat(raw.fileno())
            current = path.stat(follow_symlinks=False)
            fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if fingerprint(before) != fingerprint(after) or fingerprint(before) != fingerprint(current):
                raise ArchiveInvalidError('Archive changed during verification.')
            if not world or not credentials:
                raise ArchiveInvalidError('Archive does not contain the expected world and configuration.')
            return {'changed': True, 'game_changed': False, 'backup': name, 'size': before.st_size,
                    'created': before.st_mtime, 'verified_at': time.time(), 'sha256': digest.hexdigest(),
                    'entries': len(names), 'gzip_crc': True, 'safe_paths': True,
                    'world_present': True, 'configuration_present': True, 'restore_tested': False}
    except (ArchiveInvalidError, ArchiveLimitError):
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError, zlib.error):
        raise ArchiveInvalidError('Archive is unavailable, incomplete or corrupt.') from None
