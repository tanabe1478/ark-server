"""SteamCMD-only job; staging/cache mounts only, no Saved, token or engine."""
import json
import os
from pathlib import Path
import sys

from . import ark_shutdown as n
from .compose_assets import check, download


def main():
    os.umask(0o077)
    from .compose_server import rootless
    rootless()
    plan = n.private_json(Path('/job/plan.json'))
    stage, steam = Path('/candidate'), Path('/steam')
    if set(plan) == {'operation', 'active_mods'} and plan['operation'] == 'check':
        n.save(stage / 'versions.json', check(steam, plan['active_mods']))
    elif set(plan) == {'operation','install'} and plan['operation'] == 'download':
        download(stage, steam, plan['install'], source=Path('/source'))
    else:
        raise ValueError('Invalid isolated installer plan.')
    print('Isolated Steam job completed; no production writes.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Isolated Steam job failed; production is unchanged.', file=sys.stderr)
        raise SystemExit(78)
