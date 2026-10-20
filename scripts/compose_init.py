"""Prepare private Compose files locally. Never connects to Discord or starts ARK."""
import argparse
import getpass
import os
from pathlib import Path
import re
import stat
import sys
import warnings
from . import ark_config as c, ark_shutdown as n
from .ark_bot_config import load_token
from .compose_setup import validate


def prepare(root=n.CODE_ROOT,check=False):
    root=Path(root);folder=root/'config/compose'
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('config/compose must be an owner-only real directory.')
    bootstrap=folder/'bootstrap.json';token=folder/'discord-bot.token'
    if check:
        validate(n.private_json(bootstrap,16384));load_token(path=token)
        return {'configuration_valid':True,'discord_connection_tested':False}
    if not bootstrap.exists():
        c.atomic_write(bootstrap,(root/'examples/compose-bootstrap.example.json').read_bytes())
    if not sys.stdin.isatty():raise ValueError('Use a private interactive terminal for hidden token input.')
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning) # never fall back to echoing stdin
        entered=getpass.getpass('Discord Bot token (hidden; blank reuses existing file): ').strip()
    if entered:
        if not re.fullmatch('[A-Za-z0-9_.-]{20,256}',entered) or entered.upper().startswith(('REPLACE','CHANGE')):
            raise ValueError('Invalid token format; no value was written.')
        c.atomic_write(token,(entered+'\n').encode())
    load_token(path=token)
    return {'private_files_prepared':True,'edit_bootstrap_before_first_start':True,'discord_connection_tested':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    os.umask(0o077)
    try:
        import json
        print(json.dumps(prepare(check=args.check)))
        if not args.check:print('Edit config/compose/bootstrap.json, then run this command with --check. No Discord connection was attempted.')
        return 0
    except Exception:
        print('Private Compose preparation failed. Check file ownership, permissions, placeholders and input format; values are suppressed.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
