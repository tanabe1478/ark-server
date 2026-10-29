"""Explicit owner-only stopped map switch: a fresh world on another installed map.

The previous world, profiles and tribes (Saved/SavedArks) are either preserved
under backups/compose-map-<id>/ or deleted, as the owner chose. Configuration,
INI, mod data (SaveGames) and logs are kept. Startup then requires the same
private save/exit qualification as any owner configuration change.
"""
from pathlib import Path
import shutil
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_control import exclusive_lock
from .ark_process import ProcessBackend, TRANSITION, QUALIFICATION, descriptor
from .compose_configure import MARKER
from .compose_setup import INPUT, ACCEPTED, input_digest, configure, validate, authentication

# Official ASE maps shipped with app 376030, by launch name.
MAPS = {
    'TheIsland': 'Maps/TheIslandSubMaps/TheIsland.umap',
    'TheCenter': 'Mods/TheCenter/TheCenter.umap',
    'ScorchedEarth_P': 'Maps/ScorchedEarth/ScorchedEarth_P.umap',
    'Ragnarok': 'Mods/Ragnarok/Ragnarok.umap',
    'Aberration_P': 'Maps/Aberration/Aberration_P.umap',
    'Extinction': 'Maps/Extinction/Extinction.umap',
    'Valguero_P': 'Mods/Valguero/Valguero_P.umap',
    'Genesis': 'Maps/Genesis/Genesis.umap',
    'CrystalIsles': 'Mods/CrystalIsles/CrystalIsles.umap',
    'Gen2': 'Maps/Genesis2/Gen2.umap',
    'LostIsland': 'Mods/LostIsland/LostIsland.umap',
    'Fjordur': 'Mods/FjordurOfficial/Fjordur.umap',
    'Aquatica': 'Maps/Abyss/Aquatica.umap',
}
CONTENT = Path('server/ShooterGame/Content')
WORLD = Path('SavedArks')
DATA = ('backup', 'delete')


def installed(root):
    return {name for name, path in MAPS.items()
            if (root / CONTENT / path).is_file() and (root / CONTENT / path).stat().st_size > 0}


def check_request(root, value, accepted, target, data):
    """The edited bootstrap may differ from the accepted one only in settings.map."""
    if data not in DATA:
        raise ValueError('Choose --data backup or --data delete.')
    if target not in MAPS:
        raise ValueError('Unknown map. Use one of: ' + ', '.join(MAPS) + '.')
    if target not in installed(root):
        raise ValueError('The map is not installed in the current game files.')
    current = c.load_settings(root)['map']
    if target == current:
        raise ValueError('The server already uses this map.')
    if value['settings']['map'] != target:
        raise ValueError('Set settings.map in the bootstrap to the requested map first.')
    strip = lambda v: v | {'settings': v['settings'] | {'map': None}}
    if strip(value) != strip(accepted):
        raise ValueError('Only settings.map may change in a map switch; use --configure-existing for other edits.')
    return current


def world_files(saved):
    folder = saved / WORLD
    return sorted(p for p in folder.rglob('*') if p.is_file() or p.is_symlink()) if folder.is_dir() else []


def switch(target, data, root=n.CODE_ROOT):
    root = Path(root)
    from .compose_server import PENDING
    if (root/PENDING).exists() or (root/MARKER).exists():raise ValueError('A pending maintenance operation requires recovery.')
    if not (root/ACCEPTED).exists():
        raise ValueError('First verify setup with the currently accepted bootstrap, before editing it.')
    installation=n.private_json(root/'config/installation.json')
    preserved=authentication(root/n.SAVED) if installation.get('authentication_policy')=='preserved_import' else None
    value=validate(n.private_json(INPUT,16384),preserved_authentication=preserved)
    previous=check_request(root,value,n.private_json(root/ACCEPTED,16384),target,data)
    with exclusive_lock(root/'.controller.lock'),exclusive_lock(root/'.control.lock'),c.server_lock(root):
        backend=ProcessBackend(root)
        try:
            state=backend.state();backend.shutdown_guard().verify_startable(state)
            runtime=descriptor(root);policy=n.policy(root)
            job=uuid.uuid4().hex;folder=root/'backups'/('compose-map-'+job)
            folder.mkdir(mode=0o700,parents=True)
            shutil.copytree(root/'config',folder/'config-before')
            if (root/QUALIFICATION).exists():shutil.copy2(root/QUALIFICATION,folder/'qualification-before.json')
            # Only the backup choice copies progress; delete keeps no world copy by design.
            if data=='backup':n.capture(root/n.SAVED,folder/'before')
            marker={'schema':1,'job':job,'purpose':'map_switch','snapshot':str(folder.relative_to(root)),
                    'previous_state':state,'runtime':runtime,'native_policy':policy,'from':previous,'to':target,'data':data}
            n.save(root/MARKER,marker)
            removed=world_files(root/n.SAVED)
            for path in removed:path.unlink()
            for path in sorted((p for p in (root/n.SAVED/WORLD).rglob('*') if p.is_dir()),reverse=True):path.rmdir()
            c.sync_directory(root/n.SAVED/WORLD) if (root/n.SAVED/WORLD).is_dir() else None
        finally:backend.popen.close()
    with exclusive_lock(root/'.controller.lock'),exclusive_lock(root/'.control.lock'):
        configure(root,value)
        c.launch_url(root)
        if world_files(root/n.SAVED):raise ValueError('World files reappeared during the map switch.')
        after=n.capture(root/n.SAVED,folder/'after')
        n.save(root/TRANSITION,{'schema':1,'purpose':'owner_configuration','runtime':runtime,'native_policy':policy,
            'previous_runtime':runtime,'previous_native_policy':policy,'explicit_owner_confirmation':True,
            'configuration':n.configuration(root),'previous_state':state,'files':after,'snapshot':marker['snapshot'],
            'map_switch':{'from':previous,'to':target,'data':data}})
        n.save(root/QUALIFICATION,{'schema':1,'invalidated_by_owner_configuration':job})
        installation=n.private_json(root/'config/installation.json');installation['input_sha256']=input_digest(value)
        n.save(root/'config/installation.json',installation)
        result={'phase':'completed','explicit_owner_confirmation':True,'from':previous,'to':target,'data':data,
                'world_files_removed':len(removed),'world_backup':marker['snapshot']+'/before' if data=='backup' else None}
        n.save(folder/'result.json',result)
        (root/MARKER).unlink();c.sync_directory((root/MARKER).parent)
    return {'changed':True,'snapshot':marker['snapshot'],'game_started':False,'from':previous,'to':target,'data':data,
            'world_files_removed':len(removed),'world_backup':result['world_backup']}
