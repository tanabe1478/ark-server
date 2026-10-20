"""Minimal automation behind Discord, with no socket, shell or Discord dependency."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import threading
import time
import uuid

from . import ark_config, ark_shutdown
from .ark_bot_config import BotConfigError, private_json
from .ark_control import ControlError, Controller, exclusive_lock, response

INTERVAL_SECONDS = 3600


def host_memory():
    try:
        values = {key: int(value.split()[0]) * 1024 for key, value in
                  (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
        return {'used': values['MemTotal'] - values['MemAvailable'], 'total': values['MemTotal']}
    except (OSError, KeyError, ValueError):
        return None


class Automation:
    """One mutation at a time, durable interrupted-operation latch, no auto-replay."""
    def __init__(self, config, controller=None, *, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.controller = Controller() if controller is None else controller
        self.root = self.controller.root
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.faulted = False
        self.record = self.root / 'logs/automation/operation.json'

    def validate(self, action, params, request_id, confirmed):
        if action not in ('status', 'start', 'restart'):
            raise ControlError('invalid_request')
        self.controller.validate(action, params, request_id, confirmed)

    def problem(self):
        if self.faulted:
            return 'automation_blocked'
        if not self.record.exists() and not self.record.is_symlink():
            return None
        value = private_json(self.record)
        if value.get('phase') == 'blocked' and self.controller.reviewed_blocking_operation():
            return None
        if value.get('phase') in ('pending', 'blocked'):
            return 'automation_blocked'
        if value.get('phase') != 'completed':
            raise BotConfigError('Automation record is invalid; inspect it locally.')
        return None

    def status(self):
        result = self.controller.execute('status')
        if not result['ok']:
            return result
        data = result['data']
        valid = False
        try:
            ark_config.launch_url(self.root)  # Validate, never expose the launch URL or INI.
            valid = True
        except (OSError, ValueError):
            pass
        problem = self.problem()
        state = data['service']['ActiveState']
        settings = ark_config.load_settings(self.root)
        game = data.get('game') or {}
        matches = (game.get('map') == settings['map'] and game.get('max_players') == settings['max_players']
                   and game.get('password_required') is True)
        metrics = data.get('metrics') or {}
        service_healthy = (state == 'active' and data['service'].get('Result') == 'success'
                           and type(metrics.get('pid')) is int and metrics['pid'] > 0
                           and (data['service'].get('Runtime') != 'docker' or data['service'].get('ObserverReady') is True))
        phase = ('ready' if service_healthy and data['query_responding'] and valid and matches else 'starting_or_unavailable') if state == 'active' else state
        if state in ('inactive', 'failed'):
            try:
                stopped = (self.controller.verify_startable_stopped(data['service']) if state == 'inactive'
                           else self.controller.verify_operational_stopped(data['service']))
                if not stopped['clean_exit']:
                    phase = 'stopped_with_warning'
            except ControlError:
                phase = 'failed'
        last_warning = None
        try:
            previous = ark_shutdown.private_json(self.root / 'logs/shutdown/last-stop.json')
            if previous.get('warning') == 'expected_native_abort' and previous.get('clean_exit') is False:
                last_warning = 'expected_native_abort'
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            last_warning = 'evidence_unavailable'
        return response(result['request_id'], 'status', data=data | {
            'phase': phase, 'configuration_valid': valid, 'service_healthy': service_healthy, 'host_memory': host_memory(),
            'automation_problem': problem, 'restart_enabled': self.config.shutdown_verified,
            'auto_stop_enabled': self.config.auto_stop_enabled, 'last_shutdown_warning': last_warning,
        })

    def _save(self, action, request_id, phase, code=None):
        self.record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.record.parent.chmod(0o700)
        ark_config.atomic_write(self.record, json.dumps({
            'action': action, 'request_id': request_id, 'phase': phase,
            'code': code, 'updated': time.time(),
        }).encode())

    def _uncertain(self, action, request_id):
        self.faulted = True
        try:
            self._save(action, request_id, 'blocked', 'outcome_unknown')
        except Exception:
            pass  # Original durable intent remains if storage cannot be updated.
        return response(request_id, action, code='outcome_unknown')

    @contextmanager
    def _exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise ControlError('busy')
        try:
            with exclusive_lock(self.root / '.automation.lock'):
                yield
        finally:
            self.lock.release()

    def _wait_ready(self, result):
        deadline = self.clock() + self.config.readiness_timeout_seconds
        invalid = False
        while True:
            status = self.status()
            if not status['ok']:
                return response(result['request_id'], result['action'], code='outcome_unknown')
            data = status['data']
            if data['service']['ActiveState'] != 'active':
                return response(result['request_id'], result['action'], code='operation_failed')
            invalid = not data['configuration_valid']
            if data['query_responding']:
                if not data['service_healthy'] and data['service'].get('Runtime') != 'docker':
                    return response(result['request_id'], result['action'], code='outcome_unknown')
                game = data.get('game') or {}
                settings = ark_config.load_settings(self.root)
                invalid = invalid or (game.get('map') != settings['map'] or game.get('max_players') != settings['max_players']
                                      or game.get('password_required') is not True)
                if not invalid and data['phase'] == 'ready':
                    return response(result['request_id'], result['action'], data=result['data'] | {'readiness_verified': True})
            # Steam may answer during map/INI initialization with provisional
            # metadata. Never declare ready, fail early, or replay the operation.
            if self.clock() >= deadline:
                code = 'invalid_configuration' if invalid else 'readiness_timeout'
                return response(result['request_id'], result['action'], code=code)
            self.sleep(min(5, max(0, deadline - self.clock())))

    def execute(self, action, params=None, *, request_id=None, confirmed=False, actor=None):
        params = {} if params is None else params
        request_id = uuid.uuid4().hex if request_id is None else request_id
        accepted = False
        safe_id = request_id if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id) else None
        safe_action = action if isinstance(action, str) and action in ('status', 'start', 'restart') else None
        try:
            self.validate(action, params, request_id, confirmed)
            if action == 'status':
                return self.status()
            if action == 'restart' and not self.config.shutdown_verified:
                raise ControlError('shutdown_unverified')
            with self._exclusive():
                if self.problem():
                    raise ControlError('automation_blocked')
                state = self.controller.backend.state()
                if state['ActiveState'] in ('inactive', 'failed'):
                    if action == 'start':
                        self.controller.verify_startable_stopped(state)
                    else:
                        self.controller.verify_operational_stopped(state)
                self._save(action, request_id, 'pending')
                accepted = True
                result = self.controller.execute(action, params, request_id=request_id, confirmed=confirmed, actor=actor)
                started = result['ok']
                if started:
                    result = self._wait_ready(result)
                code = None if result['ok'] else result['error']['code']
                unsafe = code in ('shutdown_failed', 'outcome_unknown', 'internal_error', 'operation_failed',
                                  'storage_unavailable', 'service_unavailable', 'configuration_recovery_required')
                unsafe = unsafe or (started and code == 'invalid_configuration')
                phase = 'blocked' if unsafe else 'completed'
                try:
                    self._save(action, request_id, phase, code)
                except Exception:
                    return self._uncertain(action, request_id)
                return result
        except ControlError as error:
            return response(safe_id, safe_action, code=error.code)
        except (OSError, ValueError):
            return self._uncertain(action, request_id) if accepted else response(safe_id, safe_action, code='storage_unavailable')
        except Exception:
            return self._uncertain(action, request_id) if accepted else response(safe_id, safe_action, code='outcome_unknown')

    def idle_check(self):
        """A one-hour tick. Disabled mode only observes and never creates a stop intent."""
        accepted = False
        request_id = None
        try:
            if not self.config.auto_stop_enabled or not self.config.shutdown_verified:
                return {'ok': True, 'data': {'changed': False, 'reason': 'disabled_unverified', 'monitor_only': True}}
            with self._exclusive():
                if self.problem():
                    raise ControlError('automation_blocked')
                request_id = 'idle-' + uuid.uuid4().hex
                self._save('stop.idle', request_id, 'pending')
                accepted = True
                result = self.controller.execute('stop.idle', {'minimum_uptime_seconds': self.config.startup_grace_seconds},
                                                 request_id=request_id, confirmed=True)
                code = None if result['ok'] else result['error']['code']
                try:
                    self._save('stop.idle', request_id, 'completed' if result['ok'] else 'blocked', code)
                except Exception:
                    return self._uncertain('stop.idle', request_id)
                return result
        except ControlError as error:
            return response(None, 'stop.idle', code=error.code)
        except (OSError, ValueError):
            return self._uncertain('stop.idle', request_id) if accepted else response(None, 'stop.idle', code='storage_unavailable')
        except Exception:
            return self._uncertain('stop.idle', request_id) if accepted else response(None, 'stop.idle', code='outcome_unknown')


def format_status(result):
    if not result['ok']:
        return failure_text(result)
    data = result['data']
    names = {'ready': '参加可能', 'starting_or_unavailable': '起動中／応答・設定を要確認', 'inactive': '停止中',
             'stopped_with_warning': '停止済み（保存経路確認・既知SIGABRTの警告あり）',
             'failed': '停止・終了結果を要確認', 'activating': '起動処理中', 'deactivating': '停止処理中'}
    lines = ['ARK: ' + names.get(data['phase'], '状態を要確認')]
    game, metrics = data.get('game') or {}, data.get('metrics') or {}
    count = game.get('players')
    lines.append('人数: ' + (str(count) if type(count) is int else '未取得'))
    uptime = metrics.get('uptime_seconds')
    if data['service']['ActiveState'] == 'active' and type(uptime) in (int, float):
        lines.append(f'稼働時間: {int(uptime // 3600)}時間{int(uptime % 3600 // 60)}分')
    memory = metrics.get('service_memory')
    if type(memory) is int:
        lines.append(f'ARKメモリ: {memory / 1024**3:.2f} GiB')
    host = data.get('host_memory')
    if host:
        lines.append(f"PC全体: {host['used'] / host['total'] * 100:.1f}%（利用可能 {(host['total'] - host['used']) / 1024**3:.1f} GiB）")
    if data.get('last_shutdown_warning') == 'expected_native_abort':
        lines.append('直近の検証済み停止: 保存経路確認・SIGABRT（6）の警告あり。正常終了0とは区別して保全。')
    elif data.get('last_shutdown_warning') == 'evidence_unavailable':
        lines.append('停止証拠の読み取りに問題があります。記録をローカルで確認してください。')
    lines.append('自動停止: ' + ('有効・1時間ごとに判定' if data['auto_stop_enabled'] else '無効'))
    if not data['restart_enabled']:
        lines.append('再起動: 停止・保存の検証待ちのため無効')
    if data['automation_problem']:
        lines.append('操作が進行中または前回結果が未確認です。自動再実行せず、状態とローカル記録を確認してください。')
    return '\n'.join(lines)


def failure_text(result):
    code = result.get('error', {}).get('code', 'internal_error')
    messages = {
        'forbidden': 'このDiscordサーバー以外・DMでは利用できません。',
        'busy': '別の処理が進行中です。重複実行はしません。',
        'shutdown_unverified': '停止・保存の安全性が未検証のため、再起動はまだ無効です。',
        'shutdown_failed': '保存・終了の証拠を確認できませんでした。自動で起動し直さず、停止記録と保存内容を確認してください。',
        'automation_blocked': '前回の操作が失敗または中断しています。自動再実行せず、ローカルで確認してください。',
        'readiness_timeout': '準備完了を時間内に確認できませんでした。プロセスの強制停止・再起動はしていません。',
        'not_running': '停止中のため再起動できません。起動には /ark start を使ってください。',
        'confirmation_expired': '確認が期限切れ・使用済みです。操作は再実行していません。',
        'invalid_configuration': '起動設定・認証・MOD等の検査に失敗しました。秘密は表示しません。',
        'outcome_unknown': '操作結果を確認できません。自動再試行せず、ローカルの状態と記録を確認してください。',
        'operation_failed': 'サービス操作が失敗しました。追加の起動・再試行はせず、状態と終了記録を確認してください。',
    }
    return messages.get(code, '処理を完了できませんでした。状態とローカル記録を確認してください。')
