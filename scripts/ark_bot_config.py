"""Private local configuration. Tokens never enter arguments, responses or logs."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import stat

from .ark_config import ROOT, atomic_write
from .discord_adapter import snowflake

CONFIG_PATH = Path('config/discord-bot.json')
TOKEN_PATH = Path('config/discord-bot.token')


class BotConfigError(ValueError):
    pass


def private_read(path, limit=8192):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
                raise BotConfigError('A private, owner-only regular configuration file is required.')
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise BotConfigError('Configuration exceeds the supported size.')
            return value
    except (OSError, ValueError) as error:
        if isinstance(error, BotConfigError):
            raise
        raise BotConfigError('Private configuration is unavailable or invalid.') from None


def private_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BotConfigError('Duplicate configuration key.')
            result[key] = value
        return result
    try:
        result = json.loads(private_read(path), object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise BotConfigError('Configuration must be an object.')
        return result
    except (ValueError, RecursionError):
        raise BotConfigError('Configuration is invalid; input values are not displayed.') from None


@dataclass(frozen=True)
class BotConfig:
    guild_id: str | None = None
    notification_channel_id: str | None = None
    auto_stop_enabled: bool = False
    shutdown_verified: bool = False
    startup_grace_seconds: int = 3600
    readiness_timeout_seconds: int = 600

    def __post_init__(self):
        for value in (self.guild_id, self.notification_channel_id):
            if value is not None and not snowflake(value):
                raise BotConfigError('Discord IDs must be valid numeric IDs or null.')
        if type(self.auto_stop_enabled) is not bool or type(self.shutdown_verified) is not bool:
            raise BotConfigError('Safety switches must be explicit booleans.')
        if type(self.startup_grace_seconds) is not int or not 300 <= self.startup_grace_seconds <= 86400:
            raise BotConfigError('Startup grace must be between 300 and 86400 seconds.')
        if type(self.readiness_timeout_seconds) is not int or not 30 <= self.readiness_timeout_seconds <= 600:
            raise BotConfigError('Readiness timeout must be between 30 and 600 seconds.')
        if self.auto_stop_enabled and (not self.shutdown_verified or self.notification_channel_id is None):
            raise BotConfigError('Auto-stop requires verified shutdown and a notification channel.')

    @classmethod
    def load(cls, root=ROOT):
        values = private_json(Path(root) / CONFIG_PATH)
        if set(values) != set(asdict(cls())):
            raise BotConfigError('Configuration must contain exactly the documented keys.')
        try:
            return cls(**values)
        except (TypeError, ValueError):
            raise BotConfigError('Configuration is invalid; input values are not displayed.') from None

    def require_guild(self):
        if self.guild_id is None:
            raise BotConfigError('Set the Discord guild ID before starting the bot.')
        return self.guild_id

    def save(self, root=ROOT):
        root = Path(root)
        (root / 'config').mkdir(mode=0o700, exist_ok=True)
        atomic_write(root / CONFIG_PATH, (json.dumps(asdict(self), indent=2) + '\n').encode())


def load_token(root=ROOT, *, path=None):
    try:
        value = private_read(Path(path) if path is not None else Path(root) / TOKEN_PATH, limit=1024).decode('ascii').strip()
    except (UnicodeError, BotConfigError):
        raise BotConfigError('The private Discord token file is unavailable or invalid.') from None
    if re.fullmatch(r'[A-Za-z0-9_.-]{20,256}', value) is None:
        raise BotConfigError('The Discord token is invalid; its value is never displayed.')
    return value
