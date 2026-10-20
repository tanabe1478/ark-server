"""Migration uses tiny synthetic packages, temporary roots and fake systemd only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import ark_config as config, ark_control as control, ark_migration as migration


class Backend:
    def __init__(self):
        self.active, self.result, self.calls = 'inactive', 'success', []
        self.pid, self.exit_status = 0, 0

    def state(self):
        return {'ActiveState': self.active, 'SubState': 'test', 'Result': self.result}

    def operate(self, action):
        self.calls.append(action)

    def metrics(self):
        return {'pid': self.pid, 'exit_status': self.exit_status}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.package = self.base / 'game', self.base / 'package'
        config.apply_config(self.root, {'settings': config.DEFAULTS.copy(),
            'password': 'CURRENT_PRIVATE_PARTICIPANT', 'admin_password': 'CURRENT_PRIVATE_ADMIN'})
        self.write(self.root / migration.SAVED / 'SavedArks/Genesis.ark', b'ORIGINAL_GENESIS')
        self.write(self.root / config.GAME_INI_PATH, b'[/script/shootergame.shootergamemode]\nX=old\n')
        self.write(self.root / migration.MODS / '123/asset.uasset', b'ORIGINAL_MOD')
        self.write(self.root / migration.MODS / '123.mod', b'ORIGINAL_META')
        self.write(self.root / migration.MAP, b'MAP')
        self.write(self.root / 'server/ShooterGame/Binaries/Linux/ShooterGameServer', b'NOT_EXECUTABLE')
        self.write(self.package / config.INI_PATH, b'[SessionSettings]\nSessionName=Imported-Aquatica\n[/Script/Engine.GameSession]\nMaxPlayers=70\n[ServerSettings]\nActiveMods=123,456\nServerPassword=PACKAGE_PRIVATE\nServerAdminPassword=PACKAGE_PRIVATE\nRCONEnabled=True\nTamingSpeedMultiplier=3\n[CustomMod]\nExtra=preserved\n')
        self.write(self.package / config.GAME_INI_PATH, b'[/script/shootergame.shootergamemode]\nEggHatchSpeedMultiplier=4\nBabyMatureSpeedMultiplier=200\n+Array=one\n+Array=two\n')
        self.write(self.package / migration.SAVED / 'SavedArks/Aquatica.ark', b'IMPORTED_WORLD')
        self.write(self.package / migration.SAVED / 'SavedArks/76561198347598830.arkprofile', b'CHARACTER')
        self.write(self.package / migration.SAVED / 'SavedArks/1090834980.arktribe', b'TRIBE')
        self.write(self.package / migration.SAVED / 'SaveGames/test.sav', b'SAVEGAME')
        self.write(self.package / migration.SAVED / 'AllowedCheaterSteamIDs.txt', b'76561198347598830\n')
        for item in ('123', '456'):
            self.write(self.package / migration.MODS / item / 'asset.uasset', ('MOD_' + item).encode())
            self.write(self.package / migration.MODS / (item + '.mod'), ('META_' + item).encode())
        self.write(self.package / 'server/start-aquatica.sh', b'echo NEVER_EXECUTE_THIS\n')
        self.manifest()
        self.backend = Backend()
        self.controller = control.Controller(self.root, self.backend)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
        path.chmod(0o600)

    def manifest(self):
        hashes = {path.relative_to(self.package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.package.rglob('*') if path.is_file() and path.name != 'manifest.json'}
        self.write(self.package / 'manifest.json', json.dumps({'package_sha256': hashes,
            'mods': [{'id': '123'}, {'id': '456'}], 'character_count': 1}).encode())

    def plan(self):
        result = self.controller.execute('migration.plan', {'package_path': str(self.package)})
        self.assertTrue(result['ok'], result)
        return result['data']

    def apply(self, plan, request='import', confirmed=True):
        return self.controller.execute('migration.apply', {'package_path': str(self.package),
            'expected_revision': plan['revision']}, request_id=request, confirmed=confirmed)

    def saved(self):
        return (self.root / migration.SAVED / 'SavedArks/Genesis.ark').read_bytes()

    def test_preview_is_read_only_sanitized_and_preserves_current_credentials(self):
        before = config.configuration_files(self.root)
        plan = self.plan()
        self.assertEqual(plan['settings']['map'], 'Aquatica')
        self.assertEqual(plan['settings']['active_mods'], ['123', '456'])
        self.assertEqual(plan['migration']['characters'], 1)
        self.assertEqual(plan['migration']['tribes'], 1)
        self.assertEqual(plan['settings']['taming_speed'], 3)
        self.assertEqual(plan['settings']['baby_mature_speed'], 200)
        self.assertNotIn('PRIVATE', json.dumps(plan))
        self.assertFalse(plan['automatic_restart'])
        self.assertEqual(config.configuration_files(self.root), before)
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')
        self.assertFalse((self.root / 'backups').exists())
        self.assertEqual(self.backend.calls, [])

    def test_confirmed_import_retires_whole_saved_config_and_replaced_mods_without_starting(self):
        plan = self.plan()
        no = self.apply(plan, confirmed=False)
        self.assertEqual(no['error']['code'], 'confirmation_required')
        self.assertFalse((self.root / 'backups').exists())
        before_config = (self.root / config.SETTINGS_PATH).read_bytes()
        result = self.apply(plan)
        self.assertTrue(result['ok'], result)
        snapshot = self.root / result['data']['snapshot']
        self.assertEqual((snapshot / 'previous' / migration.SAVED / 'SavedArks/Genesis.ark').read_bytes(), b'ORIGINAL_GENESIS')
        self.assertEqual((snapshot / 'previous' / config.SETTINGS_PATH).read_bytes(), before_config)
        self.assertEqual((snapshot / 'previous' / migration.MODS / '123/asset.uasset').read_bytes(), b'ORIGINAL_MOD')
        self.assertEqual((self.root / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'IMPORTED_WORLD')
        self.assertFalse((self.root / migration.SAVED / 'SavedArks/Genesis.ark').exists())
        text = (self.root / config.INI_PATH).read_text()
        self.assertIn('ServerPassword=CURRENT_PRIVATE_PARTICIPANT', text)
        self.assertIn('ServerAdminPassword=CURRENT_PRIVATE_ADMIN', text)
        self.assertIn('RCONEnabled=False', text)
        self.assertIn('Extra=preserved', text)
        self.assertNotIn('PACKAGE_PRIVATE', text)
        self.assertIn('+Array=one\n+Array=two', (self.root / config.GAME_INI_PATH).read_text())
        self.assertEqual(config.load_settings(self.root)['map'], 'Aquatica')
        self.assertEqual(config.load_settings(self.root)['game_port'], 7777)
        self.assertTrue(all(row['content_present'] for row in config.mod_inventory(self.root, ['123', '456'])))
        self.assertFalse((self.root / 'server/start-aquatica.sh').exists())
        self.assertFalse((self.root / migration.PENDING).exists())
        self.assertEqual(json.loads((snapshot / 'manifest.json').read_text())['status'], 'installed')
        self.assertEqual(self.apply(plan), result)  # durable replay, not another import
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(result['data']['readiness_verified'])
        self.assertFalse(result['data']['restore_tested'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        # The private enclosing directories protect retired units without changing
        # their original directory modes (which must remain usable on rollback).
        for path in (snapshot, snapshot / 'previous', snapshot / 'incoming'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        for path in snapshot.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_server_only_package_gets_unreal_version_before_first_start(self):
        source = self.package / config.INI_PATH
        original = source.read_bytes()
        self.assertNotIn(b'Version=', original)
        plan = self.plan()
        # Neither the source package nor production is normalized by a preview.
        self.assertEqual(source.read_bytes(), original)
        result = self.apply(plan)
        self.assertTrue(result['ok'], result)
        text, _ = config.decode_ini((self.root / config.INI_PATH).read_bytes())
        config.validate_ini_credentials(text)
        config.validate_ini_initialization(text)
        self.assertIn('Version=5', text)
        self.assertIn('ActiveMods=123,456', text)
        self.assertIn('Extra=preserved', text)
        self.assertEqual(source.read_bytes(), original)
        self.assertIn('Aquatica?', config.launch_url(self.root))
        self.assertEqual(self.backend.calls, [])

    def test_import_normalizes_wrong_and_case_variant_version_without_losing_other_keys(self):
        source = self.package / config.INI_PATH
        source.write_bytes(source.read_bytes() + b'\n[/script/shootergame.shootergameusersettings]\nVersion=0\nKeep=preserved\n')
        self.manifest()
        result = self.apply(self.plan())
        self.assertTrue(result['ok'], result)
        text, _ = config.decode_ini((self.root / config.INI_PATH).read_bytes())
        config.validate_ini_initialization(text)
        self.assertIn('Version=5', text)
        self.assertIn('Keep=preserved', text)
        self.assertNotIn('Version=0', text)
        self.assertIn('ActiveMods=123,456', text)
        self.assertEqual(self.backend.calls, [])

    def broken_import(self):
        imported = self.apply(self.plan())
        self.assertTrue(imported['ok'], imported)
        snapshot = Path(imported['data']['snapshot']).name
        self.write(self.root / config.INI_PATH, b'[ServerSettings]\nServerPassword=\nServerAdminPassword=\n[/Script/ShooterGame.ShooterGameUserSettings]\nVersion=5\n')
        self.write(self.root / migration.SAVED / 'SavedArks/Aquatica.ark', b'CURRENT_WORLD_WITHOUT_MODS')
        self.backend.active, self.backend.result, self.backend.exit_status = 'failed', 'core-dump', 6
        return snapshot

    def recovery_plan(self, snapshot):
        result = self.controller.execute('migration.recover.plan', {'package_path': str(self.package), 'snapshot': snapshot})
        self.assertTrue(result['ok'], result)
        return result['data']

    def recover(self, plan, request='recover', confirmed=True):
        return self.controller.execute('migration.recover', {'package_path': str(self.package),
            'snapshot': plan['recovery']['snapshot'], 'expected_revision': plan['revision']}, request_id=request, confirmed=confirmed)

    def test_recovery_preview_is_read_only_and_discloses_secret_restore_world_replacement_and_failure(self):
        snapshot = self.broken_import()
        before = config.configuration_files(self.root)
        folders = sorted((self.root / 'backups').iterdir())
        plan = self.recovery_plan(snapshot)
        self.assertTrue(plan['restores_secrets'])
        self.assertFalse(plan['migration']['credentials_preserved'])
        self.assertEqual(plan['recovery']['snapshot'], snapshot)
        self.assertEqual(plan['recovery']['service_before']['Result'], 'core-dump')
        self.assertEqual(plan['recovery']['service_before']['MainPID'], 0)
        self.assertEqual(plan['recovery']['service_before']['ExecMainStatus'], 6)
        self.assertFalse(plan['recovery']['clears_service_failure'])
        self.assertEqual(plan['recovery']['world_before']['bytes'], len(b'CURRENT_WORLD_WITHOUT_MODS'))
        self.assertEqual(plan['recovery']['world_after']['sha256'], hashlib.sha256(b'IMPORTED_WORLD').hexdigest())
        self.assertEqual(plan['changes']['active_mods']['before'], '未取得')
        self.assertEqual(config.configuration_files(self.root), before)
        self.assertEqual(sorted((self.root / 'backups').iterdir()), folders)
        self.assertNotIn('PRIVATE', json.dumps(plan))
        self.assertEqual(self.backend.calls, [])

    def test_confirmed_recovery_retires_current_world_restores_original_and_credentials_but_preserves_failure(self):
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        self.assertEqual(self.recover(plan, confirmed=False)['error']['code'], 'confirmation_required')
        result = self.recover(plan)
        self.assertTrue(result['ok'], result)
        retired = self.root / result['data']['snapshot'] / 'previous'
        self.assertEqual((retired / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'CURRENT_WORLD_WITHOUT_MODS')
        self.assertEqual((self.root / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'IMPORTED_WORLD')
        self.assertEqual((self.root / 'backups' / snapshot / 'previous' / migration.SAVED / 'SavedArks/Genesis.ark').read_bytes(), b'ORIGINAL_GENESIS')
        text = (self.root / config.INI_PATH).read_text()
        self.assertIn('ServerPassword=CURRENT_PRIVATE_PARTICIPANT', text)
        self.assertIn('ServerAdminPassword=CURRENT_PRIVATE_ADMIN', text)
        self.assertNotIn('PACKAGE_PRIVATE', text)
        config.validate_ini_initialization(text)
        self.assertEqual(config.load_editable_settings(self.root)['active_mods'], ['123', '456'])
        self.assertIn('Aquatica?', config.launch_url(self.root))
        record = json.loads((self.root / result['data']['snapshot'] / 'manifest.json').read_text())
        self.assertEqual(record['recovery'], plan['recovery'])
        self.assertEqual(record['status'], 'installed')
        self.assertEqual(self.backend.result, 'core-dump')
        self.assertEqual(self.backend.active, 'failed')
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.root / migration.PENDING).exists())
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(self.recover(plan), result)
        self.assertEqual(self.apply(self.plan(), 'ordinary-migration-still-blocked')['error']['code'], 'shutdown_failed')

    def test_recovery_requires_known_stopped_state_zero_pid_and_known_exit(self):
        snapshot = self.broken_import()
        for active, result, pid, status, error in [
            ('active', 'success', 123, 0, 'server_running'),
            ('failed', 'core-dump', None, 6, 'outcome_unknown'),
            ('failed', 'core-dump', 123, 6, 'outcome_unknown'),
            ('failed', 'core-dump', 0, None, 'outcome_unknown'),
            ('failed', 'signal', 0, 6, 'shutdown_failed'),
            ('failed', 'core-dump', 0, 9, 'shutdown_failed'),
            ('inactive', 'unknown', 0, 0, 'outcome_unknown'),
            ('deactivating', 'success', 0, 0, 'outcome_unknown'),
        ]:
            with self.subTest(active=active, result=result, pid=pid, status=status):
                self.backend.active, self.backend.result, self.backend.pid, self.backend.exit_status = active, result, pid, status
                value = self.controller.execute('migration.recover.plan', {'package_path': str(self.package), 'snapshot': snapshot})
                self.assertEqual(value['error']['code'], error)
        self.assertEqual(self.backend.calls, [])

    def test_recovery_rejects_unsafe_incomplete_uninstalled_and_symlinked_credential_snapshots(self):
        snapshot = self.broken_import()
        params = {'package_path': str(self.package), 'snapshot': snapshot}
        for name in ('../escape', '/tmp/private', 'config-20261018T110527Z-' + '0' * 32):
            self.assertFalse(self.controller.execute('migration.recover.plan', params | {'snapshot': name})['ok'])
        folder = self.root / 'backups' / snapshot
        manifest = folder / 'manifest.json'
        original = manifest.read_bytes()
        manifest.write_bytes(original.replace(b'installed', b'rolled_back'))
        self.assertFalse(self.controller.execute('migration.recover.plan', params)['ok'])
        manifest.write_bytes(original)
        ini = folder / 'previous' / config.INI_PATH
        original = ini.read_bytes()
        ini.unlink()
        self.assertFalse(self.controller.execute('migration.recover.plan', params)['ok'])
        ini.symlink_to(self.package / config.INI_PATH)
        self.assertFalse(self.controller.execute('migration.recover.plan', params)['ok'])
        ini.unlink()
        ini.write_bytes(original.replace(b'CURRENT_PRIVATE_ADMIN', b'CURRENT_PRIVATE_PARTICIPANT'))
        self.assertFalse(self.controller.execute('migration.recover.plan', params)['ok'])
        self.assertEqual(self.backend.calls, [])

    def test_recovery_revision_binds_retired_credentials_and_service_record(self):
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        ini = self.root / 'backups' / snapshot / 'previous' / config.INI_PATH
        original = ini.read_bytes()
        ini.write_bytes(original.replace(b'CURRENT_PRIVATE_ADMIN', b'ROTATED_PRIVATE_ADMIN'))
        self.assertEqual(self.recover(plan, 'changed-credentials')['error']['code'], 'stale_configuration')
        ini.write_bytes(original)
        self.backend.active, self.backend.result, self.backend.exit_status = 'inactive', 'success', 0
        self.assertEqual(self.recover(plan, 'changed-service')['error']['code'], 'stale_configuration')
        self.assertEqual((self.root / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'CURRENT_WORLD_WITHOUT_MODS')
        self.assertEqual(self.backend.calls, [])

    def test_recovery_rechecks_credential_snapshot_after_staging_and_never_replaces_on_change(self):
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        original_hash = migration.file_hash
        changed = False
        def change_snapshot(path, budget, destination=None):
            nonlocal changed
            result = original_hash(path, budget, destination)
            if destination is not None and not changed:
                changed = True
                ini = self.root / 'backups' / snapshot / 'previous' / config.INI_PATH
                ini.write_bytes(ini.read_bytes() + b'\n; changed after preview\n')
            return result
        with patch.object(migration, 'file_hash', change_snapshot):
            result = self.recover(plan)
        self.assertEqual(result['error']['code'], 'stale_configuration')
        self.assertEqual((self.root / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'CURRENT_WORLD_WITHOUT_MODS')
        self.assertFalse((self.root / migration.PENDING).exists())
        self.assertEqual(self.backend.calls, [])

    def test_recovery_rechecks_stopped_service_after_staging(self):
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        file_hash = migration.file_hash
        def change_service(path, budget, destination=None):
            result = file_hash(path, budget, destination)
            if destination is not None:
                self.backend.active = 'active'
            return result
        with patch.object(migration, 'file_hash', change_service):
            result = self.recover(plan)
        self.assertEqual(result['error']['code'], 'server_running')
        self.assertEqual((self.root / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'CURRENT_WORLD_WITHOUT_MODS')
        self.assertFalse((self.root / migration.PENDING).exists())
        self.assertEqual(self.backend.calls, [])

    def test_recovery_interruption_preserves_guard_and_failure_record_without_replaying(self):
        class Interrupted(BaseException):
            pass
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        rename = Path.rename
        def interrupt(path, target):
            if 'incoming' in path.parts and path.name == 'server.json':
                raise Interrupted()
            return rename(path, target)
        with patch.object(Path, 'rename', interrupt), self.assertRaises(Interrupted):
            self.recover(plan)
        pending = json.loads((self.root / migration.PENDING).read_text())
        record = json.loads((self.root / pending['snapshot'] / 'manifest.json').read_text())
        self.assertEqual(record['recovery']['service_before']['Result'], 'core-dump')
        self.assertEqual(self.recover(plan)['error']['code'], 'outcome_unknown')
        self.assertTrue((self.root / migration.PENDING).exists())
        self.assertEqual(self.backend.active, 'failed')
        self.assertEqual(self.backend.calls, [])

    def test_recovery_respects_control_backup_lifetime_locks_and_pending_guard(self):
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        for name in ('.control.lock', '.backup.lock', '.server.lock'):
            with self.subTest(lock=name), (self.root / name).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(self.recover(plan, name.replace('.', '_'))['error']['code'], 'busy')
        self.write(self.root / migration.PENDING, b'{}')
        result = self.controller.execute('migration.recover.plan', {'package_path': str(self.package), 'snapshot': snapshot})
        self.assertEqual(result['error']['code'], 'configuration_recovery_required')
        self.assertEqual(self.recover(plan, 'pending')['error']['code'], 'configuration_recovery_required')
        self.assertEqual(self.backend.calls, [])

    def test_recovery_rename_failure_rolls_back_to_exact_broken_state_without_clearing_error(self):
        snapshot = self.broken_import()
        plan = self.recovery_plan(snapshot)
        files = config.configuration_files(self.root)
        rename = Path.rename
        def fail(path, target):
            if 'incoming' in path.parts and path.name == 'server.json':
                raise OSError('synthetic disk failure')
            return rename(path, target)
        with patch.object(Path, 'rename', fail):
            result = self.recover(plan)
        self.assertEqual(result['error']['code'], 'migration_invalid')
        self.assertEqual(config.configuration_files(self.root), files)
        self.assertEqual((self.root / migration.SAVED / 'SavedArks/Aquatica.ark').read_bytes(), b'CURRENT_WORLD_WITHOUT_MODS')
        self.assertFalse((self.root / migration.PENDING).exists())
        self.assertEqual(self.backend.active, 'failed')
        self.assertEqual(self.backend.calls, [])

    def test_stale_source_and_target_are_rejected_before_replacement(self):
        plan = self.plan()
        self.write(self.package / migration.SAVED / 'SavedArks/Aquatica.ark', b'NEW_WORLD')
        self.manifest()
        self.assertEqual(self.apply(plan)['error']['code'], 'stale_configuration')
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')
        plan = self.plan()
        self.write(self.root / migration.SAVED / 'SavedArks/Genesis.ark', b'NEW_GENESIS')
        self.assertEqual(self.apply(plan, 'target-stale')['error']['code'], 'stale_configuration')
        self.assertEqual(self.saved(), b'NEW_GENESIS')

    def test_hash_mismatch_missing_character_mod_and_map_fail_closed(self):
        candidates = [self.package / migration.MODS / '123/asset.uasset',
                      self.package / migration.SAVED / 'SavedArks/76561198347598830.arkprofile',
                      self.root / migration.MAP]
        for path in candidates:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.unlink()
                result = self.controller.execute('migration.plan', {'package_path': str(self.package)})
                self.assertFalse(result['ok'])
                self.write(path, original)
        self.write(self.package / migration.MODS / '456.mod', b'ALTERED')
        result = self.controller.execute('migration.plan', {'package_path': str(self.package)})
        self.assertEqual(result['error']['code'], 'migration_invalid')
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')

    def test_imported_mature_speed_200_is_preserved_but_unbounded_values_are_rejected(self):
        self.assertEqual(self.plan()['settings']['baby_mature_speed'], 200)
        data = (self.package / config.GAME_INI_PATH).read_bytes().replace(b'Multiplier=200', b'Multiplier=1001')
        self.write(self.package / config.GAME_INI_PATH, data)
        self.manifest()
        result = self.controller.execute('migration.plan', {'package_path': str(self.package)})
        self.assertFalse(result['ok'])
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')

    def test_symlinks_traversal_and_nonregular_entries_are_rejected(self):
        linked = self.base / 'linked-package'
        linked.symlink_to(self.package, target_is_directory=True)
        with self.assertRaises(migration.MigrationInvalidError):
            migration.plan_migration(self.root, str(linked))
        asset = self.package / migration.MODS / '123/asset.uasset'
        asset.unlink()
        asset.symlink_to(self.root / config.INI_PATH)
        with self.assertRaises(migration.MigrationInvalidError):
            migration.plan_migration(self.root, str(self.package))
        asset.unlink()
        os.mkfifo(asset)
        with self.assertRaises(migration.MigrationInvalidError):
            migration.plan_migration(self.root, str(self.package))
        asset.unlink()
        self.write(asset, b'MOD_123')
        manifest = json.loads((self.package / 'manifest.json').read_text())
        manifest['package_sha256']['../escape'] = 'a' * 64
        self.write(self.package / 'manifest.json', json.dumps(manifest).encode())
        with self.assertRaises(migration.MigrationInvalidError):
            migration.plan_migration(self.root, str(self.package))

    def test_active_failed_uncertain_and_lifetime_locked_servers_cannot_import(self):
        plan = self.plan()
        for state, result, code in [('active', 'success', 'server_running'), ('failed', 'core-dump', 'shutdown_failed'), ('inactive', 'unknown', 'outcome_unknown')]:
            self.backend.active, self.backend.result = state, result
            self.assertEqual(self.apply(plan, state)['error']['code'], code)
        self.backend.active, self.backend.result = 'inactive', 'success'
        with (self.root / '.server.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.apply(plan, 'locked')['error']['code'], 'busy')
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')
        self.assertEqual(self.backend.calls, [])

    def test_failed_rename_rolls_back_all_units_and_preserves_private_recovery_copy(self):
        plan = self.plan()
        original = Path.rename
        def fail_runtime(path, target):
            if 'incoming' in path.parts and path.name == 'server.json':
                raise OSError('synthetic disk error')
            return original(path, target)
        with patch.object(Path, 'rename', fail_runtime):
            result = self.apply(plan)
        self.assertEqual(result['error']['code'], 'migration_invalid')
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')
        self.assertEqual(config.load_settings(self.root)['map'], 'Genesis')
        self.assertFalse((self.root / migration.PENDING).exists())
        snapshot = next((self.root / 'backups').glob('migration-*'))
        self.assertEqual(json.loads((snapshot / 'manifest.json').read_text())['status'], 'rolled_back')
        self.assertTrue((snapshot / 'failed' / migration.SAVED / 'SavedArks/Aquatica.ark').is_file())

    def test_rollback_failure_leaves_marker_and_refuses_start(self):
        plan = self.plan()
        original = Path.rename
        def fail(path, target):
            if ('incoming' in path.parts and path.name == 'server.json') or ('previous' in path.parts and path.name == 'Saved'):
                raise OSError('synthetic disk error')
            return original(path, target)
        with patch.object(Path, 'rename', fail):
            result = self.apply(plan)
        self.assertEqual(result['error']['code'], 'configuration_recovery_required')
        self.assertTrue((self.root / migration.PENDING).is_file())
        start = self.controller.execute('start', request_id='must-not-start')
        self.assertEqual(start['error']['code'], 'configuration_recovery_required')
        self.assertEqual(self.backend.calls, [])

    def test_interruption_during_commit_keeps_start_guard_and_does_not_retry(self):
        class Interrupted(BaseException):
            pass
        plan = self.plan()
        original = Path.rename
        def crash(path, target):
            if 'incoming' in path.parts and path.name == 'server.json':
                raise Interrupted()
            return original(path, target)
        with patch.object(Path, 'rename', crash), self.assertRaises(Interrupted):
            self.apply(plan)
        self.assertTrue((self.root / migration.PENDING).exists())
        self.assertEqual(self.apply(plan)['error']['code'], 'outcome_unknown')
        with self.assertRaises(config.ConfigRecoveryError):
            config.launch_url(self.root)
        self.assertEqual(self.backend.calls, [])

    def test_stopped_state_rechecked_after_staging_and_limits_are_bounded(self):
        plan = self.plan()
        stopped = self.backend.state()
        with patch.object(self.backend, 'state', side_effect=[stopped, stopped, stopped | {'ActiveState': 'active'}]):
            result = self.apply(plan)
        self.assertEqual(result['error']['code'], 'outcome_unknown')
        self.assertEqual(self.saved(), b'ORIGINAL_GENESIS')
        self.assertFalse((self.root / migration.PENDING).exists())
        for budget in (migration.Budget(seconds=0), migration.Budget(byte_limit=1), migration.Budget(entry_limit=1)):
            with self.assertRaises(migration.MigrationLimitError):
                migration.inspect(self.root, str(self.package), budget=budget)


if __name__ == '__main__':
    unittest.main()
