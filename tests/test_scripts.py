"""Integration tests with a fake server/systemctl; never touch the real service."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from scripts import ark_config as config

SOURCE = Path(__file__).resolve().parent.parent


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        for name in ("start.sh", "backup.sh", "install-update.sh", "ark_config.py"):
            shutil.copy2(SOURCE / "scripts" / name, self.root / "scripts" / name)
        self.payload = {
            "settings": config.DEFAULTS.copy(),
            "password": "participant_0123456789",
            "admin_password": "administrator_9876543210",
        }
        config.apply_config(self.root, self.payload)
        self.mockbin = self.root / "mockbin"
        self.mockbin.mkdir()
        systemctl = self.mockbin / "systemctl"
        systemctl.write_text(
            '#!/usr/bin/env bash\n'
            'printf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"\n'
            'if [[ "$*" == *is-active* ]]; then exit "${TEST_ACTIVE_RC:-3}"; fi\n'
            'if [[ "$*" == *" stop "* ]]; then exit "${TEST_STOP_RC:-0}"; fi\n'
            'if [[ "$*" == *--property=ActiveState* ]]; then echo "${TEST_STOP_STATE:-inactive}"; exit "${TEST_SHOW_RC:-0}"; fi\n'
            'if [[ "$*" == *--property=Result* ]]; then echo "${TEST_STOP_RESULT:-success}"; exit "${TEST_SHOW_RC:-0}"; fi\n'
        )
        systemctl.chmod(0o700)
        self.environment = os.environ | {
            "PATH": str(self.mockbin) + os.pathsep + os.environ["PATH"],
            "TEST_SYSTEMCTL_LOG": str(self.root / "systemctl.log"),
            "TEST_ACTIVE_RC": "3",
        }

    def run_script(self, name):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / name)],
            env=self.environment, capture_output=True, text=True, timeout=15,
        )

    def fake_binary(self):
        binary = self.root / "server/ShooterGame/Binaries/Linux/ShooterGameServer"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
        binary.chmod(0o700)
        return binary

    def test_start_passes_one_url_argument_with_no_passwords(self):
        self.fake_binary()
        result = self.run_script("start.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(result.stdout)
        self.assertEqual(args, [config.launch_url(self.root), "-server", "-log", "-NoCrashDialog"])
        self.assertNotIn(self.payload["password"], result.stdout)
        self.assertNotIn(self.payload["admin_password"], result.stdout)

    def test_start_refuses_missing_credentials_before_executing_binary(self):
        self.fake_binary()
        (self.root / config.INI_PATH).write_text("[ServerSettings]\n")
        result = self.run_script("start.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to start", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_initial_setup_includes_unreal_initialization_version(self):
        text, _ = config.decode_ini((self.root / config.INI_PATH).read_bytes())
        config.validate_ini_initialization(text)
        parsed = config.parse_ini(text)
        self.assertEqual(config._ini_value(parsed, config.GAME_USER_SETTINGS_SECTION, 'Version', ''), '5')

    def test_start_refuses_missing_or_invalid_version_without_executing_binary(self):
        self.fake_binary()
        path = self.root / config.INI_PATH
        original = path.read_text()
        for replacement in ('', 'Version=0', 'Version=6', 'Version=PRIVATE_INVALID_VALUE'):
            with self.subTest(replacement=replacement):
                path.write_text(original.replace('Version=5', replacement))
                result = self.run_script('start.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('initialization version', result.stderr)
                self.assertNotIn('PRIVATE_INVALID_VALUE', result.stderr)
                self.assertNotIn(self.payload['password'], result.stderr)
                self.assertEqual(result.stdout, '')

    def test_start_accepts_utf16_initialized_configuration(self):
        self.fake_binary()
        path = self.root / config.INI_PATH
        path.write_bytes(path.read_text().encode('utf-16'))
        result = self.run_script('start.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0], config.launch_url(self.root))

    def test_start_and_update_refuse_held_server_lock(self):
        with (self.root / ".server.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for name in ("start.sh", "install-update.sh"):
                with self.subTest(name=name):
                    result = self.run_script(name)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("running", result.stderr)

    def test_backup_roundtrip_contains_saved_and_is_private(self):
        saved = self.root / "server/ShooterGame/Saved"
        (saved / "SavedArks").mkdir()
        (saved / "SavedArks/Genesis.ark").write_bytes(b"test-world-data")
        result = self.run_script("backup.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list((self.root / "backups").glob("genesis-*.tar.gz"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        with tarfile.open(archives[0]) as archive:
            self.assertEqual(archive.extractfile("Saved/SavedArks/Genesis.ark").read(), b"test-world-data")
            self.assertIn(self.payload["password"].encode(), archive.extractfile("Saved/Config/LinuxServer/GameUserSettings.ini").read())
        self.assertFalse(list((self.root / "backups").glob("*.partial")))
        self.assertNotIn("--user start", (self.root / "systemctl.log").read_text())

    def test_backup_restarts_only_a_previously_running_server(self):
        self.environment["TEST_ACTIVE_RC"] = "0"
        result = self.run_script("backup.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "systemctl.log").read_text().splitlines()
        self.assertEqual(calls, [
            "--user is-active --quiet ark-genesis.service",
            "--user stop ark-genesis.service",
            "--user show ark-genesis.service --property=ActiveState --value",
            "--user show ark-genesis.service --property=Result --value",
            "--user start ark-genesis.service",
        ])

    def test_backup_abnormal_or_unverified_stop_never_archives_or_restarts(self):
        cases = ({"TEST_STOP_STATE": "failed", "TEST_STOP_RESULT": "core-dump"},
                 {"TEST_STOP_STATE": "inactive", "TEST_STOP_RESULT": "signal"},
                 {"TEST_STOP_STATE": "active"}, {"TEST_SHOW_RC": "1"},
                 {"TEST_STOP_RC": "1"})
        self.environment["TEST_ACTIVE_RC"] = "0"
        for changes in cases:
            with self.subTest(changes=changes):
                environment = self.environment | changes
                result = subprocess.run(["bash", str(self.root / "scripts/backup.sh")],
                                        env=environment, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "backups").exists())
                self.assertNotIn("--user start", (self.root / "systemctl.log").read_text())

    def test_backup_refuses_live_server_lock_without_writing_archive(self):
        with (self.root / ".server.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_script("backup.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup aborted", result.stderr)
        self.assertFalse((self.root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
