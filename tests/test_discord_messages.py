"""Participant text and public-address discovery; no live DNS/HTTP/Discord/game."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from scripts.discord_messages import JoinInfo, JoinResolver, NoRedirect, external_ipv4, failure_text, format_status, ready_text, stopped_text


class ParticipantMessagesTests(unittest.TestCase):
    def setUp(self):
        self.join = JoinInfo('ark.example.org:28015')
        self.result = {'ok': True, 'data': {'phase': 'ready', 'service': {'ActiveState': 'active', 'MainPID': 123},
            'game': {'players': 2, 'max_players': 70, 'map': 'Aquatica'}, 'metrics': {'uptime_seconds': 3660},
            'auto_stop_enabled': True, 'restart_enabled': True, 'automation_problem': None,
            'last_shutdown_warning': 'expected_native_abort', 'warnings': ['expected_native_abort'],
            'clean_exit': False, 'readiness_verified': True, 'changed': True}}

    def test_ready_explains_start_and_external_steam_steps_without_internal_warning(self):
        before = copy.deepcopy(self.result)
        text = ready_text(self.result, 'start', self.join)
        for value in ('起動しました', '参加受付中', 'ark.example.org:28015', '表示', 'ゲームサーバー', 'お気に入り', 'ARK: Survival Evolved', '案内済みの参加パスワード'):
            self.assertIn(value, text)
        for value in ('SIGABRT', '正常終了', '警告:', 'MainPID', '123'):
            self.assertNotIn(value, text)
        self.assertEqual(self.result, before)
        self.assertFalse(self.result['data']['clean_exit'])

    def test_noop_and_restart_have_truthful_distinct_titles(self):
        self.assertIn('再起動が完了', ready_text(self.result, 'restart', self.join))
        self.result['data']['changed'] = False
        self.assertIn('すでに起動', ready_text(self.result, 'start', self.join))

    def test_success_without_verified_readiness_does_not_claim_ready(self):
        for value in (False, None, 1):
            self.result['data']['readiness_verified'] = value
            self.assertNotIn('参加受付中', ready_text(self.result, 'start', self.join))

    def test_status_prioritizes_playability_people_map_and_joining(self):
        text = format_status(self.result, self.join)
        for value in ('起動済み・参加受付中', '2 / 70人', 'Aquatica', '1時間1分', 'ark.example.org:28015', 'お気に入り'):
            self.assertIn(value, text)
        for value in ('SIGABRT', '正常終了0', 'PID', 'GiB', 'PC全体', '保存経路'):
            self.assertNotIn(value, text)

    def test_qualified_stop_is_saved_not_a_claim_of_clean_exit(self):
        self.result['data']['phase'] = 'stopped_with_warning'
        text = format_status(self.result, self.join)
        self.assertIn('停止中（保存確認済み）', text)
        self.assertIn('/ark start', text)
        self.assertNotIn('SIGABRT', text)
        self.assertNotIn('正常', text)
        self.assertFalse(self.result['data']['clean_exit'])

    def test_real_failure_and_missing_evidence_are_not_hidden(self):
        self.result['data'].update(phase='failed', last_shutdown_warning='evidence_unavailable', automation_problem='automation_blocked')
        text = format_status(self.result, self.join)
        self.assertIn('管理者', text)
        self.assertIn('保存・停止状況を確認できていません', text)
        self.assertIn('繰り返さず', text)
        self.assertNotIn('参加受付中', text)
        self.assertNotIn('保存確認済み', text)

    def test_preparing_not_ready_and_unknown_player_count_is_not_zero(self):
        self.result['data']['phase'] = 'starting_or_unavailable'
        for value in (None, True, False, -1, '0'):
            self.result['data']['game']['players'] = value
            text = format_status(self.result, self.join)
            self.assertIn('まだ参加できません', text)
            self.assertIn('参加人数: 確認できません', text)
        self.result['data']['game']['players'] = 0
        self.assertIn('参加人数: 0 / 70人', format_status(self.result, self.join))

    def test_missing_address_is_explicit_not_a_placeholder_real_connection(self):
        text = ready_text(self.result, 'start', JoinInfo())
        self.assertIn('取得できませんでした', text)
        self.assertNotIn('未登録', text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('0.0.0.0', text)
        self.assertNotIn('ark.example.org', text)

    def test_failure_never_becomes_ready_or_displays_raw_details(self):
        for code in ('shutdown_failed', 'outcome_unknown', 'private-raw-token', 'invalid_configuration'):
            result = {'ok': False, 'error': {'code': code, 'message': 'private-raw-token'}}
            text = ready_text(result, 'start', self.join)
            self.assertIn('管理者', text)
            self.assertNotIn('private-raw-token', text)
            self.assertNotIn('参加受付中', text)

    def test_unavailable_control_explains_maintenance_without_claiming_game_state(self):
        result = {'ok': False, 'error': {'code': 'service_unavailable'}}
        for text in (failure_text(result), format_status(result, self.join)):
            self.assertIn('一時的に応答していない', text)
            self.assertIn('メンテナンス', text)
            self.assertNotIn('処理を完了できませんでした', text)
            self.assertNotIn('停止しました', text)
            self.assertNotIn('参加受付中', text)

    def test_idle_stop_is_actionable_without_diagnostic_codes(self):
        text = stopped_text(self.result)
        self.assertIn('停止しました', text)
        self.assertIn('/ark start', text)
        self.assertNotIn('SIGABRT', text)
        self.assertNotIn('正常終了', text)

    def test_map_cannot_inject_mentions_and_nonfinite_uptime_does_not_crash(self):
        self.result['data']['game']['map'] = '@everyone\nprivate-text'
        for value in (float('nan'), float('inf'), True, -1):
            self.result['data']['metrics']['uptime_seconds'] = value
            text = format_status(self.result, self.join)
            self.assertNotIn('@everyone', text)
            self.assertNotIn('private-text', text)
            self.assertNotIn('起動から:', text)

    def test_address_config_blocks_credentials_markup_local_hosts_and_bad_ports(self):
        for address in ('127.0.0.1:27015', '0.0.0.0:27015', '10'+'.0.0.1:27015', 'localhost:27015',
                        'machine.local:27015', 'https://ark.example.org:27015', 'user:secret@ark.example.org:27015',
                        'ark.example.org:0', 'ark.example.org:65536', 'ark.example.org:True',
                        'ark.example.org:27015\n@everyone', '999.0.0.1:27015', '[::1]:27015'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                JoinInfo(address)


class AddressDiscoveryTests(unittest.TestCase):
    def response(self, raw, status=200):
        response = MagicMock(); response.__enter__.return_value = response
        response.status = status; response.read.return_value = raw
        return response

    def test_tls_two_provider_lookup_is_bounded_and_never_contains_credentials(self):
        opener = Mock(); first = self.response(b'8.8.8.8\n'); second = self.response(b'8.8.8.8')
        opener.open.side_effect = [first, second]
        with patch('scripts.discord_messages.urllib.request.build_opener', return_value=opener):
            self.assertEqual(external_ipv4(), '8.8.8.8')
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertTrue(request.full_url.startswith('https://'))
            self.assertEqual(call.kwargs['timeout'], 3)
            self.assertIsNone(request.get_header('Authorization'))
        first.read.assert_called_once_with(65); second.read.assert_called_once_with(65)

    def test_mismatching_public_answers_are_not_guessed(self):
        opener = Mock(); opener.open.side_effect = [self.response(b'8.8.8.8'), self.response(b'1.1.1.1')]
        with patch('scripts.discord_messages.urllib.request.build_opener', return_value=opener):
            self.assertIsNone(external_ipv4())

    def test_invalid_or_failed_discovery_never_uses_lan_address_or_raw_error(self):
        for first in (b'127.0.0.1', ('10'+'.0.0.1').encode(), b'0.0.0.0', b'not-an-address', b'x'*65, b'\xff'):
            opener = Mock(); opener.open.side_effect = [self.response(first), OSError('private-network-detail')]
            with patch('scripts.discord_messages.urllib.request.build_opener', return_value=opener):
                self.assertIsNone(external_ipv4())

    def test_one_available_valid_provider_can_supply_the_address(self):
        opener = Mock(); opener.open.side_effect = [OSError('private'), self.response(b'8.8.8.8')]
        with patch('scripts.discord_messages.urllib.request.build_opener', return_value=opener):
            self.assertEqual(external_ipv4(), '8.8.8.8')

    def test_redirect_is_refused(self):
        request = SimpleNamespace(full_url='https://api4.ipify.org/')
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(request, None, 302, 'redirect', {}, 'http://localhost/')

    def test_resolver_uses_published_query_port_caches_and_refreshes_dynamic_ip(self):
        discover = Mock(side_effect=['8.8.8.8', '1.1.1.1', '8.8.8.8']); now = [10]
        resolver = JoinResolver(port=28015, discover=discover, clock=lambda: now[0])
        self.assertEqual(resolver.resolve().public_query_address, '8.8.8.8:28015')
        resolver.resolve(); discover.assert_called_once_with()
        self.assertEqual(resolver.resolve(force=True).public_query_address, '1.1.1.1:28015')
        now[0] += 301
        self.assertEqual(resolver.resolve().public_query_address, '8.8.8.8:28015')

    def test_discovery_failure_is_short_cached_and_does_not_keep_stale_address(self):
        discover = Mock(side_effect=['8.8.8.8', RuntimeError('private raw network failure'), '1.1.1.1']); now = [10]
        resolver = JoinResolver(discover=discover, clock=lambda: now[0])
        resolver.resolve(); self.assertIsNone(resolver.resolve(force=True).public_query_address)
        resolver.resolve(); self.assertEqual(discover.call_count, 2)
        now[0] += 31
        self.assertEqual(resolver.resolve().public_query_address, '1.1.1.1:27015')

    def test_explicit_ddns_override_needs_no_discovery(self):
        discover = Mock(side_effect=AssertionError('must not connect'))
        resolver = JoinResolver(override='ark.example.org:28015', discover=discover)
        self.assertEqual(resolver.resolve(force=True).public_query_address, 'ark.example.org:28015')
        discover.assert_not_called()

    def test_environment_defaults_to_automatic_discovery_not_manual_input(self):
        with patch.dict('os.environ', {}, clear=True):
            resolver = JoinResolver.from_environment()
        self.assertIsNone(resolver.override.public_query_address); self.assertEqual(resolver.port, 27015)
        with patch.dict('os.environ', {'ARK_QUERY_PORT': '28015'}, clear=True):
            self.assertEqual(JoinResolver.from_environment().port, 28015)
        for port in (0, True, '27015', 65536):
            with self.assertRaises(ValueError):JoinResolver(port=port)
