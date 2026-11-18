"""ASA-only cases; tests/test_asa.py runs this module with ARK_GAME=asa."""
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

from scripts import ark_config as config, ark_game as game, asa_rcon as rcon
from scripts.query import QueryError


class AsaConfigTests(unittest.TestCase):
    def setUp(self):
        assert game.ASA, 'run through tests/test_asa.py'
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = {'settings': config.DEFAULTS | {'max_players': 20, 'session_name': 'nabe'},
                        'password': 'participant_0123456789', 'admin_password': 'administrator_9876543210'}

    def test_profile_paths(self):
        self.assertEqual(game.APP_ID, 2430930)
        self.assertEqual(config.INI_PATH.parent.name, 'WindowsServer')
        self.assertEqual(str(game.world('Astraeos_WP')), 'SavedArks/Astraeos_WP/Astraeos_WP.ark')
        self.assertEqual(config.DEFAULTS['map'], 'Astraeos_WP')

    def test_apply_enables_internal_rcon_and_launch_url(self):
        config.apply_config(self.root, self.payload)
        text = (self.root / config.INI_PATH).read_text()
        self.assertIn('RCONEnabled=True', text)
        self.assertIn('RCONPort=27020', text)
        (self.root / config.INI_PATH).write_text(text + '\n[/Script/ShooterGame.ShooterGameUserSettings]\nVersion=5\n')
        self.assertEqual(config.launch_url(self.root),
                         'Astraeos_WP?listen?SessionName=nabe?MaxPlayers=20?Port=7777?RCONEnabled=True?RCONPort=27020')
        self.assertEqual(rcon.admin_password(self.root), 'administrator_9876543210')

    def test_rcon_must_stay_enabled_on_the_internal_port(self):
        base = '[ServerSettings]\nServerPassword=a_participant_1\nServerAdminPassword=b_administrator_2\n'
        for extra in ('', 'RCONEnabled=False\nRCONPort=27020\n', 'RCONEnabled=True\nRCONPort=27015\n'):
            with self.subTest(extra=extra), self.assertRaises(config.ConfigError):
                config.validate_ini_credentials(base + extra)
        config.validate_ini_credentials(base + 'RCONEnabled=True\nRCONPort=27020\n')

    def test_active_mods_are_downloaded_by_the_server(self):
        self.assertEqual(config.mod_inventory(self.root, []), [])
        item = config.mod_inventory(self.root, ['929713', '928708'])[1]
        self.assertEqual((item['content_present'], item['order'], item['source']), (True, 2, 'server_download'))


class FakeRcon:
    """One-connection Source-RCON server on loopback."""
    def __init__(self, password, reply):
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.password, self.reply, self.commands = password, reply, []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def read(self, conn):
        size = struct.unpack('<i', conn.recv(4))[0]
        data = b''
        while len(data) < size:
            data += conn.recv(size - len(data))
        request, kind = struct.unpack('<ii', data[:8])
        return request, kind, data[8:-2].decode()

    def send(self, conn, request, kind, body):
        data = struct.pack('<ii', request, kind) + body.encode() + b'\0\0'
        conn.sendall(struct.pack('<i', len(data)) + data)

    def serve(self):
        conn, _ = self.listener.accept()
        with conn:
            request, _, body = self.read(conn)
            ok = body == self.password
            self.send(conn, request if ok else -1, 2, '')
            if ok:
                request, _, body = self.read(conn)
                self.commands.append(body)
                self.send(conn, request, 0, self.reply)
        self.listener.close()


