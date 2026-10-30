"""Explicit owner-only stopped configuration maintenance, never a Saved rollback."""
from pathlib import Path
import shutil
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_control import exclusive_lock
from .ark_docker import verify_files
from .ark_process import ProcessBackend, CONFIG, TRANSITION, QUALIFICATION, LAUNCH, descriptor
from .compose_setup import INPUT, ACCEPTED, input_digest, configure, validate, authentication

MARKER=Path('config/compose-config.pending.json')
MANAGED=(Path('config/server.json'),Path('config/compose.json'),Path('config/discord-request.json'),Path('config/installation.json'),ACCEPTED)
INI=(c.INI_PATH,c.GAME_INI_PATH)


def check_non_ini(root, original):
    current=n.inventory(root/n.SAVED)
    ini={str(path.relative_to(n.SAVED)) for path in INI}
    wanted={key:value for key,value in original.items() if key not in ini}
    actual={key:value for key,value in current.items() if key not in ini}
    if set(wanted)!=set(actual) or any(actual[key]['bytes']!=value['bytes'] or n.digest(root/n.SAVED/key)!=value['sha256'] for key,value in wanted.items()):
        raise ValueError('Non-INI progress changed. Configuration recovery must not restore Saved.')


def update(root=n.CODE_ROOT):
    root=Path(root)
    from .compose_server import PENDING
    if (root/PENDING).exists() or (root/MARKER).exists():raise ValueError('A pending maintenance operation requires recovery.')
    if not (root/ACCEPTED).exists():
        raise ValueError('First verify setup with the currently accepted bootstrap, before editing it.')
    installation=n.private_json(root/'config/installation.json')
    preserved=authentication(root/n.SAVED) if installation.get('authentication_policy')=='preserved_import' else None
    value=validate(n.private_json(INPUT,16384),preserved_authentication=preserved)
    # Missing new MODs fail BEFORE any configuration write. Their installation
    # is a separate staged asset operation; this tool never executes installers.
    c.plan_settings(root,value['multipliers']|value.get('options',{})|{'active_mods':value['active_mods']})
    with exclusive_lock(root/'.controller.lock'),exclusive_lock(root/'.control.lock'),c.server_lock(root):
        backend=ProcessBackend(root)
        try:
            state=backend.state();backend.shutdown_guard().verify_startable(state)
            runtime=descriptor(root);policy=n.policy(root)
            job=uuid.uuid4().hex;folder=root/'backups'/('compose-config-'+job)
            before=n.capture(root/n.SAVED,folder/'before')
            shutil.copytree(root/'config',folder/'config-before')
            if (root/QUALIFICATION).exists():shutil.copy2(root/QUALIFICATION,folder/'qualification-before.json')
            marker={'schema':1,'job':job,'snapshot':str(folder.relative_to(root)),'previous_state':state,
                    'runtime':runtime,'native_policy':policy,'files':before}
            n.save(root/MARKER,marker)
            # configure uses its own server lock; no game can start while the
            # controller lifetime lock is held and the pending marker exists.
        finally:backend.popen.close()
    # Reacquire only the controller/control lifetime locks; patch_settings
    # deliberately takes the separate lifetime server lock itself.
    with exclusive_lock(root/'.controller.lock'),exclusive_lock(root/'.control.lock'):
        configure(root,value)
        c.launch_url(root)
        check_non_ini(root,before)
        after=n.capture(root/n.SAVED,folder/'after')
        n.save(root/TRANSITION,{'schema':1,'purpose':'owner_configuration','runtime':runtime,'native_policy':policy,
            'previous_runtime':runtime,'previous_native_policy':policy,'explicit_owner_confirmation':True,
            'configuration':n.configuration(root),'previous_state':state,'files':after,'snapshot':marker['snapshot']})
        # Preserve the old qualification in the private snapshot, then require
        # a real private save/exit again before public startup with new INIs.
        n.save(root/QUALIFICATION,{'schema':1,'invalidated_by_owner_configuration':job})
        installation=n.private_json(root/'config/installation.json');installation['input_sha256']=input_digest(value)
        n.save(root/'config/installation.json',installation)
        n.save(folder/'result.json',{'phase':'completed','explicit_owner_confirmation':True,'saved_world_restored':False})
        (root/MARKER).unlink();c.sync_directory((root/MARKER).parent)
    return {'changed':True,'snapshot':marker['snapshot'],'game_started':False,'saved_world_restored':False}


def restore(root=n.CODE_ROOT):
    root=Path(root)
    marker=n.private_json(root/MARKER)
    path=Path(marker['snapshot'])
    import re
    if not re.fullmatch('backups/compose-config-[0-9a-f]{32}',str(path)):
        raise ValueError('Invalid configuration preservation.')
    folder=root/path
    with exclusive_lock(root/'.controller.lock'),exclusive_lock(root/'.control.lock'),c.server_lock(root):
        if descriptor(root)!=marker['runtime'] or n.policy(root)!=marker['native_policy']:
            raise ValueError('Runtime changed. Do not restore older configuration automatically.')
        if (root/LAUNCH).exists() and n.private_json(root/LAUNCH).get('invocation_id')!=marker['previous_state']['InvocationID']:
            raise ValueError('A later invocation exists. Recovery needs a separate progress review.')
        check_non_ini(root,marker['files'])
        attempt=folder/('recovery-'+uuid.uuid4().hex)
        n.capture(root/n.SAVED,attempt/'preserved-current')
        shutil.copytree(root/'config',attempt/'config-current')
        for path in MANAGED:
            source=folder/'config-before'/path.relative_to('config')
            if source.exists():c.atomic_write(root/path,source.read_bytes())
            elif (root/path).exists():(root/path).unlink()
        for path in INI:
            source=folder/'before/Saved'/path.relative_to(n.SAVED)
            if source.exists():c.atomic_write(root/path,source.read_bytes())
            elif (root/path).exists():(root/path).unlink()
        # Remove only this unconsumed owner's transition, never an update or
        # later launch. Its previous contents were preserved above.
        transition=root/TRANSITION
        previous_transition=folder/'config-before'/TRANSITION.relative_to('config')
        if previous_transition.exists():c.atomic_write(transition,previous_transition.read_bytes())
        elif transition.exists():
            value=n.private_json(transition)
            if value.get('purpose')!='owner_configuration' or value.get('snapshot')!=marker['snapshot']:
                raise ValueError('A different transition exists; do not erase it.')
            transition.unlink()
        qualified=folder/'qualification-before.json'
        if qualified.exists():c.atomic_write(root/QUALIFICATION,qualified.read_bytes())
        verify_files(root,marker['files'])
        n.save(attempt/'result.json',{'phase':'configuration_restored','saved_world_restored':False,'explicit_owner_confirmation':True})
        (root/MARKER).unlink();c.sync_directory((root/MARKER).parent)
    return {'configuration_restored':True,'game_started':False,'saved_world_restored':False}
