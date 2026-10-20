"""Compose Bot entrypoint; live safety flags, file-only token, explicit offline QA."""
import asyncio
import os
from pathlib import Path
import signal
import sys
import time

from . import ark_shutdown as n
from .ark_bot_config import BotConfig, load_token
from .ark_control import exclusive_lock
from .ark_control_ipc import RemoteAutomation

PATH = Path('/run/ark-bot-config/discord-bot.json')


class LiveConfig:
    """Reload safety switches without reconnecting or re-registering commands."""
    def __init__(self, path=PATH):
        self.path = path
        value = self.read()
        self.scope = (value.guild_id, value.notification_channel_id)

    def read(self):
        value = BotConfig(**n.private_json(self.path))
        if 'scope' in self.__dict__ and (value.guild_id, value.notification_channel_id) != self.scope:
            raise ValueError('Changing Discord scope requires an explicit Bot redeployment.')
        return value

    def __getattr__(self, name):
        return getattr(self.read(), name)


def main():
    os.umask(0o077)
    config = LiveConfig()
    with exclusive_lock(n.CODE_ROOT / '.discord.lock'):
        if os.environ.get('ARK_DISCORD_OFFLINE') == '1':
            # No SDK client, token read, DNS, HTTP, Gateway or command sync.
            # This mode is for isolated validation, never evidence of live auth.
            end = False
            def stop(number, frame):
                nonlocal end
                end = True
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            print('Discord OFFLINE validation: no Discord network requests or messages.', flush=True)
            remote = RemoteAutomation(config, '/run/ark-control/control.sock', n.CODE_ROOT)
            while not end:
                result = remote.execute('status')
                n.save(Path('/tmp/offline-bot-health.json'), {'ok': result['ok'], 'monotonic': time.monotonic()})
                time.sleep(2)
            return 0
        from .discord_bot import run
        config.require_guild()
        token = load_token()
        asyncio.run(run(config, token))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Compose Bot configuration or connection failed; private details suppressed.', file=sys.stderr)
        raise SystemExit(78)
