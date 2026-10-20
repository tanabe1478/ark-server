"""Apply a qualified candidate while the old controller is stopped.

No Docker socket or writable Saved mount. Interrupted application stays held;
there is no automatic restore of Saved or replay after a new invocation.
"""
import os
from pathlib import Path
import shutil
import sys
import uuid

from . import ark_shutdown as n
from .ark_docker import verify_files
from .ark_process import CONFIG, TRANSITION, LAUNCH, build_identity, contract
from .compose_assets import copy_assets, tree_manifest
from .compose_qualify import digest_value
from .compose_server import PENDING


def asset_files(root):
    return {k:v for k,v in tree_manifest(root).items() if not k.startswith('ShooterGame/Saved/')}


def validate_certificate(certificate, pending, trial=Path('/validation')):
    if (type(certificate.get('schema')) is not int or certificate['schema'] != 1
            or certificate.get('two_cycles_passed') is not True or certificate.get('progress_shape_verified') is not True
            or certificate.get('image_build') != build_identity() or certificate.get('source_contract') != contract()
            or certificate.get('in_game_content_verified') is not False
            or not isinstance(certificate.get('cycles'),list) or len(certificate['cycles']) != 2):
        raise ValueError('Candidate qualification is incomplete.')
    files = {name:{'bytes':meta['bytes'],'sha256':meta['sha256']} for name,meta in pending['files'].items()}
    if (certificate.get('input_saved') != files or certificate.get('input_saved_digest') != digest_value(files)
            or certificate.get('input_configuration') != pending['configuration']):
        raise ValueError('Qualification was not performed on the latest stopped Saved copy.')
    points = certificate['native_policy']['native_points']
    invocations=[];runs=[]
    for cycle in certificate['cycles']:
        value = cycle['receipt']
        state = cycle['state']
        if (type(value.get('schema')) is not int or value['schema'] != 4 or value.get('clean_exit') is not False
                or value.get('warning') != 'expected_native_abort' or value.get('service_record') != state
                or type(state.get('SupervisorReturnCode')) is not int or state['SupervisorReturnCode'] != -6
                or type(state.get('MainPID')) is not int or state['MainPID'] != 0
                or state.get('ActiveState')!='failed' or state.get('Result')!='signal' or state.get('Runtime')!='compose-process'
                or state.get('OOMKilled') is not False or state.get('PrivateBootstrap') is not True
                or value.get('runtime')!=certificate['runtime'] or value.get('native_policy') != certificate['native_policy']):
            raise ValueError('Candidate native exit is not qualified.')
        if value['intent'].get('invocation_id')!=state.get('InvocationID'):
            raise ValueError('Candidate invocation binding changed.')
        invocations.append(state['InvocationID']);runs.append(value['intent']['run_id'])
        relative = Path(value['snapshot'])
        if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 2 or relative.parts[0] != 'backups':
            raise ValueError('Unsafe qualification snapshot.')
        folder = trial / relative
        if n.private_json(folder / 'receipt.json') != value:
            raise ValueError('Candidate receipt changed.')
        before = n.private_json(folder / 'before/manifest.json',8*1024**2)['files']
        after = n.private_json(folder / 'after/manifest.json',8*1024**2)['files']
        if not n.observed_proof(value['observation'],value['intent'],value['world'],before,after,points):
            raise ValueError('Candidate native observation is insufficient.')
        original=files.get(value['world'])
        if original and after[value['world']]['bytes']*10<original['bytes']*9:
            raise ValueError('Candidate world shrank substantially across loading.')
        roster={key for key in files if key.endswith(('.arkprofile','.arktribe'))}
        if not roster<=after.keys() or any(after[key]['bytes']==0 for key in roster):
            raise ValueError('Candidate lost a pre-load profile or tribe.')
    if len(set(invocations))!=2 or len(set(runs))!=2:raise ValueError('Candidate cycles were duplicated.')


def clear_programs(server):
    for path in server.iterdir():
        if path.name == 'ShooterGame' and path.is_dir() and not path.is_symlink():
            for child in path.iterdir():
                if child.name != 'Saved':
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        else:
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()


def apply(root=n.CODE_ROOT):
    root = Path(root)
    pending = n.private_json(root / PENDING)
    candidate = Path('/candidate')
    cert = n.private_json(Path('/result/qualification.json'),8*1024**2)
    if pending.get('phase') not in ('prepared','applying'):
        raise ValueError('A prepared stopped update is required.')
    launch = root / LAUNCH
    if launch.exists() and n.private_json(launch).get('invocation_id') != pending['previous_state']['InvocationID']:
        raise ValueError('A later invocation exists. Never replay application or roll Saved back.')
    verify_files(root,pending['files'])
    if n.configuration(root)!=pending['configuration']:
        raise ValueError('Production configuration changed after preparation.')
    validate_certificate(cert,pending)
    if digest_value(asset_files(candidate / 'server')) != cert['assets_digest']:
        raise ValueError('Candidate assets changed after qualification.')
    archive = root / 'backups' / ('update-' + pending['job'])
    archive.mkdir(mode=0o700,parents=True,exist_ok=True)
    if pending['phase'] == 'prepared':
        previous = asset_files(root / 'server')
        copy_assets(root / 'server',archive / 'previous-server')
        if previous != asset_files(archive / 'previous-server'):
            raise ValueError('Previous asset preservation failed.')
        shutil.copytree(root / 'config',archive / 'previous-config')
        n.save(archive / 'previous-assets.json',{'files':previous})
        n.save(archive / 'pending-before-apply.json',pending)
        pending['phase'] = 'applying'
        n.save(root / PENDING,pending)
    elif asset_files(archive / 'previous-server') != n.private_json(archive / 'previous-assets.json',64*1024**2)['files']:
        raise ValueError('Explicit recovery requires the complete original asset preservation.')
    clear_programs(root / 'server')
    copy_assets(candidate / 'server',root / 'server')
    if digest_value(asset_files(root / 'server')) != cert['assets_digest']:
        raise ValueError('Promoted asset bytes do not match qualification.')
    policy = n.install_policy(root)
    if policy != cert['native_policy']:
        raise ValueError('Promoted native profile differs from qualification.')
    runtime = {'schema':1,'deployment_id':uuid.uuid4().hex,'source_contract':contract(),'image_build':build_identity()}
    n.save(root / CONFIG,runtime)
    verify_files(root,pending['files'])
    n.save(root / TRANSITION,{'schema':1,'purpose':'qualified_update','runtime':runtime,'native_policy':policy,
        'configuration':n.configuration(root),'previous_state':pending['previous_state'],
        'files':pending['files'],'qualification':cert})
    versions = n.private_json(candidate / 'asset-manifest.json',64*1024**2)['versions']
    from . import ark_config as c
    n.save(root / 'config/assets.json',{'schema':1,'versions':versions,
        'active_mods':c.load_editable_settings(root)['active_mods'],'source':'qualified_automatic_update'})
    n.save(archive / 'application.json',{'phase':'applied','runtime':runtime,'saved_restored':False,
                                      'in_game_content_verified':False})
    return runtime


if __name__ == '__main__':
    os.umask(0o077)
    try:
        from .compose_server import rootless
        rootless()
        apply()
        print('Qualified assets applied; Saved was not restored or copied back.')
    except Exception:
        print('Application incomplete; maintenance hold retained. No automatic rollback.',file=sys.stderr)
        raise SystemExit(78)