class AsaRconTests(unittest.TestCase):
    def test_command_authenticates_and_returns_reply(self):
        server = FakeRcon('secret_admin', 'World Saved \n')
        self.assertEqual(rcon.command('SaveWorld', 'secret_admin', port=server.port), 'World Saved')
        self.assertEqual(server.commands, ['SaveWorld'])

    def test_wrong_password_is_an_error(self):
        server = FakeRcon('secret_admin', 'x')
        with self.assertRaises(QueryError):
            rcon.command('ListPlayers', 'wrong_password', port=server.port)

    def test_player_list_is_never_guessed(self):
        self.assertEqual(rcon.parse_players('No Players Connected'), [])
        self.assertEqual(rcon.parse_players('0. yazumaru, 0002f1\n1. sora, 0002f2\n'), ['yazumaru', 'sora'])
        for text in ('', 'Server received, But no response!!', '0. yazumaru'):
            with self.subTest(text=text), self.assertRaises(QueryError):
                rcon.parse_players(text)



class AsaSaveProofTests(unittest.TestCase):
    """The save proof needs ALL of: RCON ack, a NEW log completion, a rewritten world."""
    def setUp(self):
        from unittest.mock import patch
        from scripts import ark_shutdown as n, asa_process
        self.n, self.asa = n, asa_process
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.saved = self.root / n.SAVED
        (self.saved / 'Logs').mkdir(parents=True)
        (self.saved / 'SavedArks/Astraeos_WP').mkdir(parents=True)
        self.world = 'SavedArks/Astraeos_WP/Astraeos_WP.ark'
        (self.saved / self.world).write_bytes(b'w' * 5000)
        self.log = self.saved / 'Logs/ShooterGame.log'
        self.log.write_text('World Save Complete. Took: 1\n')
        backend = type('B', (), {'observer_check': lambda inner: {'started_ns': 0, 'pid': 1}})()
        self.guard = asa_process.AsaShutdownGuard(self.root, backend)
        for target in ('scripts.asa_process.rcon.admin_password', 'scripts.asa_process.time.sleep'):
            patcher = patch(target, return_value='x' if 'admin' in target else None)
            patcher.start(); self.addCleanup(patcher.stop)
        self.deadline = patch('scripts.asa_process.time.monotonic', side_effect=[0, 0, 1, 200, 200, 200])
        self.deadline.start(); self.addCleanup(self.deadline.stop)

    def run_save(self, reply, write_log, rewrite):
        from unittest.mock import patch
        before = self.n.inventory(self.saved)[self.world]
        def command(text, password, **kwargs):
            if write_log:
                with self.log.open('a') as stream:
                    stream.write('World Save Complete. Took: 0.6\n')
            if rewrite:
                os.utime(self.saved / self.world, ns=(before['mtime_ns'] + 10**9, before['mtime_ns'] + 10**9))
            return reply
        with patch('scripts.asa_process.rcon.command', side_effect=command):
            return self.guard._save(self.world, before)

    def test_complete_save_is_confirmed(self):
        result = self.run_save('World Saved', True, True)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['world'], self.world)

    def test_any_missing_piece_is_not_a_save(self):
        for reply, write_log, rewrite in (('Server received, But no response!!', True, True),
                                          ('World Saved', False, True), ('World Saved', True, False)):
            with self.subTest(reply=reply, log=write_log, rewrite=rewrite):
                self.deadline.stop(); self.deadline.start()
                with self.assertRaises(self.n.ShutdownError):
                    self.run_save(reply, write_log, rewrite)


