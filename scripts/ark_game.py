"""Which ARK this deployment runs: ASE (native Linux) or ASA (Windows server under Proton).

Selected once per container by ARK_GAME; the default stays ASE so existing
deployments, tests and recovery paths keep their exact behaviour.
"""
import os
from pathlib import Path

GAME = os.environ.get('ARK_GAME', 'ase')
if GAME not in ('ase', 'asa'):
    raise ValueError('ARK_GAME must be ase or asa.')
ASA = GAME == 'asa'

APP_ID = 2430930 if ASA else 376030
CONFIG_DIR = Path('server/ShooterGame/Saved/Config') / ('WindowsServer' if ASA else 'LinuxServer')
BINARY = Path('server/ShooterGame/Binaries') / ('Win64/ArkAscendedServer.exe' if ASA else 'Linux/ShooterGameServer')
DEFAULT_MAP = 'Astraeos_WP' if ASA else 'Genesis'

# ASA RCON stays inside the server container: compose never publishes it.
RCON_PORT = 27020
PROTON = Path('/opt/proton')
# Besides Saved, ASA writes here at runtime (CurseForge user data, crash
# reporter, engine config). compose.asa.yaml mounts each as its own volume.
RUNTIME_WRITABLE = (Path('server/ShooterGame/Binaries/Win64/ShooterGame'),
                    Path('server/ShooterGame/.sentry-native'), Path('server/Engine/Saved'))
COMPAT = Path('compat')
# Inside Saved, ASA keeps writing these while running (logs on events, the
# ServerMetrics heartbeat every minute). They are copied but are not progress,
# so a change during a running-game capture must not abort a guarded stop.
VOLATILE_SAVED = ('Logs/', 'ServerMetrics/') if ASA else ()
# ASA maps verified to start from app 2430930 without a MOD. ASA packs maps
# into IoStore containers, so presence cannot be checked from loose .umap files.
ASA_MAPS = ('Astraeos_WP',)


def world(map_name):
    """Primary world file inside Saved, relative to it."""
    return Path('SavedArks') / (f'{map_name}/{map_name}.ark' if ASA else f'{map_name}.ark')
