import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import ark_config as config
from scripts.ark_automation import Automation, failure_text, format_status, INTERVAL_SECONDS
from scripts.ark_bot_config import BotConfig, BotConfigError, CONFIG_PATH, TOKEN_PATH, load_token, private_json
from scripts.ark_control import Controller, Actor, ControlError, SystemdBackend
from scripts.discord_adapter import Context, GuildPolicy
from scripts import discord_setup
from test_control import FakeBackend


def TIMEOUT(*args, **kwargs):
    raise OSError('private raw query failure')

GUILD = '123456789012345678'
CHANNEL = '223456789012345678'
USER = '323456789012345678'


class MetricsBackend(FakeBackend):
    def __init__(self, state='inactive'):
        super().__init__(state)
        self.pid = 17
        self.uptime = 7200
        self.bad_stop = False

    def state(self, *args):
        value = super().state(*args)
        value['MainPID'] = self.pid if self.active == 'active' else 0
        value['ExecMainStatus'] = 6 if self.active == 'failed' else 0
        if self.active == 'failed':
            value['Result'] = 'core-dump'
        return value

    def operate(self, action):
        if action == 'stop' and self.bad_stop:
            self.calls.append(action)
            self.active = 'failed'
        else:
            super().operate(action)

    def metrics(self):
        return {'pid': self.pid if self.active == 'active' else 0,
                'uptime_seconds': self.uptime, 'service_memory': 1024**3}


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, value):
        self.value += value


class AutomationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config.apply_config(self.root, {'settings': config.DEFAULTS.copy(),
                                       'password': 'participant-secret', 'admin_password': 'administrator-secret'})
        self.game = {'players': 0, 'map': 'Genesis', 'max_players': 13, 'password_required': True}
        self.backend = MetricsBackend()
        self.controller = Controller(self.root, backend=self.backend, probe=lambda *args, **kwargs: dict(self.game))
        self.clock = Clock()
        self.automation = Automation(BotConfig(guild_id=GUILD), self.controller, clock=self.clock.now, sleep=self.clock.sleep)
        self.sleep = patch('scripts.ark_control.time.sleep')
        self.delay = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def idle(self, request='idle-request'):
        return self.controller.execute('stop.idle', {'minimum_uptime_seconds': 3600}, confirmed=True, request_id=request)

    def enabled(self):
        self.automation.config = BotConfig(guild_id=GUILD, notification_channel_id=CHANNEL, shutdown_verified=True, auto_stop_enabled=True)


