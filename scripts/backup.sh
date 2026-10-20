#!/usr/bin/env bash
# Consistent backup: stop gracefully, archive Saved, then restart if previously active.
set -euo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -e "$ROOT/config/shutdown-policy.json" || -L "$ROOT/config/shutdown-policy.json" ]]; then
    echo 'Legacy backup stop/restart is disabled under evidence-qualified stopping. Guarded stops preserve Saved before and after; this script did not stop the game.' >&2
    exit 1
fi
exec 8>"$ROOT/.backup.lock"
flock -n 8 || { echo 'Another backup is running.' >&2; exit 1; }
restart=0
if systemctl --user is-active --quiet ark-genesis.service; then
    systemctl --user stop ark-genesis.service
    # A successful `systemctl stop` does NOT imply a successful game exit.
    # Never archive/restart automatically after a crash during shutdown.
    if ! stopped_state=$(systemctl --user show ark-genesis.service --property=ActiveState --value) ||
       ! stopped_result=$(systemctl --user show ark-genesis.service --property=Result --value) ||
       [[ "$stopped_state" != inactive || "$stopped_result" != success ]]; then
        echo 'Server shutdown was abnormal or unverifiable; backup and automatic restart were cancelled.' >&2
        exit 1
    fi
    restart=1
fi
cleanup() {
    local result=$?
    trap - EXIT
    flock -u 9 || true
    if (( restart )); then systemctl --user start ark-genesis.service || result=1; fi
    exit "$result"
}
exec 9>"$ROOT/.server.lock"
trap cleanup EXIT
flock -n 9 || { echo 'Server/maintenance is still running; backup aborted.' >&2; exit 1; }
[[ -d "$ROOT/server/ShooterGame/Saved" ]] || { echo 'No Saved directory.' >&2; exit 1; }
mkdir -p "$ROOT/backups"
FILE="$ROOT/backups/genesis-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$FILE.partial" -C "$ROOT/server/ShooterGame" Saved
mv -- "$FILE.partial" "$FILE"
echo "Backup complete: $FILE"
# Only remove matching completed archives older than 14 days, after a successful backup.
find "$ROOT/backups" -maxdepth 1 -type f -name 'genesis-*.tar.gz' -mtime +14 -delete
