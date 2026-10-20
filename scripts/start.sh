#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -e "$ROOT/config/shutdown-policy.json" || -L "$ROOT/config/shutdown-policy.json" ]]; then
    exec python3 "$ROOT/scripts/ark_supervisor.py" "$ROOT"
fi
exec 9>"$ROOT/.server.lock"
flock -n 9 || { echo 'Another server or maintenance task is running.' >&2; exit 1; }
# Passwords stay in the private INI, never in process arguments.
# Without config/server.json, retain the original Genesis/13-player defaults.
URL=$(python3 "$ROOT/scripts/ark_config.py" launch-url)
cd "$ROOT/server/ShooterGame/Binaries/Linux"
exec ./ShooterGameServer "$URL" -server -log -NoCrashDialog
