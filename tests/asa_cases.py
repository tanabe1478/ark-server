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

    def test_active_mods_block_startup_until_supported(self):
        self.assertEqual(config.mod_inventory(self.root, [])[:1], [])
        self.assertFalse(config.mod_inventory(self.root, ['928102'])[0]['content_present'])


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

if __name__ == '__main__':
    unittest.main()
