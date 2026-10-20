"""Local controller tests: temporary game data and an injected fake systemd only."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import ark_config, ark_control, arkctl


class FakeBackend:
    def __init__(self, state="inactive"):
        self.active = state
        self.calls = []
        self.error = None

    def state(self, unit=ark_control.GAME_UNIT):
        return {"ActiveState": self.active if unit == ark_control.GAME_UNIT else "inactive", "SubState": "test", "Result": "success"}

    def operate(self, action):
        self.calls.append(action)
        if self.error:
            raise self.error
        if action in ("start", "restart"):
            self.active = "active"
        elif action == "stop":
            self.active = "inactive"


class ControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = {"settings": ark_config.DEFAULTS.copy(), "password": "participant_0123456789", "admin_password": "administrator_9876543210"}
        ark_config.apply_config(self.root, self.payload)
        self.backend = FakeBackend()
        self.controller = ark_control.Controller(self.root, self.backend, probe=lambda *a, **kw: {"players": 2})

    def execute(self, action, params=None, **kwargs):
        return self.controller.execute(action, params, request_id=kwargs.pop("request_id", "test-request"), **kwargs)

    def plan(self, changes=None):
        result = self.execute("settings.plan", {"changes": {"max_players": 15} if changes is None else changes})
        self.assertTrue(result["ok"], result)
        return result["data"]

    def test_start_is_idempotent_but_does_not_claim_game_readiness(self):
        first = self.execute("start")
        self.assertTrue(first["ok"])
        self.assertFalse(first["data"]["readiness_verified"])
        self.assertEqual(self.execute("start"), first)
        self.assertEqual(self.backend.calls, ["start"])
        again = self.execute("start", request_id="new-request")
        self.assertFalse(again["data"]["changed"])
        self.assertEqual(self.backend.calls, ["start"])

    def test_stop_restart_and_backup_require_explicit_confirmation(self):
        for action in ("stop", "restart", "backup"):
            with self.subTest(action=action):
                result = self.execute(action)
                self.assertEqual(result["error"]["code"], "confirmation_required")
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.root / "logs/control").exists())

    def test_stop_noop_and_restart_does_not_start_stopped_game(self):
        stop = self.execute("stop", confirmed=True)
        self.assertFalse(stop["data"]["changed"])
        restart = self.execute("restart", request_id="restart", confirmed=True)
        self.assertEqual(restart["error"]["code"], "not_running")
        self.assertEqual(self.backend.calls, [])

    def test_status_distinguishes_service_active_from_query_response(self):
        self.backend.active = "active"
        self.assertTrue(self.execute("status")["data"]["query_responding"])
        self.controller.probe = lambda *a, **kw: (_ for _ in ()).throw(OSError("private diagnostic"))
        result = self.execute("status")
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["query_responding"])
        self.assertIsNone(result["data"]["game"])
        self.assertNotIn("private diagnostic", json.dumps(result))
        self.assertFalse((self.root / "logs/control").exists())

    def test_rejects_shell_paths_unknown_keys_and_malformed_ids(self):
        cases = [
            ("shell", {}, "safe"), ("start", {"unit": "other.service"}, "safe"),
            ("start", {}, "../../escape"), ("start", {}, "a" * 65),
            ("settings.plan", {"changes": {"ServerPassword": "not-allowed"}}, "safe"),
            ("settings.plan", {"changes": {"map": "TheIsland"}}, "safe"),
            ("settings.plan", {"changes": {"query_port": 27025}}, "safe"),
            ("settings.plan", {"changes": {"RCONEnabled": True}}, "safe"),
        ]
        for action, params, request_id in cases:
            with self.subTest(action=action, params=params):
                result = self.execute(action, params, request_id=request_id)
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.root / "logs/control").exists())

    def test_replay_conflicts_are_bound_to_actor_and_parameters(self):
        self.backend.active = "active"
        first = self.execute("restart", confirmed=True)
        self.assertTrue(first["ok"])
        other = self.execute("restart", confirmed=True, actor=ark_control.Actor("local", "9999"))
        self.assertEqual(other["error"]["code"], "request_conflict")
        other_action = self.execute("stop", confirmed=True)
        self.assertEqual(other_action["error"]["code"], "request_conflict")
        self.assertEqual(self.backend.calls, ["stop", "start"])

    def test_abnormal_stop_is_not_reported_as_success_or_automatically_restarted(self):
        for action in ("stop", "restart"):
            with self.subTest(action=action):
                backend = FakeBackend("active")
                def unclean(operation):
                    backend.calls.append(operation)
                    backend.active = "failed"
                backend.operate = unclean
                controller = ark_control.Controller(self.root, backend)
                result = controller.execute(action, request_id="unclean-" + action, confirmed=True)
                self.assertEqual(result["error"]["code"], "shutdown_failed")
                self.assertEqual(backend.calls, ["stop"])
                self.assertEqual(controller.execute(action, request_id="unclean-" + action, confirmed=True), result)
                self.assertEqual(backend.calls, ["stop"])

    def test_restart_does_not_start_after_unverified_or_unsuccessful_stop(self):
        initial = {"ActiveState": "active", "SubState": "running", "Result": "success"}
        cases = [({"ActiveState": "inactive", "Result": "signal"}, "shutdown_failed"),
                 ({"ActiveState": "deactivating", "Result": "success"}, "outcome_unknown"),
                 ({"ActiveState": "inactive", "Result": "unknown"}, "outcome_unknown"),
                 ({"ActiveState": "inactive"}, "outcome_unknown"),
                 (ark_control.ControlError("service_unavailable"), "outcome_unknown")]
        for index, (stopped, expected) in enumerate(cases):
            with self.subTest(stopped=type(stopped).__name__):
                backend = FakeBackend("active")
                controller = ark_control.Controller(self.root, backend)
                with patch.object(backend, "state", side_effect=[initial, stopped]):
                    result = controller.execute("restart", request_id="unverified-" + str(index), confirmed=True)
                self.assertEqual(result["error"]["code"], expected)
                self.assertEqual(backend.calls, ["stop"])

    def test_crash_intent_is_not_reexecuted_and_exception_is_not_echoed(self):
        self.backend.error = RuntimeError("SECRET_DIAGNOSTIC")
        first = self.execute("start")
        self.assertEqual(first["error"]["code"], "outcome_unknown")
        second = self.execute("start")
        self.assertEqual(second["error"]["code"], "outcome_unknown")
        self.assertEqual(self.backend.calls, ["start"])
        self.assertNotIn("SECRET_DIAGNOSTIC", json.dumps(first))

    def test_lost_service_state_after_command_is_unknown_and_not_reexecuted(self):
        with patch.object(self.backend, "state", side_effect=[
            {"ActiveState": "inactive", "SubState": "dead", "Result": "success"},
            ark_control.ControlError("service_unavailable"),
        ]):
            first = self.execute("start")
        self.assertEqual(first["error"]["code"], "outcome_unknown")
        self.assertEqual(self.execute("start"), first)
        self.assertEqual(self.backend.calls, ["start"])

    def test_audit_failure_prevents_new_operation(self):
        with patch.object(self.controller, "_audit", side_effect=OSError("private")):
            result = self.execute("start")
        self.assertEqual(result["error"]["code"], "storage_unavailable")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.execute("start")["error"]["code"], "outcome_unknown")

    def test_post_operation_audit_failure_is_unknown_and_never_reexecuted(self):
        audit = self.controller._audit
        count = 0

        def fail_completion(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated disk error")
            return audit(*args, **kwargs)

        with patch.object(self.controller, "_audit", side_effect=fail_completion):
            first = self.execute("start")
        self.assertEqual(first["error"]["code"], "outcome_unknown")
        self.assertEqual(self.execute("start")["error"]["code"], "outcome_unknown")
        self.assertEqual(self.backend.calls, ["start"])

    def test_backup_child_can_acquire_its_own_lock(self):
        original = self.backend.operate

        def operate(action):
            with (self.root / ".backup.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                original(action)

        self.backend.operate = operate
        result = self.execute("backup", confirmed=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.backend.calls, ["backup"])

    def test_audit_and_request_records_are_private_and_exclude_credentials(self):
        self.assertTrue(self.execute("start")["ok"])
        directory = self.root / "logs/control"
        for path in directory.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                content = path.read_text()
                for key in ("password", "admin_password"):
                    self.assertNotIn(self.payload[key], content)
        events = [json.loads(line) for line in (directory / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events], ["accepted", "completed"])
        self.assertTrue(all("params" not in e for e in events))

    def test_control_and_backup_locks_exclude_mutations(self):
        for filename in (".control.lock", ".backup.lock"):
            with self.subTest(filename=filename), (self.root / filename).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = self.execute("start", request_id=filename.replace(".", ""))
                self.assertEqual(result["error"]["code"], "busy")
        self.assertEqual(self.backend.calls, [])

    def test_manual_server_lock_blocks_start(self):
        with (self.root / ".server.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.execute("start")
        self.assertEqual(result["error"]["code"], "busy")
        self.assertEqual(self.backend.calls, [])

    def test_settings_preview_apply_snapshot_and_no_automatic_restart(self):
        before = (self.root / ark_config.INI_PATH).read_bytes()
        plan = self.plan()
        self.assertEqual((self.root / ark_config.INI_PATH).read_bytes(), before)
        result = self.execute("settings.apply", {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}, confirmed=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["settings"]["max_players"], 15)
        self.assertEqual(result["data"]["changes"]["max_players"], {"before": 13, "after": 15})
        snapshot = self.root / result["data"]["snapshot"]
        self.assertEqual((snapshot / "GameUserSettings.ini").read_bytes(), before)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertEqual((snapshot / "GameUserSettings.ini").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backend.calls, [])
        text, _ = ark_config.decode_ini((self.root / ark_config.INI_PATH).read_bytes())
        parsed = ark_config.parse_ini(text)
        self.assertEqual(parsed.get("ServerSettings", "ServerPassword"), self.payload["password"])
        self.assertEqual(parsed.get("ServerSettings", "ServerAdminPassword"), self.payload["admin_password"])
        again = self.plan()
        self.assertFalse(again["changed"])

    def test_settings_apply_requires_stopped_server_and_current_revision(self):
        plan = self.plan()
        params = {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}
        self.backend.active = "active"
        active = self.execute("settings.apply", params, confirmed=True)
        self.assertEqual(active["error"]["code"], "server_running")
        self.backend.active = "inactive"
        ini = self.root / ark_config.INI_PATH
        ini.write_bytes(ini.read_bytes() + b"\n; External edit\n")
        stale = self.execute("settings.apply", params, request_id="second", confirmed=True)
        self.assertEqual(stale["error"]["code"], "stale_configuration")
        self.assertEqual(ark_config.load_settings(self.root)["max_players"], 13)
        self.assertFalse(list((self.root / "backups").glob("config-*")) if (self.root / "backups").exists() else [])

    def test_settings_pair_rolls_back_when_second_file_write_fails(self):
        plan = self.plan()
        paths = (self.root / ark_config.INI_PATH, self.root / ark_config.SETTINGS_PATH)
        originals = [p.read_bytes() for p in paths]
        atomic_write = ark_config.atomic_write
        failed = False

        def fail_once(path, data):
            nonlocal failed
            if path == paths[1] and not failed:
                failed = True
                raise OSError("simulated disk error")
            return atomic_write(path, data)

        with patch.object(ark_config, "atomic_write", side_effect=fail_once):
            result = self.execute("settings.apply", {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual([p.read_bytes() for p in paths], originals)
        self.assertEqual(len(list((self.root / "backups").glob("config-*"))), 1)

    def test_failed_write_restores_a_legacy_install_without_runtime_file(self):
        runtime = self.root / ark_config.SETTINGS_PATH
        runtime.unlink()
        original = (self.root / ark_config.INI_PATH).read_bytes()
        plan = self.plan()
        atomic_write = ark_config.atomic_write
        failed = False

        def fail_after_replace(path, data):
            nonlocal failed
            result = atomic_write(path, data)
            if path == runtime and not failed:
                failed = True
                raise OSError("simulated directory sync failure")
            return result

        with patch.object(ark_config, "atomic_write", side_effect=fail_after_replace):
            result = self.execute("settings.apply", {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertFalse(runtime.exists())
        self.assertEqual((self.root / ark_config.INI_PATH).read_bytes(), original)

    def test_failed_rollback_requires_manual_recovery_from_retained_snapshot(self):
        plan = self.plan()
        atomic_write = ark_config.atomic_write

        def fail_runtime_and_rollback(path, data):
            if path == self.root / ark_config.SETTINGS_PATH:
                raise OSError("simulated persistent storage failure")
            return atomic_write(path, data)

        with patch.object(ark_config, "atomic_write", side_effect=fail_runtime_and_rollback):
            result = self.execute("settings.apply", {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}, confirmed=True)
        self.assertEqual(result["error"]["code"], "configuration_recovery_required")
        self.assertEqual(len(list((self.root / "backups").glob("config-*"))), 1)
        self.assertEqual(self.backend.calls, [])

    def test_failed_snapshot_leaves_live_configuration_untouched(self):
        plan = self.plan()
        original = (self.root / ark_config.INI_PATH).read_bytes()
        atomic_write = ark_config.atomic_write

        def fail_snapshot(path, data):
            if "backups" in path.parts:
                raise OSError("simulated disk error")
            return atomic_write(path, data)

        with patch.object(ark_config, "atomic_write", side_effect=fail_snapshot):
            result = self.execute("settings.apply", {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / ark_config.INI_PATH).read_bytes(), original)

    def test_cli_outputs_json_and_never_echoes_invalid_arguments(self):
        saved_umask = os.umask(0o077)
        self.addCleanup(os.umask, saved_umask)
        for args, text in ((["settings", "plan"], '{"max_players":15}'),
                           (["start", "--bad-SECRET_VALUE"], ""),
                           (["settings", "plan"], '{"max_players":1,"max_players":2}')):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                code = arkctl.main(args, controller=self.controller, stdin=io.StringIO(text))
                result = json.loads(output.getvalue())
                self.assertEqual(code, 0 if result["ok"] else 1)
                self.assertNotIn("SECRET_VALUE", output.getvalue())
                self.assertEqual(result["api_version"], 1)

    def test_patch_rejects_multiline_managed_values_and_preserves_utf16_arrays(self):
        ini = self.root / ark_config.INI_PATH
        text, _ = ark_config.decode_ini(ini.read_bytes())
        ini.write_bytes((text + "\n[Other]\n+Array=first\n+Array=second\n").encode("utf-16"))
        plan = self.plan()
        result = self.execute("settings.apply", {"changes": {"max_players": 15}, "expected_revision": plan["revision"]}, confirmed=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(ini.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff")))
        self.assertIn("+Array=first\n+Array=second", ini.read_bytes().decode("utf-16"))
        text = ini.read_bytes().decode("utf-16").replace("MaxPlayers=15", "MaxPlayers=15\n  continuation")
        ini.write_bytes(text.encode("utf-16"))
        invalid = self.execute("settings.plan", {"changes": {"max_players": 16}})
        self.assertEqual(invalid["error"]["code"], "invalid_configuration")

    def test_systemd_backend_uses_fixed_argv_and_hides_errors(self):
        backend = ark_control.SystemdBackend()
        with patch("scripts.ark_control.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            backend.operate("stop")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/systemctl", "--user", "stop", ark_control.GAME_UNIT])
            self.assertNotIn("shell", run.call_args.kwargs)
        with patch("scripts.ark_control.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "SECRET_DIAGNOSTIC")):
            with self.assertRaises(ark_control.ControlError) as raised:
                backend.operate("stop")
            self.assertNotIn("SECRET_DIAGNOSTIC", str(raised.exception))
        with self.assertRaises(ark_control.ControlError):
            backend.operate("enable-rcon")


if __name__ == "__main__":
    unittest.main()
