"""Data-free native profile discovery before touching the running server."""
from pathlib import Path
import sys
from . import ark_shutdown as n
from .compose_profiles import expected

if __name__ == '__main__':
    try:
        n.save(Path('/result/profile.json'),expected(n.CODE_ROOT,n.contract()))
        print('Native instruction fingerprints and abort/caller/PLT relations matched.')
    except Exception:
        n.save(Path('/result/failure.json'),{'phase':'unsupported_native_layout','production_touched':False})
        print('Unknown native layout; keep the existing production release.',file=sys.stderr)
        raise SystemExit(78)
