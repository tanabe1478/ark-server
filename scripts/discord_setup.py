#!/usr/bin/env python3
"""Install only the Discord receiver service, or enter secrets locally without echo."""
import argparse
from dataclasses import replace
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import warnings

from .ark_bot_config import BotConfig, BotConfigError, CONFIG_PATH, TOKEN_PATH, load_token
from .ark_config import ROOT, atomic_write
from .discord_adapter import snowflake


def install(root=ROOT, home=None, *, reload=True):
    root = Path(root).resolve()
    home = Path.home() if home is None else Path(home)
    if re.fullmatch(r'/[A-Za-z0-9_./-]+', str(root)) is None:
        raise BotConfigError('The service directory must be an absolute path without spaces or systemd escapes.')
    python = root / '.venv-discord/bin/python'
    if not python.is_file():
        raise BotConfigError('Create the dedicated Discord virtual environment first.')
    path = root / CONFIG_PATH
    if not path.exists():
        BotConfig().save(root)
    else:
        BotConfig.load(root)  # Never overwrite an existing private policy.
    source = root / 'deploy/discord/ark-discord.service.in'
    unit = source.read_text().replace('@ROOT@', str(root))
    target = home / '.config/systemd/user/ark-discord.service'
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, unit.encode())
    if reload:
        subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
    # No start/enable, ARK operation, token creation or network connection here.
    return target


def configure(root=ROOT):
    if not sys.stdin.isatty():
        raise BotConfigError('Enter credentials in an interactive local terminal, never a pipe or chat.')
    existing = BotConfig.load(root)
    guild = input('DiscordサーバーID（数値）: ').strip()
    channel = input('自動停止の通知先チャンネルID（空欄なら未設定）: ').strip() or None
    if not snowflake(guild) or (channel is not None and not snowflake(channel)):
        raise BotConfigError('Discord IDs must be numeric IDs copied from developer mode.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            token = getpass.getpass('Botトークン（非表示・既存値を使う場合は空欄）: ').strip()
        except getpass.GetPassWarning:
            raise BotConfigError('Secure token input is unavailable; refusing an echoed fallback.') from None
    if token:
        if re.fullmatch(r'[A-Za-z0-9_.-]{20,256}', token) is None:
            raise BotConfigError('Token format is invalid; its value is not displayed.')
    else:
        load_token(root)  # Blank does not silently create an empty credential.
    # Keep auto-stop and restart locked until explicit shutdown/save validation.
    config = replace(existing, guild_id=guild, notification_channel_id=channel,
                     auto_stop_enabled=False, shutdown_verified=False)
    if token:
        atomic_write(Path(root) / TOKEN_PATH, (token + '\n').encode())
    config.save(root)


def check(root=ROOT):
    config = BotConfig.load(root)
    config.require_guild()
    load_token(root)
    print('Private configuration present. No Discord connection or ARK operation was performed.')
    print('Automatic stopping: ' + ('enabled' if config.auto_stop_enabled else 'disabled'))
    print('Restart: ' + ('enabled' if config.shutdown_verified else 'awaiting shutdown/save validation'))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--install', action='store_true')
    mode.add_argument('--configure', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.install:
            print('Installed: ' + str(install()))
            print('Service was NOT started/enabled. ARK was not operated.')
        elif args.configure:
            configure()
            print('Private settings saved. Auto-stop/restart remain disabled pending validation.')
            print('Start the receiver with: systemctl --user enable --now ark-discord.service')
        else:
            check()
    except (BotConfigError, OSError, ValueError, EOFError, subprocess.SubprocessError):
        print('Setup is incomplete or failed. No secret values are displayed. Inspect the local files and IDs.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nCancelled. The ARK service was not operated.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