class AsaCaptureRetryTests(unittest.TestCase):
    def test_capture_overlapping_an_autosave_is_copied_again(self):
        from unittest.mock import patch
        from scripts import ark_shutdown as n, asa_process
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / n.SAVED / 'SavedArks').mkdir(parents=True)
            (root / n.SAVED / 'SavedArks/w.ark').write_bytes(b'w' * 10)
            guard = asa_process.AsaShutdownGuard(root, None)
            waits = []
            guard.wait = waits.append
            real = n.capture
            calls = []
            def flaky(source, destination):
                calls.append(1)
                if len(calls) == 1:
                    destination.mkdir(parents=True)
                    (destination / 'partial').write_bytes(b'x')
                    raise n.ShutdownError('Saved changed during capture.')
                return real(source, destination)
            target = root / 'backups/process-shutdown-x'
            with patch.object(asa_process.n, 'capture', side_effect=flaky):
                result = guard._capture_before(target)
            self.assertEqual((len(calls), waits), (2, [asa_process.CAPTURE_RETRY_SECONDS]))
            self.assertIn('SavedArks/w.ark', result)
            self.assertFalse((target / 'before/partial').exists())
            with patch.object(asa_process.n, 'capture', side_effect=n.ShutdownError('changed')), \
                 self.assertRaises(n.ShutdownError):
                guard._capture_before(root / 'backups/process-shutdown-y')
            self.assertEqual(len(waits), 1 + asa_process.CAPTURE_ATTEMPTS - 1)


class AsaProfileTests(unittest.TestCase):
    def test_expected_profile_binds_build_binary_and_proton(self):
        from unittest.mock import patch
        from scripts import asa_process
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'server/steamapps').mkdir(parents=True)
            (root / 'server/steamapps/appmanifest_2430930.acf').write_text('"AppState"\n{\n\t"buildid"\t\t"26672069"\n}\n')
            (root / game.BINARY).parent.mkdir(parents=True)
            (root / game.BINARY).write_bytes(b'MZ')
            proton = root / 'proton'
            proton.mkdir()
            (proton / 'version').write_text('1700000000 GE-Proton11-11\n')
            with patch.object(asa_process.game, 'PROTON', proton):
                value = asa_process.expected(root, {'contract': 'x'})
        self.assertEqual(value['build_id'], '26672069')
        self.assertEqual(value['proton'], '1700000000 GE-Proton11-11')
        self.assertEqual(value['native_points'], {})
        self.assertEqual(value['observer_contract'], {'contract': 'x'})

    def test_maintenance_tools_use_the_asa_backend(self):
        # Configuration edits and map switches verify the last stop before
        # touching INIs; with the ASE backend an ASA receipt can never pass.
        from scripts import asa_process, compose_configure, compose_map
        from scripts.compose_configure import game_backend
        self.assertIs(compose_configure.game_backend, game_backend)
        self.assertIs(compose_map.game_backend, game_backend)
        with tempfile.TemporaryDirectory() as folder:
            backend = game_backend(Path(folder))
            self.addCleanup(backend.popen.close)
        self.assertIsInstance(backend, asa_process.AsaBackend)
        self.assertIs(backend.GUARD, asa_process.AsaShutdownGuard)


class AsaIntegrityTests(unittest.TestCase):
    def test_client_user_settings_may_change_but_server_settings_may_not(self):
        from collections import Counter
        from scripts.compose_integrity import unmanaged
        values = Counter({('/script/shootergame.shootergameusersettings', 'bhassavedgame', 'False'): 1,
                          ('serversettings', 'rconport', '27020'): 1})
        kept = unmanaged(values, 'GameUserSettings.ini')
        self.assertEqual(set(kept), {('serversettings', 'rconport', '27020')})
        self.assertEqual(unmanaged(values, 'Game.ini'), values)

if __name__ == '__main__':
    unittest.main()


