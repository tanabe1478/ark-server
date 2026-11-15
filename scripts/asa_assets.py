"""ASA server files from Steam (app 2430930, anonymous; the depot is Windows-only).

The Astraeos map ships inside these files, so no MOD download is involved.
Files are staged, checked and then copied; production is never written in place.
"""
from pathlib import Path
import re

from . import ark_game as game
from .compose_assets import steam

MANIFEST = Path('steamapps/appmanifest_2430930.acf')
# Community images remove it: under Proton it makes the server hang or crash.
STEAM_CLIENT = Path('ShooterGame/Binaries/Win64/steamclient64.dll')


def build_id(server):
    match = re.search(r'"buildid"\s+"(\d+)"', (Path(server) / MANIFEST).read_text(errors='replace'))
    if match is None:
        raise ValueError('ASA build id is unavailable.')
    return match.group(1)


def latest_build(steam_root, log):
    text = steam(steam_root, ['login anonymous', 'app_info_update 1', f'app_info_print {game.APP_ID}'], log)
    match = re.search(r'"branches"\s*\{\s*"public"\s*\{[^}]*?"buildid"\s+"(\d+)"', text)
    if match is None:
        raise ValueError('Latest ASA build id is unavailable.')
    return match.group(1)


def download(stage, steam_root):
    """Install or update the server into stage/server and return (path, versions)."""
    target = Path(stage) / 'server'
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A fresh SteamCMD may self-update and report "Missing configuration" once.
    for attempt in (1, 2):
        try:
            steam(steam_root, [f'force_install_dir {target}', 'login anonymous', f'app_update {game.APP_ID} validate'],
                  Path(stage) / f'steamcmd-{attempt}.log')
        except ValueError:
            if attempt == 2:
                raise
        if (target / MANIFEST).is_file() and (target / game.BINARY.relative_to('server')).is_file():
            break
    if not (target / game.BINARY.relative_to('server')).is_file():
        raise ValueError('The ASA server binary is missing after download.')
    (target / STEAM_CLIENT).unlink(missing_ok=True)
    return target, {'build_id': build_id(target), 'mods': {}}
