"""Offline tests. All configuration and fake game data live in TemporaryDirectory."""
import copy
import fcntl
import json
from pathlib import Path
import stat
import tempfile
import unittest

from scripts import ark_config as config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = {
            "settings": config.DEFAULTS.copy(),
            "password": "participant_0123456789",
            "admin_password": "administrator_9876543210",
        }

    def write_ini(self, text, encoding="utf-8"):
        path = self.root / config.INI_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path

    def test_new_install_is_private_and_idempotent(self):
        self.assertTrue(config.apply_config(self.root, self.payload))
        self.assertFalse(config.apply_config(self.root, self.payload))
        for relative in (config.INI_PATH, config.SETTINGS_PATH):
            self.assertEqual(stat.S_IMODE((self.root / relative).stat().st_mode), 0o600)
        runtime = (self.root / config.SETTINGS_PATH).read_text()
        self.assertNotIn(self.payload["password"], runtime)
        self.assertNotIn(self.payload["admin_password"], runtime)
        self.assertEqual(json.loads(runtime), config.DEFAULTS)

    def test_preserves_utf16_comments_unknown_and_repeated_keys(self):
        original = (
            "; Keep this comment\r\n[ServerSettings]\r\nTamingSpeedMultiplier=2.0\r\n"
            "ServerPassword=old\r\nServerAdminPassword=older\r\nRCONEnabled=True\r\n"
            "[Other]\r\n+Array=first\r\n+Array=second\r\n"
        )
        path = self.write_ini(original, "utf-16")
        config.apply_config(self.root, self.payload)
        data = path.read_bytes()
        self.assertTrue(data.startswith((b"\xff\xfe", b"\xfe\xff")))
        text = data.decode("utf-16")
        self.assertIn("; Keep this comment\r\n", text)
        self.assertIn("TamingSpeedMultiplier=2.0", text)
        self.assertIn("+Array=first\r\n+Array=second", text)
        self.assertIn("RCONEnabled=False", text)
        self.assertNotIn("ServerPassword=old", text)
        self.assertFalse(config.apply_config(self.root, self.payload))

    def test_preserves_utf8_bom_and_non_ascii(self):
        path = self.write_ini("[Other]\nMessage=こんにちは\n", "utf-8-sig")
        config.apply_config(self.root, self.payload)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("こんにちは", path.read_text(encoding="utf-8-sig"))

    def test_updates_case_insensitive_and_duplicate_managed_keys(self):
        self.write_ini(
            "[serversettings]\nserverpassword=old\nServerPassword=stale\n"
            "serveradminpassword=oldadmin\nrconenabled=True\n"
            "[ServerSettings]\nServerPassword=other\nServerAdminPassword=otheradmin\n"
        )
        config.apply_config(self.root, self.payload)
        text, _ = config.decode_ini((self.root / config.INI_PATH).read_bytes())
        self.assertNotIn("=stale", text)
        self.assertNotIn("=other", text)
        config.validate_ini_credentials(text)
        self.assertFalse(config.apply_config(self.root, self.payload))

    def test_rejects_invalid_settings_before_writing(self):
        bad_settings = [
            {"map": "Genesis?Password=bad"},
            {"session_name": "bad?Port=1"},
            {"session_name": "bad\nname"},
            {"session_name": " "},
            {"game_port": 65535},
            {"game_port": 80},
            {"query_port": 7777},
            {"query_port": 7778},
            {"max_players": True},
            {"max_players": 0},
            {"extra": "unexpected"},
        ]
        for changes in bad_settings:
            with self.subTest(changes=changes):
                payload = copy.deepcopy(self.payload)
                payload["settings"].update(changes)
                with self.assertRaises(config.ConfigError):
                    config.apply_config(self.root, payload)
                self.assertFalse((self.root / config.INI_PATH).exists())

    def test_rejects_missing_equal_short_and_unsafe_passwords(self):
        for value in (None, "", "short", "spaces are forbidden", "a" * 129, "abc\n" * 5):
            with self.subTest(value=value):
                payload = copy.deepcopy(self.payload)
                payload["password"] = value
                with self.assertRaises(config.ConfigError):
                    config.apply_config(self.root, payload)
        self.payload["admin_password"] = self.payload["password"]
        with self.assertRaises(config.ConfigError):
            config.apply_config(self.root, self.payload)

    def test_invalid_ini_is_not_overwritten_and_error_omits_contents(self):
        secret_line = "credential_that_must_not_be_printed"
        path = self.write_ini(secret_line)
        with self.assertRaises(config.ConfigError) as raised:
            config.apply_config(self.root, self.payload)
        self.assertNotIn(secret_line, str(raised.exception))
        self.assertEqual(path.read_text(), secret_line)

    def test_configuration_refuses_server_lock(self):
        with (self.root / ".server.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(config.ConfigError):
                config.apply_config(self.root, self.payload)
        self.assertFalse((self.root / config.INI_PATH).exists())

    def test_launch_url_uses_custom_settings_without_secrets(self):
        self.payload["settings"].update({"map": "TheIsland", "session_name": "Home ARK", "game_port": 7787, "query_port": 27025})
        config.apply_config(self.root, self.payload)
        url = config.launch_url(self.root)
        self.assertEqual(url, "TheIsland?SessionName=Home ARK?MaxPlayers=13?Port=7787?QueryPort=27025")
        self.assertNotIn(self.payload["password"], url)
        self.assertNotIn(self.payload["admin_password"], url)

    def test_legacy_install_keeps_default_url_and_short_existing_password(self):
        self.write_ini("[ServerSettings]\nServerPassword=old\nServerAdminPassword=different\nRCONEnabled=False\n"
                       "[/Script/ShooterGame.ShooterGameUserSettings]\nVersion=5\n")
        self.assertEqual(config.launch_url(self.root), "Genesis?SessionName=Genesis-Private?MaxPlayers=13?Port=7777?QueryPort=27015")
        self.assertFalse((self.root / config.SETTINGS_PATH).exists())

    def test_legacy_uninitialized_config_is_not_silently_repaired_at_start(self):
        path = self.write_ini("[ServerSettings]\nServerPassword=old\nServerAdminPassword=different\nRCONEnabled=False\n")
        original = path.read_bytes()
        with self.assertRaisesRegex(config.ConfigError, 'initialization version'):
            config.launch_url(self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / config.SETTINGS_PATH).exists())

    def test_launch_fails_closed_for_missing_equal_or_rcon_credentials(self):
        variants = [
            "[ServerSettings]\n",
            "[ServerSettings]\nServerPassword=same\nServerAdminPassword=same\n",
            "[ServerSettings]\nServerPassword=one\nServerAdminPassword=two\nRCONEnabled=True\n",
            "[ServerSettings]\nServerPassword=one\nServerAdminPassword=two\nRCONEnabled=invalid\n",
        ]
        for text in variants:
            with self.subTest(text=text):
                self.write_ini(text)
                with self.assertRaises(config.ConfigError):
                    config.launch_url(self.root)

    def test_permissions_are_repaired_without_changing_contents(self):
        config.apply_config(self.root, self.payload)
        path = self.root / config.INI_PATH
        original = path.read_bytes()
        path.chmod(0o644)
        self.assertTrue(config.apply_config(self.root, self.payload))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_runtime_settings_do_not_fall_back_to_defaults(self):
        path = self.root / config.SETTINGS_PATH
        path.parent.mkdir()
        path.write_text('{"map": "Genesis"}')
        with self.assertRaises(config.ConfigError):
            config.load_settings(self.root)


if __name__ == "__main__":
    unittest.main()
