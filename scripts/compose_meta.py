"""Read immutable public image identity in a data-free helper container."""
from pathlib import Path
from . import ark_shutdown as n
from .ark_process import build_identity, contract

if __name__ == '__main__':
    result = {'release': n.private_json(n.CODE_ROOT / 'release.json')}
    if (n.CODE_ROOT / 'build-identity.json').exists():
        result.update(image_build=build_identity(),source_contract=contract())
    n.save(Path('/result/image.json'),result)
