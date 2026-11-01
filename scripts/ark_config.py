#!/usr/bin/env python3
"""Manage only our ARK INI keys; keep runtime settings separate from secrets."""
import argparse
import configparser
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "map": "Genesis",
    "session_name": "Genesis-Private",
    "max_players": 13,
    "game_port": 7777,
    "query_port": 27015,
}
INI_PATH = Path("server/ShooterGame/Saved/Config/LinuxServer/GameUserSettings.ini")
SETTINGS_PATH = Path("config/server.json")
GAME_INI_PATH = INI_PATH.with_name("Game.ini")
GAME_USER_SETTINGS_SECTION = "/Script/ShooterGame.ShooterGameUserSettings"
GAME_USER_SETTINGS_VERSION = "5"
# Fixed ASE keys, not OS environment variables or arbitrary INI editing.
RULES = {
    "taming_speed": {"label": "テイム速度", "ini_key": "TamingSpeedMultiplier", "file": "GameUserSettings.ini", "section": "ServerSettings", "min": 0.1, "max": 100, "default": 1.0, "hint": "大きいほどテイムが速くなります"},
    "harvest_amount": {"label": "採取量", "ini_key": "HarvestAmountMultiplier", "file": "GameUserSettings.ini", "section": "ServerSettings", "min": 0.1, "max": 100, "default": 1.0, "hint": "1回あたりに得られる資源の倍率"},
    "xp_multiplier": {"label": "経験値", "ini_key": "XPMultiplier", "file": "GameUserSettings.ini", "section": "ServerSettings", "min": 0.1, "max": 100, "default": 1.0, "hint": "獲得経験値の倍率"},
    "day_cycle_speed": {"label": "昼夜サイクル", "ini_key": "DayCycleSpeedScale", "file": "GameUserSettings.ini", "section": "ServerSettings", "min": 0.1, "max": 10, "default": 1.0, "hint": "大きいほど1日が短くなります"},
    "mating_interval": {"label": "交配の待ち時間", "ini_key": "MatingIntervalMultiplier", "file": "Game.ini", "section": "/script/shootergame.shootergamemode", "min": 0.01, "max": 100, "default": 1.0, "hint": "小さいほど次の交配までの待ち時間が短くなります"},
    "egg_hatch_speed": {"label": "孵化速度", "ini_key": "EggHatchSpeedMultiplier", "file": "Game.ini", "section": "/script/shootergame.shootergamemode", "min": 0.1, "max": 100, "default": 1.0, "hint": "大きいほど卵が速く孵ります"},
    "baby_mature_speed": {"label": "成長速度", "ini_key": "BabyMatureSpeedMultiplier", "file": "Game.ini", "section": "/script/shootergame.shootergamemode", "min": 0.1, "max": 1000, "default": 1.0, "hint": "大きいほど子どもが速く成長します。刷り込み間隔は別設定です"},
    "baby_cuddle_interval": {"label": "刷り込みの間隔", "ini_key": "BabyCuddleIntervalMultiplier", "file": "Game.ini", "section": "/script/shootergame.shootergamemode", "min": 0.0001, "max": 100, "default": 1.0, "hint": "小さいほど刷り込みの世話を頻繁に求められます（1.0で8時間ごと）"},
    "baby_imprint_amount": {"label": "刷り込み量", "ini_key": "BabyImprintAmountMultiplier", "file": "Game.ini", "section": "/script/shootergame.shootergamemode", "min": 0.1, "max": 100, "default": 1.0, "hint": "1回の世話で増える刷り込み率の倍率。100%を超える分は切り捨てられます"},
}
# Fixed ASE on/off keys, written as True/False.
TOGGLES = {
    "show_map_player_location": {"label": "地図に自分の位置を表示", "ini_key": "ShowMapPlayerLocation", "file": "GameUserSettings.ini", "section": "ServerSettings", "default": False, "hint": "オンにすると地図に自分の位置と向きが出ます"},
}
EDITABLE_SETTINGS = frozenset(("session_name", "max_players", "active_mods", *RULES, *TOGGLES))
MOD_ID = r"[1-9][0-9]{0,19}"
SNAPSHOT_ID = r"config-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}"


