"""Content identity of the public Compose release, independent of image UUIDs."""
import hashlib
import json
from pathlib import Path

from . import ark_shutdown as n

EXTRA = ('compose.yaml', 'LICENSE', 'requirements-discord.lock',
         'deploy/docker/Dockerfile.compose', 'deploy/docker/Dockerfile.compose-bot')


def manifest(root):
    root = Path(root)
    paths = sorted({p.relative_to(root).as_posix() for suffix in ('*.py', '*.gdb') for p in (root / 'scripts').glob(suffix)} | set(EXTRA))
    values = {name: n.digest(root / name) for name in paths}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'schema': 1, 'digest': digest, 'files': values}


if __name__ == '__main__':
    n.save(n.CODE_ROOT / 'release.json', manifest(n.CODE_ROOT))
