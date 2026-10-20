#!/usr/bin/env bash
set -u
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
pids=()
cleanup() {
    trap - EXIT INT TERM
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
printf '\033]0;ARK Genesis - Live Logs\007'
printf '=== ARK Genesis リアルタイム監視 ===\n'
printf 'Ctrl+C またはウィンドウを閉じる：監視のみ終了（サーバーは停止しません）\n'
printf 'サービスログ、ゲームログ、10秒ごとの人数確認（変化時のみ表示）\n'
printf 'ゲームログはゲーム側のバッファリングにより遅延する場合があります。\n\n'
journalctl --user -u ark-genesis.service -n 30 -f --no-pager &
pids+=("$!")
tail -n 50 -F "$ROOT/server/ShooterGame/Saved/Logs/ShooterGame.log" &
pids+=("$!")
python3 - "$ROOT" <<'PY' &
import datetime, json, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
previous = None
while True:
    try:
        p = subprocess.run([sys.executable, str(root/'scripts/query.py')], capture_output=True, text=True, timeout=8)
        if p.returncode:
            status = '問い合わせ応答なし（起動中・停止中・通信エラーの可能性）'
        else:
            d = json.loads(p.stdout)
            status = f"map={d['map']} players={d['players']}/{d['max_players']} password_required={d['password_required']}"
        if status != previous:
            print(f"[状態 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {status}", flush=True)
            previous = status
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        status = f'監視エラー: {type(exc).__name__}'
        if status != previous:
            print(status, flush=True)
            previous = status
    time.sleep(10)
PY
pids+=("$!")
wait
