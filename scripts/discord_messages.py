"""Participant-facing Discord text; never changes control results or evidence."""
from dataclasses import dataclass
import ipaddress
import math
import os
import re
import threading
import time
import urllib.request


@dataclass(frozen=True)
class JoinInfo:
    # Explicit owner configuration, not a guessed outbound/NAT address.
    public_query_address: str | None = None

    def __post_init__(self):
        address = self.public_query_address
        if address is None:
            return
        if not isinstance(address, str) or len(address) > 260 or address.count(':') != 1:
            raise ValueError('Set a public IPv4 address or DNS hostname and query port.')
        host, port = address.rsplit(':', 1)
        if not re.fullmatch(r'[1-9][0-9]{0,4}', port) or int(port) > 65535:
            raise ValueError('Set a valid external query port.')
        try:
            parsed = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            labels = host.split('.')
            if (len(host) > 253 or len(labels) < 2 or host != host.lower()
                    or host.endswith(('.local', '.localhost', '.internal'))
                    or re.fullmatch(r'[0-9.]+', host)
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
                raise ValueError('Set a public IPv4 address or DNS hostname and query port.') from None
        else:
            if not parsed.is_global or parsed.is_multicast:
                raise ValueError('A local or reserved address is not an external connection address.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Address lookup redirects are not permitted.')


def external_ipv4():
    """Bounded TLS-only discovery, no credentials/proxies/redirects/raw logging.

    This discovers an outbound public address, NOT external game reachability.
    Differing answers are ambiguous; never choose one at random.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    found = set()
    for url in ('https://api4.ipify.org/', 'https://checkip.amazonaws.com/'):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'ARK-Discord-Join/1'})
            with opener.open(request, timeout=3) as response:
                if response.status != 200:
                    continue
                raw = response.read(65)
            if len(raw) > 64:
                continue
            value = str(ipaddress.IPv4Address(raw.decode('ascii').strip()))
            if ipaddress.IPv4Address(value).is_global and not ipaddress.IPv4Address(value).is_multicast:
                found.add(value)
        except (OSError, ValueError, UnicodeError):
            continue
    return next(iter(found)) if len(found) == 1 else None


class JoinResolver:
    """Refresh on startup completion and cache status lookups without game actions."""
    def __init__(self, *, port=27015, override=None, discover=external_ipv4, clock=time.monotonic):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('External query port must be between 1 and 65535.')
        self.port, self.override, self.discover, self.clock = port, JoinInfo(override), discover, clock
        self.lock = threading.Lock()
        self.cached, self.until = JoinInfo(), 0

    @classmethod
    def from_environment(cls):
        raw = os.environ.get('ARK_QUERY_PORT', '27015')
        if not re.fullmatch(r'[1-9][0-9]{0,4}', raw):
            raise ValueError('External query port is invalid.')
        return cls(port=int(raw), override=os.environ.get('ARK_PUBLIC_QUERY_ADDRESS') or None)

    def resolve(self, *, force=False):
        if self.override.public_query_address:
            return self.override
        with self.lock:
            if not force and self.clock() < self.until:
                return self.cached
            try:
                address = self.discover()
                self.cached = JoinInfo(f'{address}:{self.port}') if address else JoinInfo()
            except Exception:
                # Address discovery must never turn a successful game operation into a retry.
                self.cached = JoinInfo()
            self.until = self.clock() + (300 if self.cached.public_query_address else 30)
            return self.cached


def join_text(join):
    if join.public_query_address:
        target = f'接続先: `{join.public_query_address}`（Steamへの登録用）'
        step = '上の接続先を入力して追加し、サーバーを選んで「接続」します。'
    else:
        target = '接続先: 外部向けアドレスを取得できませんでした。少し待って /ark status で確認し、続く場合は管理者に確認してください。'
        step = '「外部IPまたはホスト名:問い合わせポート」を入力して追加します（取得後に /ark status に表示します）。'
    return '\n'.join((target, '**Steam（PC）からの参加方法**',
        '1. Steamの「表示」→「ゲームサーバー」→「お気に入り」→「＋（追加）」を開きます。',
        '2. ' + step,
        '3. ARK: Survival Evolvedで参加し、求められたら案内済みの参加パスワードを入力してください。',
        '初回はMODのダウンロードに時間がかかる場合があります。'))


def failure_text(result):
    code = result.get('error', {}).get('code')
    messages = {
        'forbidden': 'このDiscordサーバー内で操作してください。DMでは使えません。',
        'busy': '別の操作が進行中です。完了通知を待ってから操作してください。',
        'service_unavailable': 'ARKサーバーの管理機能が一時的に応答していないため、今は状態確認・起動・再起動ができません。自動更新などのメンテナンス中によく起こり、通常は10〜20分ほどで戻ります。少し待ってからもう一度お試しください。1時間以上続く場合は管理者に連絡してください。',
        'shutdown_unverified': '再起動はまだ利用できません。管理者による準備・確認をお待ちください。',
        'shutdown_failed': '保存・停止の完了を確認できませんでした。起動・再起動を繰り返さず、管理者に連絡してください。',
        'automation_blocked': '前回の操作の確認が必要です。操作を繰り返さず、管理者に連絡してください。',
        'readiness_timeout': '参加準備の完了をまだ確認できません。操作を繰り返さず /ark status で確認し、長く続く場合は管理者に連絡してください。',
        'not_running': 'ARKは停止中です。遊ぶときは /ark start で起動してください。',
        'confirmation_expired': '確認が期限切れ、または使用済みです。このクリックで再起動はしていません。',
        'invalid_configuration': 'サーバー設定の確認が必要なため、起動できません。管理者に連絡してください。',
        'outcome_unknown': '操作の結果を確認できません。操作を繰り返さず、管理者に連絡してください。',
        'operation_failed': '操作を完了できませんでした。操作を繰り返さず、管理者に連絡してください。',
    }
    return messages.get(code, '処理を完了できませんでした。操作を繰り返さず、管理者に連絡してください。')


def ready_text(result, action, join):
    if result.get('ok') is not True:
        return failure_text(result)
    data = result.get('data', {})
    if data.get('readiness_verified') is not True:
        return '参加準備の完了をまだ確認できません。/ark status で確認し、操作を繰り返さず管理者の確認をお待ちください。'
    if data.get('changed') is False:
        title = 'ARKサーバーはすでに起動しており、参加受付中です。'
    elif action == 'restart':
        title = 'ARKサーバーの再起動が完了しました。参加受付中です。'
    else:
        title = 'ARKサーバーが起動しました。参加受付中です。'
    # Qualified native-abort details remain in the unchanged result/audit/receipt.
    # They are not a new participant-facing fault or a claim of clean exit 0.
    return title + '\n\n' + join_text(join)


def stopped_text(result):
    if result.get('ok') is not True:
        return failure_text(result)
    return '参加者がいないことを再確認し、ARKサーバーを停止しました。\n次に遊ぶときは /ark start で起動してください。'


def format_status(result, join=None):
    if result.get('ok') is not True:
        return failure_text(result)
    join = JoinInfo() if join is None else join
    data = result['data']
    phase = data.get('phase')
    names = {'ready': '起動済み・参加受付中', 'starting_or_unavailable': '起動準備中・状態確認中（まだ参加できません）',
             'inactive': '停止中', 'stopped_with_warning': '停止中（保存確認済み）',
             'failed': '停止中・管理者の確認が必要です', 'activating': '起動中（まだ参加できません）',
             'deactivating': '停止処理中（参加できません）'}
    lines = ['**ARK: ' + names.get(phase, '状態を確認できません。管理者に確認してください。') + '**']
    game, metrics = data.get('game') or {}, data.get('metrics') or {}
    count, capacity = game.get('players'), game.get('max_players')
    known_count = type(count) is int and count >= 0
    people = str(count) + (f' / {capacity}人' if type(capacity) is int and capacity > 0 else '人') if known_count else '確認できません'
    lines.append('参加人数: ' + ('—（停止中）' if phase in ('inactive', 'stopped_with_warning') else people))
    map_name = game.get('map')
    if isinstance(map_name, str) and re.fullmatch(r'[A-Za-z0-9_ -]{1,64}', map_name):
        lines.append('マップ: ' + map_name)
    uptime = metrics.get('uptime_seconds')
    if (phase == 'ready' and type(uptime) in (int, float) and math.isfinite(uptime) and uptime >= 0):
        lines.append(f'起動から: {int(uptime // 3600)}時間{int(uptime % 3600 // 60)}分')
    if phase in ('inactive', 'stopped_with_warning'):
        lines.append('遊ぶときは /ark start で起動してください。')
    elif phase in ('activating', 'starting_or_unavailable'):
        lines.append('準備完了の通知をお待ちください。長く続く場合は管理者に確認してください。')
    elif phase == 'failed':
        lines.append('前回の処理の確認が必要です。起動・再起動を繰り返さず、管理者に連絡してください。')
    if data.get('last_shutdown_warning') == 'evidence_unavailable':
        lines.append('前回の保存・停止状況を確認できていません。管理者に確認してください。')
    if data.get('automation_problem'):
        lines.append('操作の進行中、または前回結果の確認待ちです。操作を連打せず、長く続く場合は管理者に連絡してください。')
    lines.append('無人時の自動停止: ' + ('有効（1時間ごとに人数を確認・起動直後は停止しません）' if data.get('auto_stop_enabled') else '無効'))
    if not data.get('restart_enabled'):
        lines.append('再起動機能: 準備・確認待ちのため、まだ利用できません。')
    lines.extend(('', join_text(join)))
    return '\n'.join(lines)