class GuardedIdleTests(AutomationFixture):
    def setUp(self):
        super().setUp()
        self.backend.active = 'active'

    def test_zero_rechecked_under_control_and_backup_locks(self):
        probes = []
        def probe(*args, **kwargs):
            for path in ('.control.lock', '.backup.lock'):
                with (self.root / path).open('a') as stream:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probes.append(1)
            return self.game
        self.controller.probe = probe
        result = self.idle()
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.backend.calls, ['stop'])
        self.assertEqual(len(probes), 2)
        self.delay.assert_called_once_with(5)
        self.assertEqual(INTERVAL_SECONDS, 3600)

    def test_unknown_and_nonzero_counts_never_stop(self):
        for index, value in enumerate((None, True, False, '0', -1, 1, 4)):
            with self.subTest(value=value):
                self.game['players'] = value
                result = self.idle('unknown-' + str(index))
                self.assertTrue(result['ok'], result)
                self.assertFalse(result['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_query_timeout_never_means_zero(self):
        self.controller.probe = TIMEOUT
        result = self.idle()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['reason'], 'query_unavailable')
        self.assertEqual(self.backend.calls, [])

    def test_lost_initialization_never_authorizes_unattended_stop(self):
        path = self.root / config.INI_PATH
        path.write_text(path.read_text().replace('Version=5', 'Version=0'))
        result = self.idle()
        self.assertEqual(result['error']['code'], 'invalid_configuration')
        self.assertEqual(self.backend.calls, [])

    def test_player_joins_during_recheck(self):
        self.delay.side_effect = lambda *_: self.game.update(players=1)
        result = self.idle()
        self.assertFalse(result['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_nameless_long_connection_does_not_block_idle_stop(self):
        self.game['players'] = 1
        self.controller.roster = lambda *a, **k: [('', 975 * 60)]
        self.assertTrue(self.idle()['data']['changed'])
        self.assertEqual(self.backend.calls, ['stop'])

    def test_named_short_or_unlisted_players_still_block_idle_stop(self):
        rosters = ([('ALU', 975 * 60)], [('', 600)], [('', 975 * 60), ('sora', 60)], [], [('', float('nan'))], TIMEOUT)
        for index, roster in enumerate(rosters):
            with self.subTest(roster=roster):
                self.game['players'] = 1
                self.controller.roster = roster if callable(roster) else (lambda r: lambda *a, **k: r)(roster)
                self.assertFalse(self.idle('kept-' + str(index))['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_pid_changes_during_recheck(self):
        self.delay.side_effect = lambda *_: setattr(self.backend, 'pid', 18)
        self.assertFalse(self.idle()['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_startup_grace_or_unknown_uptime_never_stops(self):
        for index, value in enumerate((0, 3599, None, True, float('nan'), float('inf'))):
            self.backend.uptime = value
            self.assertFalse(self.idle('uptime-' + str(index))['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_wrong_map_and_unknown_pid_never_stop(self):
        self.game['map'] = 'AnotherMap'
        self.assertFalse(self.idle('map')['data']['changed'])
        self.game['map'] = 'Genesis'
        self.backend.pid = 0
        self.assertFalse(self.idle('pid')['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_abnormal_stop_is_not_success_and_is_not_retried(self):
        self.backend.bad_stop = True
        first = self.idle()
        second = self.idle()
        self.assertEqual(first['error']['code'], 'shutdown_failed')
        self.assertEqual(first, second)
        self.assertEqual(self.backend.calls, ['stop'])
        self.assertEqual(self.backend.active, 'failed')

    def test_idle_requires_confirmation_and_bounded_grace(self):
        result = self.controller.execute('stop.idle', {'minimum_uptime_seconds': 3600})
        self.assertEqual(result['error']['code'], 'confirmation_required')
        for value in (True, 0, 299, 86401, '3600'):
            result = self.controller.execute('stop.idle', {'minimum_uptime_seconds': value}, confirmed=True)
            self.assertEqual(result['error']['code'], 'invalid_request')
        self.assertEqual(self.backend.calls, [])

    def test_fresh_request_protects_an_already_running_server(self):
        result = self.controller.execute('start', request_id='fresh-player-request')
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['changed'])
        result = self.idle()
        self.assertFalse(result['data']['changed'])
        self.assertEqual(result['data']['reason'], 'request_grace_or_unknown_record')
        self.assertEqual(self.backend.calls, [])

    def test_corrupt_request_grace_record_never_authorizes_stop(self):
        path = self.root / 'logs/control/start-grace.json'
        path.parent.mkdir(parents=True)
        config.atomic_write(path, b'{"monotonic":true}')
        self.assertFalse(self.idle()['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_zero_pid_and_exit_zero_are_required_even_with_success_result(self):
        for pid, exit_code, expected in ((0, 6, 'shutdown_failed'), (None, 0, 'outcome_unknown'),
                                         (17, 0, 'outcome_unknown'), (0, None, 'outcome_unknown')):
            with self.assertRaises(ControlError) as caught:
                Controller._verify_stopped({'ActiveState': 'inactive', 'Result': 'success',
                                            'MainPID': pid, 'ExecMainStatus': exit_code})
            self.assertEqual(caught.exception.code, expected)
        backend = SystemdBackend()
        with patch.object(backend, '_run', return_value='LoadState=loaded\nActiveState=inactive\nResult=success\nMainPID=0\nExecMainStatus=6\n'):
            self.assertEqual(backend.state()['ExecMainStatus'], 6)

    def test_backup_conflict_prevents_stop(self):
        with (self.root / '.backup.lock').open('w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.idle()
        self.assertEqual(result['error']['code'], 'busy')
        self.assertEqual(self.backend.calls, [])


class AutomationTests(AutomationFixture):
    def test_start_reports_only_actual_readiness(self):
        result = self.automation.execute('start', request_id='start')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['data']['readiness_verified'])
        self.assertEqual(self.backend.calls, ['start'])
        self.assertEqual(private_json(self.automation.record)['phase'], 'completed')
        self.assertEqual(self.automation.record.stat().st_mode & 0o777, 0o600)

    def test_already_running_start_is_idempotent(self):
        self.backend.active = 'active'
        first = self.automation.execute('start', request_id='first')
        second = self.automation.execute('start', request_id='second')
        self.assertTrue(first['ok'] and second['ok'])
        self.assertFalse(first['data']['changed'])
        self.assertEqual(self.backend.calls, [])

    def test_delayed_query_response(self):
        probes = iter((OSError('private raw failure'), self.game))
        def probe(*args, **kwargs):
            value = next(probes)
            if isinstance(value, Exception):
                raise value
            return value
        self.controller.probe = probe
        self.assertTrue(self.automation.execute('start')['ok'])
        self.assertEqual(self.clock.value, 5)
        self.assertEqual(self.backend.calls, ['start'])

    def test_provisional_startup_metadata_is_waited_for_not_replayed(self):
        values = iter((self.game | {'map': 'None', 'password_required': False}, self.game))
        self.controller.probe = lambda *a, **k: next(values)
        result = self.automation.execute('start')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.clock.value, 5)
        self.assertEqual(self.backend.calls, ['start'])

    def test_readiness_timeout_does_not_stop_or_restart(self):
        self.controller.probe = TIMEOUT
        self.automation.config = BotConfig(guild_id=GUILD, readiness_timeout_seconds=30)
        result = self.automation.execute('start')
        self.assertEqual(result['error']['code'], 'readiness_timeout')
        self.assertEqual(self.backend.calls, ['start'])
        self.assertEqual(self.backend.active, 'active')
        self.assertEqual(self.clock.value, 30)

    def test_authentication_or_map_mismatch_is_not_ready(self):
        self.game['password_required'] = False
        result = self.automation.execute('start')
        self.assertEqual(result['error']['code'], 'invalid_configuration')
        self.assertEqual(self.backend.calls, ['start'])
        self.assertNotIn('participant-secret', json.dumps(result))

    def test_live_ini_initialization_loss_is_not_ready(self):
        original = self.backend.operate
        def lose_ini(action):
            original(action)
            gus = self.root / config.INI_PATH
            gus.write_text(gus.read_text().replace('Version=5', 'Version=0'))
        self.backend.operate = lose_ini
        result = self.automation.execute('start')
        self.assertEqual(result['error']['code'], 'invalid_configuration')
        self.assertEqual(self.backend.calls, ['start'])

    def test_restart_gated_before_live_shutdown_validation(self):
        self.backend.active = 'active'
        result = self.automation.execute('restart', confirmed=True)
        self.assertEqual(result['error']['code'], 'shutdown_unverified')
        self.assertFalse(self.automation.record.exists())
        self.assertEqual(self.backend.calls, [])

    def test_verified_restart_stops_before_start_and_is_deduplicated(self):
        self.enabled()
        self.backend.active = 'active'
        actor = Actor('discord', USER, GUILD, CHANNEL)
        first = self.automation.execute('restart', request_id='same', confirmed=True, actor=actor)
        second = self.automation.execute('restart', request_id='same', confirmed=True, actor=actor)
        self.assertTrue(first['ok'] and second['ok'], (first, second))
        self.assertEqual(self.backend.calls, ['stop', 'start'])

    def test_bad_stop_blocks_restart_and_future_mutations(self):
        self.enabled()
        self.backend.active = 'active'
        self.backend.bad_stop = True
        result = self.automation.execute('restart', confirmed=True)
        self.assertEqual(result['error']['code'], 'shutdown_failed')
        self.assertEqual(self.backend.calls, ['stop'])
        self.assertEqual(private_json(self.automation.record)['phase'], 'blocked')
        self.assertEqual(self.automation.execute('start')['error']['code'], 'automation_blocked')
        self.assertEqual(self.automation.idle_check()['error']['code'], 'automation_blocked')
        self.assertEqual(self.backend.calls, ['stop'])

    def test_pending_operation_survives_process_restart_without_replay(self):
        self.automation._save('start', 'interrupted', 'pending')
        other = Automation(self.automation.config, self.controller)
        result = other.execute('start')
        self.assertEqual(result['error']['code'], 'automation_blocked')
        self.assertEqual(private_json(other.record)['request_id'], 'interrupted')
        self.assertEqual(self.backend.calls, [])

    def test_completion_storage_failure_is_unknown_not_a_safe_retry(self):
        original = self.automation._save
        def fail_completion(action, request, phase, code=None):
            if phase == 'completed':
                raise OSError('private-storage-error')
            return original(action, request, phase, code)
        with patch.object(self.automation, '_save', side_effect=fail_completion):
            result = self.automation.execute('start')
        self.assertEqual(result['error']['code'], 'outcome_unknown')
        self.assertEqual(self.backend.calls, ['start'])
        self.assertEqual(private_json(self.automation.record)['phase'], 'blocked')
        self.assertEqual(self.automation.execute('start')['error']['code'], 'automation_blocked')
        self.assertEqual(self.backend.calls, ['start'])

    def test_concurrent_request_is_busy(self):
        with self.automation._exclusive():
            result = self.automation.execute('start')
        self.assertEqual(result['error']['code'], 'busy')
        self.assertEqual(self.backend.calls, [])

    def test_monitor_mode_never_stops(self):
        self.backend.active = 'active'
        result = self.automation.idle_check()
        self.assertTrue(result['data']['monitor_only'])
        self.assertFalse(self.automation.record.exists())
        self.assertEqual(self.backend.calls, [])

    def test_enabled_idle_stop_uses_shared_guarded_controller(self):
        self.enabled()
        self.backend.active = 'active'
        result = self.automation.idle_check()
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.backend.calls, ['stop'])

    def test_failed_service_never_auto_resets_or_starts(self):
        self.backend.active = 'failed'
        self.backend.result = 'core-dump'
        result = self.automation.execute('start')
        self.assertEqual(result['error']['code'], 'shutdown_failed')
        self.assertEqual(self.backend.calls, [])

    def test_bot_facade_rejects_all_other_actions(self):
        for action in ('backup', 'stop', 'stop.idle', 'settings.apply', 'migration.apply'):
            self.assertFalse(self.automation.execute(action, confirmed=True)['ok'])
        self.assertEqual(self.backend.calls, [])

    def test_status_separates_host_memory_and_game_memory(self):
        self.backend.active = 'active'
        self.game['players'] = None
        result = self.automation.execute('status')
        text = format_status(result)
        self.assertIn('人数: 未取得', text)
        self.assertIn('ARKメモリ:', text)
        self.assertIn('PC全体:', text)
        self.assertNotIn('人数: 0', text)
        self.assertNotIn('participant-secret', text)
        self.assertEqual(self.backend.calls, [])

    def test_exit_six_is_abnormal_in_status_even_if_systemd_reports_success(self):
        with patch.object(self.backend, 'state', return_value={'ActiveState': 'inactive', 'Result': 'success', 'MainPID': 0, 'ExecMainStatus': 6}):
            result = self.automation.execute('status')
            self.assertEqual(result['data']['phase'], 'failed')
            self.assertIn('終了結果を要確認', format_status(result))
            self.assertEqual(self.automation.execute('start')['error']['code'], 'shutdown_failed')
        self.assertEqual(self.backend.calls, [])

    def test_raw_failures_never_appear_in_discord(self):
        value = {'ok': False, 'error': {'code': 'unexpected', 'message': 'token-private-password'}}
        self.assertNotIn('token-private-password', failure_text(value))


class ConfigAndGuildTests(unittest.TestCase):
    def test_every_guild_member_in_any_channel_has_same_three_commands(self):
        policy = GuildPolicy(GUILD)
        for user in (USER, '423456789012345678'):
            for channel in (CHANNEL, '523456789012345678'):
                context = Context(GUILD, channel, user)
                for action in ('status', 'start', 'restart'):
                    self.assertTrue(policy.allows(context, action))
                for action in ('stop', 'backup', 'settings.show', 'migration.apply', 'stop.idle'):
                    self.assertFalse(policy.allows(context, action))
        self.assertFalse(policy.allows(Context(None, CHANNEL, USER), 'start'))
        self.assertFalse(policy.allows(Context('623456789012345678', CHANNEL, USER), 'start'))

    def test_no_guild_wildcards(self):
        for value in (None, '*', 123, GUILD + ' '):
            with self.assertRaises(ValueError):
                GuildPolicy(value)

    def test_private_config_roundtrip_and_fail_closed_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            BotConfig(guild_id=GUILD).save(root)
            self.assertEqual(BotConfig.load(root).guild_id, GUILD)
            with self.assertRaises(BotConfigError):
                BotConfig(auto_stop_enabled=True)
            with self.assertRaises(BotConfigError):
                BotConfig(shutdown_verified='true')
            with self.assertRaises(BotConfigError):
                BotConfig(startup_grace_seconds=True)
            path = root / CONFIG_PATH
            path.chmod(0o644)
            with self.assertRaises(BotConfigError):
                BotConfig.load(root)
            path.unlink()
            path.symlink_to(root / 'absent')
            with self.assertRaises(BotConfigError):
                BotConfig.load(root)

    def test_private_token_is_not_an_argument_or_echoed_in_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            BotConfig().save(root)
            path = root / TOKEN_PATH
            config.atomic_write(path, b'private-token.invalid.token\n')
            self.assertEqual(load_token(root), 'private-token.invalid.token')
            config.atomic_write(path, b'Bot private-token.invalid.token')
            with self.assertRaises(BotConfigError) as caught:
                load_token(root)
            self.assertNotIn('private-token', str(caught.exception))
            path.chmod(0o644)
            with self.assertRaises(BotConfigError):
                load_token(root)

    def test_duplicate_and_unknown_configuration_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            BotConfig().save(root)
            path = root / CONFIG_PATH
            config.atomic_write(path, b'{"guild_id":null,"guild_id":null}')
            with self.assertRaises(BotConfigError):
                BotConfig.load(root)
            config.atomic_write(path, b'{"token":"must-never-be-echoed"}')
            with self.assertRaises(BotConfigError) as caught:
                BotConfig.load(root)
            self.assertNotIn('must-never-be-echoed', str(caught.exception))

    def test_setup_installs_only_bot_and_does_not_start_enable_or_operate_ark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            home = Path(directory) / 'home'
            python = root / '.venv-discord/bin/python'
            python.parent.mkdir(parents=True)
            python.touch()
            source = root / 'deploy/discord/ark-discord.service.in'
            source.parent.mkdir(parents=True)
            source.write_text((Path(__file__).resolve().parents[1] / 'deploy/discord/ark-discord.service.in').read_text())
            with patch('scripts.discord_setup.subprocess.run') as run:
                target = discord_setup.install(root, home)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', '--user', 'daemon-reload'])
            self.assertEqual(target.name, 'ark-discord.service')
            self.assertNotIn('ExecStop=', target.read_text())
            self.assertFalse(BotConfig.load(root).auto_stop_enabled)
            self.assertFalse((root / TOKEN_PATH).exists())
            self.assertEqual(len(list(target.parent.iterdir())), 1)

    def test_local_wizard_keeps_unsafe_shutdown_paths_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            BotConfig().save(root)
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=[GUILD, CHANNEL]), patch('getpass.getpass', return_value='private-token.something.12345'):
                discord_setup.configure(root)
            settings = BotConfig.load(root)
            self.assertEqual(settings.guild_id, GUILD)
            self.assertFalse(settings.shutdown_verified)
            self.assertFalse(settings.auto_stop_enabled)
            self.assertEqual((root / TOKEN_PATH).stat().st_mode & 0o777, 0o600)
