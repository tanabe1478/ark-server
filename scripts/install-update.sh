#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec 9>"$ROOT/.server.lock"
flock -n 9 || { echo 'Server or another maintenance task is running. Stop the server first.' >&2; exit 1; }
mkdir -p "$ROOT/logs" "$ROOT/server"
# SteamCMD validation may replace shipped configuration files; preserve ours.
CONFIG="$ROOT/server/ShooterGame/Saved/Config/LinuxServer"
SNAPSHOT=$(mktemp -d "$ROOT/logs/config-update.XXXXXX")
for name in GameUserSettings.ini Game.ini; do
    if [[ -f "$CONFIG/$name" ]]; then cp -p -- "$CONFIG/$name" "$SNAPSHOT/$name"; fi
done
restore_config() {
    local result=$?
    trap - EXIT
    mkdir -p "$CONFIG"
    for name in GameUserSettings.ini Game.ini; do
        if [[ -f "$SNAPSHOT/$name" ]]; then cp -p -- "$SNAPSHOT/$name" "$CONFIG/$name" || result=1; fi
    done
    # Keep the private snapshot for manual recovery if restoration failed.
    if (( result == 0 )); then rm -rf -- "$SNAPSHOT"; fi
    exit "$result"
}
trap restore_config EXIT
"$ROOT/steamcmd/steamcmd.sh" +force_install_dir "$ROOT/server" +login anonymous +app_update 376030 validate +quit
BINARY="$ROOT/server/ShooterGame/Binaries/Linux/ShooterGameServer"
if [[ ! -x "$BINARY" ]]; then echo 'Server binary was not installed.' >&2; exit 1; fi
if ldd "$BINARY" | grep 'not found'; then echo 'Missing shared libraries (see above).' >&2; exit 1; fi
# Older dedicated servers also search ~/.steam/sdk64 for steamclient.so.
if [[ -f "$ROOT/steamcmd/linux64/steamclient.so" ]]; then
    mkdir -p "$HOME/.steam/sdk64"
    if [[ ! -e "$HOME/.steam/sdk64/steamclient.so" && ! -L "$HOME/.steam/sdk64/steamclient.so" ]]; then
        ln -s "$ROOT/steamcmd/linux64/steamclient.so" "$HOME/.steam/sdk64/steamclient.so"
    fi
fi
echo 'Installation/update complete. Server has NOT been started.'