class AsaCaptureTests(unittest.TestCase):
    """ASA rewrites its logs and ServerMetrics heartbeat while running."""
    def capture_while(self, relative):
        from unittest.mock import patch
        from scripts import ark_shutdown as n
        real = n.shutil.copytree
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / 'Saved'
            for name, data in (('SavedArks/Astraeos_WP/Astraeos_WP.ark', b'w' * 5000),
                               ('Logs/ShooterGame.log', b'log'), ('ServerMetrics/nekosaba_7777.run', b'beat')):
                (saved / name).parent.mkdir(parents=True, exist_ok=True)
                (saved / name).write_bytes(data)
            def copy_then_write(source, *args, **kwargs):
                result = real(source, *args, **kwargs)
                if Path(source) == saved:  # copytree recurses through this name
                    with (saved / relative).open('ab') as stream:
                        stream.write(b'+changed')
                return result
            with patch.object(n.shutil, 'copytree', side_effect=copy_then_write):
                return n.capture(saved, Path(folder) / 'snapshot')

    def test_heartbeat_and_log_changes_do_not_abort_a_stop(self):
        for relative in ('ServerMetrics/nekosaba_7777.run', 'Logs/ShooterGame.log'):
            entries = self.capture_while(relative)
            # Recorded as copied: the snapshot's own bytes, never the moving source.
            self.assertEqual(entries[relative]['bytes'], len(b'beat' if 'Metrics' in relative else b'log'))
            self.assertIn('SavedArks/Astraeos_WP/Astraeos_WP.ark', entries)

    def test_progress_changes_still_abort(self):
        from scripts import ark_shutdown as n
        with self.assertRaises(n.ShutdownError):
            self.capture_while('SavedArks/Astraeos_WP/Astraeos_WP.ark')


class AsaQualificationTests(unittest.TestCase):
    def cycle(self):
        state = {'ActiveState': 'inactive', 'Result': 'success', 'MainPID': 0, 'InvocationID': 'a' * 32,
                 'Runtime': 'compose-process', 'ObserverReady': False, 'SupervisorReturnCode': 0,
                 'PrivateBootstrap': True, 'OOMKilled': False}
        world = 'SavedArks/Astraeos_WP/Astraeos_WP.ark'
        receipt = {'schema': 5, 'game': 'asa', 'clean_exit': True, 'warning': None, 'service_record': state,
                   'runtime': {'r': 1}, 'native_policy': {'p': 1}, 'world': world,
                   'save': {'confirmed': True, 'rcon_reply': 'World Saved', 'world': world}}
        return receipt, state, {'runtime': {'r': 1}, 'native_policy': {'p': 1}}

    def test_clean_private_exit_is_accepted(self):
        from scripts.compose_apply import asa_cycle
        asa_cycle(*self.cycle())

    def test_any_gap_is_rejected(self):
        from scripts.compose_apply import asa_cycle
        for change in (lambda r, s: s.update(SupervisorReturnCode=-6), lambda r, s: s.update(PrivateBootstrap=False),
                       lambda r, s: s.update(OOMKilled=None), lambda r, s: r['save'].update(confirmed=False),
                       lambda r, s: r.update(schema=4), lambda r, s: r.update(clean_exit=False),
                       lambda r, s: r.update(runtime={'other': 1})):
            receipt, state, certificate = self.cycle()
            change(receipt, state)
            with self.assertRaises(ValueError):
                asa_cycle(receipt, state, certificate)

    def test_trial_profile_probe_uses_the_asa_profile(self):
        source = Path('scripts/compose_profile_probe.py').read_text()
        self.assertIn('n._expected(n.CODE_ROOT)', source)
        self.assertNotIn('compose_profiles', source)


class AsaEngineTests(unittest.TestCase):
    def test_server_targets_match_compose_asa(self):
        import re
        from scripts.compose_engine import TARGETS
        text = Path('compose.asa.yaml').read_text()
        declared = dict((target, name) for name, target in re.findall(r'- (\w+):(/opt/ark/\S+)', text))
        extra = {target: value for target, value in TARGETS['server'].items() if target in declared}
        self.assertEqual(len(declared), 4)
        self.assertEqual(extra, {target: (name, True) for target, name in declared.items()})


