"""Operational features: all game files/backups are synthetic in TemporaryDirectory."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import ark_config as config, ark_recovery as recovery, arkctl
from scripts.ark_control import Controller, SystemdBackend, response


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guard = patch('scripts.ark_control.SystemdBackend._run', side_effect=AssertionError('Never operate the real service'))
        self.backend = self.guard.start()
        self.addCleanup(self.guard.stop)
        config.apply_config(self.root, {'settings': config.DEFAULTS.copy(), 'password': 'PARTICIPANT_TEST_SECRET', 'admin_password': 'ADMINISTRATOR_TEST_SECRET'})

    def apply(self, changes):
        plan = config.plan_settings(self.root, changes)
        return config.patch_settings(self.root, changes, plan['revision'])

    def archive(self, entries=None):
        path = self.root / 'backups/genesis-20260102-030405.tar.gz'
        path.parent.mkdir(exist_ok=True)
        if entries is None:
            entries = [('Saved/SavedArks/Genesis.ark', b'NOT_A_REAL_WORLD'), ('Saved/Config/LinuxServer/GameUserSettings.ini', b'PRIVATE_CONTENT')]
        with tarfile.open(path, 'w:gz') as output:
            for entry in entries:
                if isinstance(entry, tarfile.TarInfo):
                    output.addfile(entry)
                else:
                    name, data = entry
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    output.addfile(info, io.BytesIO(data))
        return path


class GameRuleTests(Fixture):
    def test_defaults_and_metadata_are_allowlisted(self):
        settings = config.load_editable_settings(self.root)
        self.assertEqual(settings['active_mods'], [])
        for key, rule in config.RULES.items():
            self.assertEqual(settings[key], rule['default'])
        self.assertEqual(set(json.loads((self.root / config.SETTINGS_PATH).read_bytes())), set(config.DEFAULTS))

    def test_patch_both_inis_preserves_encoding_arrays_and_credentials(self):
        ini = self.root / config.INI_PATH
        text = ini.read_text() + '\n[Other]\n+Array=one\n+Array=two\n'
        ini.write_bytes(text.encode('utf-16'))
        game = self.root / config.GAME_INI_PATH
        game.write_bytes('; Keep me\r\n[/script/shootergame.shootergamemode]\r\nUnknown=keep\r\n'.encode('utf-8-sig'))
        before = config.configuration_files(self.root)
        changes = {'taming_speed': 3, 'harvest_amount': 2, 'xp_multiplier': 2, 'day_cycle_speed': .5, 'mating_interval': .25, 'egg_hatch_speed': 5, 'baby_mature_speed': 10, 'active_mods': ['731604991', '1609138312']}
        result = self.apply(changes)
        self.assertEqual(set(result['changes']), set(changes))
        self.assertTrue(ini.read_bytes().startswith(b'\xff\xfe'))
        self.assertIn('+Array=one\n+Array=two', ini.read_text(encoding='utf-16'))
        self.assertIn('PARTICIPANT_TEST_SECRET', ini.read_text(encoding='utf-16'))
        self.assertIn('ActiveMods=731604991,1609138312', ini.read_text(encoding='utf-16'))
        self.assertTrue(game.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn(b'Unknown=keep', game.read_bytes())
        self.assertIn(b'EggHatchSpeedMultiplier=5', game.read_bytes())
        self.assertEqual(config.load_editable_settings(self.root), result['settings'])
        self.assertEqual(set(config.load_settings(self.root)), set(config.DEFAULTS))
        snapshot = self.root / result['snapshot']
        for relative, original in zip(recovery.FILES, before):
            self.assertEqual((snapshot / relative.name).read_bytes(), original)
            self.assertEqual(stat.S_IMODE((snapshot / relative.name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
        self.assertNotIn('TEST_SECRET', json.dumps(result))

    def test_bad_rates_and_mod_ids_never_write(self):
        before = config.configuration_files(self.root)
        for value in (0, -.1, 101, True, '3', None, float('inf'), float('nan'), 10**3000):
            with self.subTest(value=type(value).__name__), self.assertRaises(config.ConfigError):
                config.plan_settings(self.root, {'taming_speed': value})
        for ids in ('731604991', ['0'], ['../bad'], ['1', '1'], [True], [{}], ['9' * 20], ['18446744073709551616'], [str(n) for n in range(1, 34)]):
            with self.subTest(ids=str(ids)[:40]), self.assertRaises(config.ConfigError):
                config.plan_settings(self.root, {'active_mods': ids})
        self.assertEqual(config.configuration_files(self.root), before)

    def test_game_ini_participates_in_revision(self):
        plan = config.plan_settings(self.root, {'max_players': 20})
        (self.root / config.GAME_INI_PATH).write_text('[Other]\nValue=external\n')
        with self.assertRaises(config.StaleConfigError):
            config.patch_settings(self.root, {'max_players': 20}, plan['revision'])
        self.assertEqual(config.load_settings(self.root)['max_players'], 13)

    def test_all_three_files_roll_back_after_failure(self):
        before = config.configuration_files(self.root)
        original = config.atomic_write
        def failing(path, data):
            if path == self.root / config.GAME_INI_PATH:
                raise OSError('PRIVATE_FAILURE')
            return original(path, data)
        plan = config.plan_settings(self.root, {'taming_speed': 3, 'egg_hatch_speed': 5})
        with patch.object(config, 'atomic_write', side_effect=failing), self.assertRaises(config.ConfigError) as caught:
            config.patch_settings(self.root, {'taming_speed': 3, 'egg_hatch_speed': 5}, plan['revision'])
        self.assertNotIn('PRIVATE_FAILURE', str(caught.exception))
        self.assertEqual(config.configuration_files(self.root), before)

    def test_mod_content_must_have_real_metadata_and_directory(self):
        self.apply({'active_mods': ['731604991']})
        with self.assertRaises(config.ModFilesMissingError):
            config.launch_url(self.root)
        folder = self.root / 'server/ShooterGame/Content/Mods'
        (folder / '731604991').mkdir(parents=True)
        metadata = folder / '731604991.mod'
        metadata.write_bytes(b'metadata')
        self.assertFalse(config.mod_inventory(self.root, ['731604991'])[0]['content_present'])
        (folder / '731604991/asset.uasset').write_bytes(b'fixture, not real MOD content')
        self.assertTrue(config.mod_inventory(self.root, ['731604991'])[0]['content_present'])
        self.assertIn('Genesis?', config.launch_url(self.root))
        metadata.unlink()
        metadata.symlink_to(self.root / config.INI_PATH)
        with self.assertRaises(config.ModFilesMissingError):
            config.launch_url(self.root)

    def test_invalid_game_ini_and_conflicting_sections_fail_closed(self):
        (self.root / config.GAME_INI_PATH).write_text('PRIVATE_INVALID_LINE')
        with self.assertRaises(config.ConfigError) as caught:
            config.launch_url(self.root)
        self.assertNotIn('PRIVATE_INVALID_LINE', str(caught.exception))
        (self.root / config.GAME_INI_PATH).unlink()
        with (self.root / config.INI_PATH).open('a') as output:
            output.write('TamingSpeedMultiplier=2\n[serversettings]\nTamingSpeedMultiplier=3\nServerPassword=one\nServerAdminPassword=two\n')
        # Put the first value explicitly into the original server section.
        path = self.root / config.INI_PATH
        path.write_text(path.read_text().replace('[ServerSettings]\n', '[ServerSettings]\nTamingSpeedMultiplier=4\n', 1))
        with self.assertRaises(config.ConfigError):
            config.load_editable_settings(self.root)


class RecoveryTests(Fixture):
    def test_restore_only_managed_values_and_preserve_current_secrets_and_unknowns(self):
        first = self.apply({'taming_speed': 4, 'egg_hatch_speed': 8, 'session_name': 'Changed'})
        path = self.root / config.INI_PATH
        text = path.read_text().replace('PARTICIPANT_TEST_SECRET', 'CURRENT_PARTICIPANT_SECRET')
        path.write_text(text + '\n[Unknown]\nKeep=current\n')
        name = Path(first['snapshot']).name
        plan = recovery.plan_restore(self.root, name)[0]
        self.assertFalse(plan['restores_secrets'])
        self.assertTrue(plan['managed_keys_only'])
        self.assertNotIn('SECRET', json.dumps(plan))
        result = recovery.restore_settings(self.root, name, plan['revision'])
        self.assertFalse(result['automatic_restart'])
        self.assertEqual(config.load_editable_settings(self.root)['taming_speed'], 1)
        self.assertEqual(config.load_editable_settings(self.root)['egg_hatch_speed'], 1)
        self.assertIn('CURRENT_PARTICIPANT_SECRET', path.read_text())
        self.assertIn('Keep=current', path.read_text())
        self.assertEqual(len(recovery.configuration_snapshots(self.root)), 2)

    def test_snapshot_and_current_file_changes_invalidate_restore(self):
        result = self.apply({'taming_speed': 4})
        name = Path(result['snapshot']).name
        plan = recovery.plan_restore(self.root, name)[0]
        source = self.root / result['snapshot'] / 'GameUserSettings.ini'
        source.write_bytes(source.read_bytes() + b'\n; external change\n')
        with self.assertRaises(config.StaleConfigError):
            recovery.restore_settings(self.root, name, plan['revision'])
        plan = recovery.plan_restore(self.root, name)[0]
        (self.root / config.GAME_INI_PATH).write_text('[Other]\nValue=changed\n')
        with self.assertRaises(config.StaleConfigError):
            recovery.restore_settings(self.root, name, plan['revision'])

    def test_legacy_snapshot_does_not_restore_game_ini_defaults(self):
        result = self.apply({'max_players': 20})
        folder = self.root / result['snapshot']
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        del manifest[str(config.GAME_INI_PATH)]
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        self.apply({'egg_hatch_speed': 6})
        plan = recovery.plan_restore(self.root, folder.name)[0]
        self.assertNotIn('egg_hatch_speed', plan['changes'])
        recovery.restore_settings(self.root, folder.name, plan['revision'])
        self.assertEqual(config.load_editable_settings(self.root)['egg_hatch_speed'], 6)
        self.assertEqual(config.load_settings(self.root)['max_players'], 13)

    def test_untrusted_snapshot_names_symlinks_and_fifos_are_not_read(self):
        result = self.apply({'max_players': 20})
        folder = self.root / result['snapshot']
        for name in ('../escape', str(folder), 'config-bad', None):
            with self.assertRaises(config.ConfigError):
                recovery.plan_restore(self.root, name)
        source = folder / 'GameUserSettings.ini'
        source.unlink()
        source.symlink_to(self.root / config.INI_PATH)
        with self.assertRaises(OSError):
            recovery.plan_restore(self.root, folder.name)
        source.unlink()
        os.mkfifo(source)
        started = time.monotonic()
        with self.assertRaises(config.ConfigError):
            recovery.plan_restore(self.root, folder.name)
        self.assertLess(time.monotonic() - started, .5)

    def test_archive_verification_hashes_and_reads_crc_without_extracting(self):
        path = self.archive()
        result = recovery.verify_backup(self.root, path.name)
        self.assertEqual(result['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result['entries'], 2)
        self.assertTrue(result['gzip_crc'])
        self.assertFalse(result['restore_tested'])
        self.assertFalse(result['game_changed'])
        self.assertFalse((self.root / 'Saved').exists())
        self.assertNotIn('PRIVATE_CONTENT', json.dumps(result))

    def test_crc_and_truncation_fail_even_after_tar_end(self):
        for corrupt in ('crc', 'truncated'):
            path = self.archive()
            data = bytearray(path.read_bytes())
            if corrupt == 'crc':
                data[-8] ^= 1
            else:
                data = data[:-6]
            path.write_bytes(data)
            with self.assertRaises(recovery.ArchiveInvalidError):
                recovery.verify_backup(self.root, path.name)

    def test_archive_rejects_unsafe_paths_links_duplicates_and_missing_world(self):
        link = tarfile.TarInfo('Saved/link')
        link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
        cases = [[('../escape', b'bad')], [('/Saved/absolute', b'bad')], [('Saved/../escape', b'bad')], [('Saved\\escape', b'bad')], [link], [('Saved/one', b'a'), ('Saved/one', b'b')], [('Saved/Config/LinuxServer/GameUserSettings.ini', b'config')]]
        for entries in cases:
            with self.subTest(entries=str(entries)[:60]):
                path = self.archive(entries)
                with self.assertRaises(recovery.ArchiveInvalidError):
                    recovery.verify_backup(self.root, path.name)
        self.assertFalse((self.root / 'escape').exists())

    def test_archive_time_bytes_and_count_limits_are_not_success(self):
        path = self.archive()
        for kwargs in ({'timeout': -1}, {'byte_limit': 512}, {'entry_limit': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(recovery.ArchiveLimitError):
                recovery.verify_backup(self.root, path.name, **kwargs)

    def test_archive_symlink_and_fifo_fail_without_blocking(self):
        path = self.archive()
        path.unlink()
        path.symlink_to(self.root / config.INI_PATH)
        with self.assertRaises(recovery.ArchiveInvalidError):
            recovery.verify_backup(self.root, path.name)
        path.unlink()
        os.mkfifo(path)
        started = time.monotonic()
        with self.assertRaises(recovery.ArchiveInvalidError):
            recovery.verify_backup(self.root, path.name)
        self.assertLess(time.monotonic() - started, .5)

    def test_archive_verification_does_not_require_a_working_game_service(self):
        path = self.archive()
        result = Controller(self.root).execute('backup.verify', {'backup': path.name}, request_id='verify', confirmed=True)
        self.assertTrue(result['ok'])
        self.backend.assert_not_called()
        self.assertEqual(Controller(self.root).execute('backup.verify', {'backup': path.name}, request_id='verify', confirmed=True), result)


class BackendAndCliTests(Fixture):
    def test_systemd_unset_values_are_not_fabricated_as_zero(self):
        backend = SystemdBackend()
        with patch.object(backend, '_run', return_value='MemoryCurrent=[not set]\nCPUUsageNSec=18446744073709551615\nMainPID=0\nNRestarts=3\nExecMainStatus=6\nActiveEnterTimestampMonotonic=0\n'):
            result = backend.metrics()
        self.assertIsNone(result['service_memory'])
        self.assertIsNone(result['cpu_usage_ns'])
        self.assertIsNone(result['uptime_seconds'])
        self.assertEqual(result['exit_status'], 6)

    def test_new_cli_shapes_route_without_arbitrary_paths_or_shell(self):
        class FakeController:
            calls = []
            def execute(inner, action, params, **kwargs):
                inner.calls.append((action, params, kwargs))
                return response('cli', action, data={})
        controller = FakeController()
        snapshot = Path(self.apply({'taming_speed': 2})['snapshot']).name
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(arkctl.main(['settings', 'restore-plan', snapshot], controller=controller), 0)
            self.assertEqual(arkctl.main(['settings', 'restore', snapshot, '--revision', 'a'*64, '--confirm'], controller=controller), 0)
            self.assertEqual(arkctl.main(['backup-verify', 'genesis-20260102-030405.tar.gz', '--confirm'], controller=controller), 0)
        self.assertEqual([row[0] for row in controller.calls], ['settings.restore.plan', 'settings.restore', 'backup.verify'])
        self.assertTrue(controller.calls[-1][2]['confirmed'])


if __name__ == '__main__':
    unittest.main()
