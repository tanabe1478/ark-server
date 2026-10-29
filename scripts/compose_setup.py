"""Idempotent first-install service. Existing Saved is never reinitialized.

The first `compose up` runs this isolated service before server/Bot. Subsequent
starts only verify the existing installation. Interrupted installs need owner
review; they are never silently retried over possible progress.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_bot_config import BotConfig
from .ark_process import bootstrap, descriptor, BOOTSTRAP, STATE, LAUNCH
from .compose_assets import check, download, copy_assets, vdf

INPUT = Path('/run/bootstrap/bootstrap.json')
ACCEPTED=Path('config/bootstrap.accepted.json')


def input_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def authentication(saved):
    path = Path(saved) / c.INI_PATH.relative_to(n.SAVED)
    if not path.exists():return None
    text = c.decode_ini(path.read_bytes())[0]
    c.validate_ini_credentials(text)
    parsed = c.parse_ini(text)
    return tuple(c._ini_value(parsed,'ServerSettings',key,'') for key in ('ServerPassword','ServerAdminPassword'))


def validate(value, *, preserved_authentication=None):
    expected = {'settings', 'password', 'admin_password', 'active_mods', 'multipliers',
                'discord', 'auto_start', 'auto_stop', 'updates'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Bootstrap must contain exactly the documented fields.')
    c.validate_settings(value['settings'])
    if value['settings']['game_port']!=7777 or value['settings']['query_port']!=27015:
        raise ValueError('Compose internal ports are fixed; change published host ports instead.')
    preserving = preserved_authentication is not None and (value['password'],value['admin_password']) == preserved_authentication
    for name in ('password', 'admin_password'):
        item = value[name]
        import re
        valid = isinstance(item,str) and (bool(re.fullmatch('[A-Za-z0-9_-]{1,128}',item)) if preserving else re_password(item) and not item.upper().startswith(('CHANGE','REPLACE')))
        if not valid:
            raise ValueError('Set two separate private game passwords before initial setup.')
    if value['password'] == value['admin_password']:
        raise ValueError('Participant and administrator passwords must differ.')
    if not isinstance(value['multipliers'], dict) or set(value['multipliers']) - set(c.RULES):
        raise ValueError('Invalid multiplier fields.')
    edits = value['settings'] | {k: r['default'] for k, r in c.RULES.items()} | value['multipliers'] | {'active_mods': value['active_mods']}
    c.validate_editable_settings(edits)
    if set(value['discord']) != {'guild_id', 'notification_channel_id'}:
        raise ValueError('Invalid Discord scope.')
    bot = BotConfig(**value['discord'])
    for name in ('auto_start', 'auto_stop'):
        if type(value[name]) is not bool:
            raise ValueError('Explicit booleans are required.')
    if value['auto_stop'] and bot.notification_channel_id is None:
        raise ValueError('Automatic stopping requires a configured notification channel.')
    updates = value['updates']
    if (not isinstance(updates, dict) or set(updates) != {'game', 'mods', 'images', 'interval_seconds', 'minimum_free_memory_bytes'}
            or any(type(updates[k]) is not bool for k in ('game', 'mods', 'images'))
            or type(updates['interval_seconds']) is not int or not 300 <= updates['interval_seconds'] <= 86400
            or type(updates['minimum_free_memory_bytes']) is not int or updates['minimum_free_memory_bytes'] < 1024**3):
        raise ValueError('Invalid automatic update configuration.')
    return value


def re_password(value):
    import re
    return re.fullmatch('[A-Za-z0-9_-]{12,128}', value) is not None


def configure(root, value):
    preserving = authentication(root/n.SAVED) == (value['password'],value['admin_password'])
    c.apply_config(root, {k: value[k] for k in ('settings', 'password', 'admin_password')},
                   preserve_existing_authentication=preserving)
    changes = value['multipliers'] | {'active_mods': value['active_mods']}
    plan = c.plan_settings(root, changes)
    c.patch_settings(root, changes, plan['revision'])
    u = value['updates']
    n.save(root / 'config/compose.json', {'schema': 1, 'auto_start': value['auto_start'], 'auto_stop': value['auto_stop'],
        'update_interval_seconds': u['interval_seconds'], 'update_game': u['game'], 'update_mods': u['mods'],
        'update_images': u['images'], 'source_repository': 'tanabe1478/ark-server',
        'minimum_free_memory_bytes': u['minimum_free_memory_bytes']})
    n.save(root / 'config/discord-request.json', asdict(BotConfig(**value['discord'])))
    n.save(root/ACCEPTED,value)


def initialize(root=n.CODE_ROOT, imported=False):
    root = Path(root)
    for directory in ('config', 'logs', 'backups'):
        (root / directory).mkdir(mode=0o700, exist_ok=True)
    original_auth = None
    if (root/BOOTSTRAP).exists():
        installed = n.private_json(root/'config/installation.json')
        if installed.get('authentication_policy') == 'preserved_import':original_auth = authentication(root/n.SAVED)
    elif imported:
        original_auth = authentication(Path('/import/Saved'))
    value = validate(n.private_json(INPUT,16384),preserved_authentication=original_auth)
    input_hash = input_digest(value)
    if (root/'config/compose-config.pending.json').exists():
        raise ValueError('Interrupted owner configuration requires explicit recovery.')
    if (root / BOOTSTRAP).exists():
        record = n.private_json(root / 'config/installation.json')
        descriptor(root)
        if record.get('input_sha256') not in (input_hash,n.digest(INPUT)):
            raise ValueError('Bootstrap input changed. Use explicit stopped configuration maintenance, not reinitialization.')
        if not (root/ACCEPTED).exists():
            n.save(root/ACCEPTED,value)
            record['input_sha256']=input_hash;n.save(root/'config/installation.json',record)
        return {'initialized': True, 'changed': False, 'saved_overwritten': False}
    marker = root / 'config/setup.pending.json'
    if marker.exists() or (root / LAUNCH).exists() or (root / STATE).exists():
        raise ValueError('Interrupted or existing installation requires explicit recovery.')
    saved = root / n.SAVED
    if saved.exists() and any(saved.iterdir()):
        raise ValueError('Existing Saved must be imported explicitly into a fresh installation.')
    job = uuid.uuid4().hex
    n.save(marker, {'schema': 1, 'job': job, 'purpose': 'incomplete_initial_installation'})
    server = root / 'server'
    if imported:
        source = Path('/import/server')
        copy_assets(source, server)
        incoming = Path('/import/Saved')
        if not incoming.is_dir():
            raise ValueError('An explicit Saved copy is required for import.')
        before = n.inventory(incoming)
        shutil.copytree(incoming, saved, dirs_exist_ok=True)
        if before != n.inventory(incoming):
            raise ValueError('Import source changed while copying.')
        for path in saved.rglob('*'):
            if path.is_file():
                path.chmod(0o600)
        versions = {'build_id': None, 'mods': {}}
        manifest = server / 'steamapps/appmanifest_376030.acf'
        if manifest.exists():
            versions['build_id'] = vdf(manifest.read_text())['AppState']['buildid']
    else:
        steam = Path('/steam')
        versions = check(steam, value['active_mods'])
        stage = Path('/stage/initial-' + job)
        stage.mkdir(mode=0o700, parents=True)
        downloaded = download(stage, steam, {'schema': 1, 'game': True,
            'mods': value['active_mods'], 'versions': versions})
        copy_assets(downloaded, server)
    configure(root, value)
    c.launch_url(root)
    runtime = bootstrap(root)
    n.save(root / 'config/assets.json', {'schema': 1, 'versions': versions,
                                       'active_mods': value['active_mods'], 'source': 'explicit_import' if imported else 'official_steam'})
    n.save(root / 'config/installation.json', {'schema': 1, 'input_sha256': input_hash, 'job': job,
        'authentication_policy':'preserved_import' if original_auth == (value['password'],value['admin_password']) else 'new_password_policy'})
    n.save(root / 'logs/setup' / (job + '.json'), {'phase': 'completed', 'runtime': runtime,
                                               'imported': imported, 'in_game_content_verified': False})
    marker.unlink()
    c.sync_directory(marker.parent)
    return {'initialized': True, 'changed': True, 'game_started': False, 'saved_overwritten': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--import-data', action='store_true')
    mode.add_argument('--configure-existing', action='store_true')
    mode.add_argument('--restore-configuration', action='store_true')
    mode.add_argument('--switch-map', metavar='MAP')
    parser.add_argument('--data', choices=('backup', 'delete'), help='With --switch-map: preserve or delete the previous world')
    parser.add_argument('--confirm',action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        from .compose_server import rootless
        rootless()
        if args.switch_map:
            if not args.confirm or not args.data:raise ValueError('Explicit owner confirmation and --data are required.')
            from . import compose_map
            result=compose_map.switch(args.switch_map,args.data)
        elif args.configure_existing or args.restore_configuration:
            if not args.confirm:raise ValueError('Explicit owner confirmation is required.')
            from . import compose_configure
            result=compose_configure.restore() if args.restore_configuration else compose_configure.update()
        else:
            result=initialize(imported=args.import_data)
        print(json.dumps(result))
        return 0
    except Exception as error:
        if args.switch_map and type(error) is ValueError:
            # Map switch checks raise fixed messages without paths or secrets.
            print('Map switch refused: '+str(error), file=sys.stderr)
        print('Compose setup failed or is incomplete. Inspect private volumes; never delete progress to retry.', file=sys.stderr)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