class AsaUpdaterTests(unittest.TestCase):
    def test_status_waits_for_a_slow_stopped_controller(self):
        # A stopped ASA controller hashes Saved on status (5.3 s seen live).
        import inspect, re
        from scripts import compose_update
        source = inspect.getsource(compose_update)
        self.assertGreaterEqual(compose_update.STATUS_TIMEOUT, 30)
        self.assertFalse(re.search(r"call\(\{'method':'status'\},timeout=\d", source))

    def updater(self, folder):
        from unittest.mock import Mock
        from scripts import compose_update as u
        engine = Mock(project='ark-asa', endpoint='unix:///x.sock')
        updater = u.Updater(root=Path(folder), engine=engine)
        updater.record = {'job': 'a' * 32}
        updater.folder = Path(folder) / 'job'
        updater.folder.mkdir()
        return u, updater, engine

    def test_candidate_images_are_built_for_asa(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            u, updater, engine = self.updater(folder)
            def run(command, **kwargs):
                Path(command[command.index('--iidfile') + 1]).write_text('sha256:' + 'b' * 64)
                return type('R', (), {'returncode': 0})()
            with patch.object(u.subprocess, 'run', side_effect=run) as called:
                updater.build(Path(folder), 'server', 'runtime')
        command = called.call_args.args[0]
        self.assertIn('ARK_GAME=asa', command)

    def test_trial_gets_its_own_proton_prefix_and_runtime_paths(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from scripts import ark_shutdown as n
        with tempfile.TemporaryDirectory() as folder:
            u, updater, engine = self.updater(folder)
            n.save(Path(folder) / u.PENDING, {'files': {}})
            (Path(folder) / n.SAVED).mkdir(parents=True)
            n.save(Path(folder) / 'config/server.json', {})
            class Stop(Exception):
                pass
            engine.run_job.side_effect = Stop
            with ExitStack() as stack:
                stack.enter_context(patch('scripts.ark_docker.verify_files'))
                stack.enter_context(patch.object(u, 'persist'))
                with self.assertRaises(Stop):
                    updater.qualify('sha256:' + 'c' * 64)
        args, kwargs = engine.run_job.call_args
        targets = {mount[2]: mount for mount in args[5]}
        for target in ('/opt/ark/compat', '/opt/ark/server/ShooterGame/Binaries/Win64/ShooterGame',
                       '/opt/ark/server/ShooterGame/.sentry-native', '/opt/ark/server/Engine/Saved'):
            suffix, subpath, _, readonly = targets[target]
            self.assertEqual(suffix, 'staging')
            self.assertTrue(subpath.startswith('jobs/' + 'a' * 32 + '/trial/'))
            self.assertFalse(readonly)
        self.assertEqual((kwargs['pids'], kwargs['oomd_omit'], kwargs['timeout']), (4096, True, 3600))


class AsaModFlagTests(unittest.TestCase):
    def test_mods_are_passed_in_load_order(self):
        from unittest.mock import patch
        from scripts import asa_worker
        settings = {'max_players': 20}
        with patch.object(asa_worker.ark_config, 'load_settings', return_value=settings), \
             patch.object(asa_worker.ark_config, 'load_editable_settings', return_value={'active_mods': ['929713', '947033']}):
            public = asa_worker.flags(Path('/x'), False)
            private = asa_worker.flags(Path('/x'), True)
        self.assertIn('-mods=929713,947033', public)
        self.assertEqual(private[-1], '-MultiHome=127.0.0.1')
        with patch.object(asa_worker.ark_config, 'load_settings', return_value=settings), \
             patch.object(asa_worker.ark_config, 'load_editable_settings', return_value={'active_mods': []}):
            self.assertFalse(any(flag.startswith('-mods') for flag in asa_worker.flags(Path('/x'), False)))

    def test_new_rules_and_toggles_write_server_settings(self):
        self.assertEqual(config.RULES['difficulty']['ini_key'], 'OverrideOfficialDifficulty')
        for key, ini in (('allow_speed_leveling', 'AllowSpeedLeveling'), ('allow_flyer_speed_leveling', 'AllowFlyerSpeedLeveling')):
            self.assertEqual((config.TOGGLES[key]['ini_key'], config.TOGGLES[key]['section']), (ini, 'ServerSettings'))