class ConfigError(ValueError):
    """Errors whose messages are safe to print without exposing credentials."""


class StaleConfigError(ConfigError):
    """The preview no longer describes the files on disk."""


class ConfigRecoveryError(ConfigError):
    """A failed write could not be rolled back completely."""


class ModFilesMissingError(ConfigError):
    """ActiveMods refers to content that is not installed locally."""


class ConfigBusyError(ConfigError):
    """A game process or maintenance task holds the server lock."""


def lock_path(path):
    """Compose shares a locks volume; legacy installations keep their paths."""
    path = Path(path)
    directory = os.environ.get("ARK_LOCK_DIRECTORY")
    if directory and path.parent.resolve() == ROOT.resolve() and path.name in (
        ".server.lock", ".control.lock", ".backup.lock", ".automation.lock", ".discord.lock", ".controller.lock",
    ):
        return Path(directory) / path.name
    return path


@contextmanager
def server_lock(root):
    with lock_path(root / ".server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigBusyError("Stop the server and maintenance tasks before applying configuration.") from None
        yield


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        raise ConfigError("Runtime settings must contain exactly the documented keys.")
    if not isinstance(settings["map"], str) or not re.fullmatch(r"[A-Za-z0-9_]+", settings["map"]):
        raise ConfigError("Map must contain only letters, digits and underscores.")
    name = settings["session_name"]
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not name.strip():
        raise ConfigError("Session name must be between 1 and 64 characters.")
    if any(not c.isprintable() or c in "?=&" for c in name):
        raise ConfigError("Session name contains an unsupported character.")
    for key, low, high in (("max_players", 1, 255), ("game_port", 1024, 65534), ("query_port", 1024, 65535)):
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ConfigError(f"{key} is outside the supported integer range.")
    if settings["query_port"] in (settings["game_port"], settings["game_port"] + 1):
        raise ConfigError("Query port must not overlap either game port.")
    return settings


def load_settings(root=ROOT):
    path = root / SETTINGS_PATH
    # Compatibility with the existing manually installed server.
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else DEFAULTS.copy()
    return validate_settings(settings)


def decode_ini(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    return data.decode(encoding), encoding


def parse_ini(text):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        config.read_string(text)
    except configparser.Error:
        # ConfigParser's original error can include a line containing a secret.
        raise ConfigError("GameUserSettings.ini is not a supported INI file.") from None
    return config


def merge_ini(text, managed):
    """Preserve comments, unknown settings and repeated Unreal array keys."""
    parse_ini(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    wanted = {section.casefold(): (section, values) for section, values in managed.items()}
    output = []
    section = None
    seen_keys = set()
    seen_sections = set()

    def append_missing():
        if section in wanted:
            for key, value in wanted[section][1].items():
                if key.casefold() not in seen_keys:
                    output.append(f"{key}={value}")

    for line in text.splitlines():
        header = re.fullmatch(r"\s*\[([^\]]+)\]\s*(?:[;#].*)?", line)
        if header:
            append_missing()
            section = header[1].casefold()
            seen_sections.add(section)
            seen_keys = set()
        elif section in wanted:
            match = re.match(r"\s*([^;#=:\s][^=:]*?)\s*[=:]", line)
            if match:
                key = match[1].strip().casefold()
                for desired_key, value in wanted[section][1].items():
                    if key == desired_key.casefold():
                        line = f"{desired_key}={value}"
                        seen_keys.add(key)
                        break
        output.append(line)
    append_missing()
    for folded, (name, values) in wanted.items():
        if folded not in seen_sections:
            output.extend(["", f"[{name}]"])
            output.extend(f"{key}={value}" for key, value in values.items())
    return newline.join(output).lstrip("\r\n") + newline


def validate_ini_credentials(text):
    config = parse_ini(text)
    sections = [s for s in config.sections() if s.casefold() == "serversettings"]
    if not sections:
        raise ConfigError("Refusing to start: private-server credentials are missing.")
    for section in sections:
        password = config.get(section, "ServerPassword", fallback="")
        admin = config.get(section, "ServerAdminPassword", fallback="")
        if not password or not admin or password == admin:
            raise ConfigError("Refusing to start: missing or unsafe private-server credentials.")
        try:
            rcon = config.getboolean(section, "RCONEnabled", fallback=False)
        except ValueError:
            raise ConfigError("Refusing to start: invalid RCON setting.") from None
        if rcon:
            raise ConfigError("Refusing to start: RCON is enabled unexpectedly.")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Write privately and atomically; report permission-only changes too."""
    changed = not path.exists() or path.read_bytes() != data
    permissions_changed = path.exists() and (path.stat().st_mode & 0o777) != 0o600
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if changed or permissions_changed:
        path.chmod(0o600)
    return changed or permissions_changed


def apply_config(root, payload, *, preserve_existing_authentication=False):
    if not isinstance(payload, dict) or set(payload) != {"settings", "password", "admin_password"}:
        raise ConfigError("Expected settings and two separate passwords on standard input.")
    settings = validate_settings(payload["settings"])
    if type(preserve_existing_authentication) is not bool:
        raise ConfigError("Authentication preservation must be explicit.")
    pattern = r"[A-Za-z0-9_-]{1,128}" if preserve_existing_authentication else r"[A-Za-z0-9_-]{12,128}"
    for name in ("password", "admin_password"):
        value = payload[name]
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ConfigError("New passwords must be 12-128 ASCII letters, digits, underscores or hyphens.")
    if payload["password"] == payload["admin_password"]:
        raise ConfigError("Participant and administrator passwords must be different.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with server_lock(root):
        path = root / INI_PATH
        text, encoding = decode_ini(path.read_bytes()) if path.exists() else ("", "utf-8")
        if preserve_existing_authentication:
            validate_ini_credentials(text)
            parsed = parse_ini(text)
            current = tuple(_ini_value(parsed, "ServerSettings", key, "") for key in ("ServerPassword", "ServerAdminPassword"))
            if current != (payload["password"], payload["admin_password"]):
                raise ConfigError("Preserved authentication must exactly match the existing INI.")
        updated = merge_ini(text, {
            "ServerSettings": {
                "ServerPassword": payload["password"],
                "ServerAdminPassword": payload["admin_password"],
                "RCONEnabled": "False",
            },
            "SessionSettings": {"SessionName": settings["session_name"]},
            "/Script/Engine.GameSession": {"MaxPlayers": str(settings["max_players"])},
            GAME_USER_SETTINGS_SECTION: {"Version": GAME_USER_SETTINGS_VERSION},
        })
        validate_ini_credentials(updated)
        changed = atomic_write(path, updated.encode(encoding))
        runtime = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return atomic_write(root / SETTINGS_PATH, runtime) or changed


def settings_revision(ini, runtime, game=None):
    digest = hashlib.sha256()
    for data in (ini, runtime, game):
        digest.update(b"missing\x00" if data is None else str(len(data)).encode() + b"\x00" + data)
    return digest.hexdigest()


def validate_editable_settings(settings):
    expected = set(DEFAULTS) | set(RULES) | set(TOGGLES) | {"active_mods"}
    if not isinstance(settings, dict) or set(settings) != expected:
        raise ConfigError("Invalid editable settings.")
    base = validate_settings({key: settings[key] for key in DEFAULTS})
    for key, rule in RULES.items():
        value = settings[key]
        if type(value) not in (int, float) or not rule["min"] <= value <= rule["max"] or not math.isfinite(value):
            raise ConfigError("A game multiplier is outside its supported range.")
        base[key] = float(value)
    for key in TOGGLES:
        if type(settings[key]) is not bool:
            raise ConfigError("An on/off setting must be true or false.")
        base[key] = settings[key]
    ids = settings["active_mods"]
    if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(item, str) or not re.fullmatch(MOD_ID, item) or int(item) > 2**64-1 for item in ids) or len(set(ids)) != len(ids):
        raise ConfigError("Use up to 32 unique numeric Workshop IDs in load order.")
    return base | {"active_mods": list(ids)}


def _ini_value(parsed, section, key, default):
    values = [parsed.get(name, key) for name in parsed.sections() if name.casefold() == section.casefold() and parsed.has_option(name, key)]
    if len(set(values)) > 1:
        raise ConfigError("Conflicting duplicate managed INI values.")
    return values[0] if values else default


def editable_from_bytes(ini, runtime, game):
    base = validate_settings(json.loads(runtime) if runtime is not None else DEFAULTS.copy())
    text, _ = decode_ini(ini)
    validate_ini_credentials(text)
    parsed = {"GameUserSettings.ini": parse_ini(text), "Game.ini": parse_ini(decode_ini(game)[0] if game is not None else "")}
    for key, rule in RULES.items():
        try:
            base[key] = float(_ini_value(parsed[rule["file"]], rule["section"], rule["ini_key"], str(rule["default"])))
        except (ValueError, OverflowError):
            raise ConfigError("A managed game multiplier is invalid.") from None
    for key, rule in TOGGLES.items():
        value = _ini_value(parsed[rule["file"]], rule["section"], rule["ini_key"], str(rule["default"])).strip().casefold()
        if value not in ("true", "false"):
            raise ConfigError("A managed on/off setting is invalid.")
        base[key] = value == "true"
    mods = _ini_value(parsed["GameUserSettings.ini"], "ServerSettings", "ActiveMods", "").strip()
    base["active_mods"] = [] if mods in ("", "0") else [item.strip() for item in mods.split(",")]
    return validate_editable_settings(base)


def configuration_files(root):
    return tuple((root / path).read_bytes() if (root / path).exists() else None for path in (INI_PATH, SETTINGS_PATH, GAME_INI_PATH))


def load_editable_settings(root=ROOT):
    ini, runtime, game = configuration_files(root)
    if ini is None:
        raise ConfigError("GameUserSettings.ini is missing.")
    return editable_from_bytes(ini, runtime, game)


def mod_inventory(root, active_ids):
    folder = root / "server/ShooterGame/Content/Mods"
    installed_ids = {path.stem for path in folder.glob("*.mod") if re.fullmatch(MOD_ID, path.stem)}
    result = []
    for item in list(active_ids) + sorted(installed_ids - set(active_ids))[:64]:
        metadata, content = folder / (item + ".mod"), folder / item
        present = metadata.is_file() and not metadata.is_symlink() and metadata.stat().st_size > 0 and content.is_dir() and not content.is_symlink() and any(content.iterdir())
        result.append({"id": item, "active": item in active_ids, "content_present": present,
                       "order": active_ids.index(item) + 1 if item in active_ids else None})
    return result


def _settings_plan(root, changes):
    if not isinstance(changes, dict) or not changes or not set(changes) <= EDITABLE_SETTINGS:
        raise ConfigError("Only allowlisted non-secret changes are supported.")
    original_ini, original_runtime, original_game = configuration_files(root)
    if original_ini is None:
        raise ConfigError("GameUserSettings.ini is missing.")
    before = editable_from_bytes(original_ini, original_runtime, original_game)
    after = validate_editable_settings(before | changes)
    text, encoding = decode_ini(original_ini)
    game_text, game_encoding = decode_ini(original_game) if original_game is not None else ("", "utf-8")
    managed = {"SessionSettings": {"SessionName": after["session_name"]},
               "/Script/Engine.GameSession": {"MaxPlayers": str(after["max_players"])}}
    game_managed = {}
    for key in changes:
        if key in RULES:
            rule = RULES[key]
            destination = game_managed if rule["file"] == "Game.ini" else managed
            destination.setdefault(rule["section"], {})[rule["ini_key"]] = format(after[key], ".15g")
        elif key in TOGGLES:
            rule = TOGGLES[key]
            destination = game_managed if rule["file"] == "Game.ini" else managed
            destination.setdefault(rule["section"], {})[rule["ini_key"]] = "True" if after[key] else "False"
        elif key == "active_mods":
            managed.setdefault("ServerSettings", {})["ActiveMods"] = ",".join(after[key])
    def merged(source, fields):
        updated = merge_ini(source, fields)
        parsed = parse_ini(updated)
        for name, values in fields.items():
            for section in parsed.sections():
                if section.casefold() == name.casefold():
                    for key, value in values.items():
                        if parsed.get(section, key) != value:
                            raise ConfigError("Managed INI values could not be merged safely.")
        return updated
    target_ini = merged(text, managed).encode(encoding)
    target_game = merged(game_text, game_managed).encode(game_encoding) if game_managed else original_game
    runtime = {key: after[key] for key in DEFAULTS}
    target_runtime = (json.dumps(runtime, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files = ((INI_PATH, original_ini, target_ini), (SETTINGS_PATH, original_runtime, target_runtime), (GAME_INI_PATH, original_game, target_game))
    preview = {"revision": settings_revision(original_ini, original_runtime, original_game),
               "changed": any(original != target for _, original, target in files),
               "changes": {key: {"before": before[key], "after": after[key]} for key in sorted(changes) if before[key] != after[key]},
               "settings": after, "requires_stopped_server": True, "automatic_restart": False,
               "files": [str(path) for path, original, target in files if original != target]}
    return preview, files


def plan_settings(root, changes):
    """Return only non-secret values; the revision also covers the private INI."""
    return _settings_plan(root, changes)[0]


def patch_settings(root, changes, expected_revision):
    """Offline-only patch with a private snapshot and rollback on write failure."""
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise ConfigError("Apply requires the revision returned by settings.plan.")
    with server_lock(root):
        preview, files = _settings_plan(root, changes)
        if expected_revision != preview["revision"]:
            raise StaleConfigError("Configuration changed; create a new preview before applying.")
        if not preview["changed"]:
            return preview | {"snapshot": None}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot = Path("backups") / f"config-{stamp}-{uuid.uuid4().hex}"
        directory = root / snapshot
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        sync_directory(root)
        sync_directory(directory.parent)
        manifest = {}
        for relative, original, _ in files:
            manifest[str(relative)] = original is not None
            if original is not None:
                atomic_write(directory / relative.name, original)
        atomic_write(directory / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        try:
            for relative, _, target in files:
                if target is not None:
                    atomic_write(root / relative, target)
        except OSError:
            try:
                for relative, original, _ in files:
                    if original is None:
                        (root / relative).unlink(missing_ok=True)
                    else:
                        atomic_write(root / relative, original)
            except OSError:
                raise ConfigRecoveryError("Configuration write and rollback failed; recover from the private config snapshot.") from None
            raise ConfigError("Configuration write failed; the original files were restored.") from None
        return preview | {"revision": settings_revision(*(target for _, _, target in files)), "snapshot": str(snapshot)}


def validate_ini_initialization(text):
    """Fail before execution if Unreal may reset the whole user-settings INI.

    ASE's UGameUserSettings schema version is 5. Credential checks alone do not
    protect a file that the engine considers uninitialized on its first load.
    This validation never edits the INI or treats an active process as ready.
    """
    parsed = parse_ini(text)
    if _ini_value(parsed, GAME_USER_SETTINGS_SECTION, "Version", "") != GAME_USER_SETTINGS_VERSION:
        raise ConfigError("Refusing to start: GameUserSettings.ini initialization version is missing or unsupported.")


def launch_url(root=ROOT):
    pending = root / 'config/migration.pending.json'
    cutover = root / 'config/docker-cutover.pending.json'
    if pending.exists() or pending.is_symlink() or cutover.exists() or cutover.is_symlink():
        raise ConfigRecoveryError('An interrupted migration requires manual recovery before starting.')
    settings = load_editable_settings(root)
    validate_ini_initialization(decode_ini((root / INI_PATH).read_bytes())[0])
    ids = settings['active_mods']
    if any(item["active"] and not item["content_present"] for item in mod_inventory(root, ids)):
        raise ModFilesMissingError("Active MOD content is missing; install it before starting the server.")
    return (
        f"{settings['map']}?SessionName={settings['session_name']}"
        f"?MaxPlayers={settings['max_players']}?Port={settings['game_port']}"
        f"?QueryPort={settings['query_port']}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "launch-url"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "apply":
            print("changed" if apply_config(ROOT, json.load(sys.stdin)) else "ok")
        else:
            print(launch_url())
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError):
        print("ARK configuration could not be read or written; check format and permissions.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
