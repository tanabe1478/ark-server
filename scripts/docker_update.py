"""Explicit isolated SteamCMD update job; never runs against the live installation.

Host: python -m scripts.docker_update --image sha256:... --confirm
Container: fixed internal /stage only. No Saved, Bot token or engine socket.
Promotion, MOD updates, profile revalidation and restore are separate reviews.
"""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from . import ark_shutdown as native
from .ark_docker import Engine, contract_id, source_contract


def container_job():
    root=Path('/stage');plan=native.private_json(root/'plan.json')
    if set(plan)!={'schema','purpose','app_id'} or plan!={'schema':1,'purpose':'isolated_steam_update','app_id':376030}:
        return 78
    home=Path('/tmp/steam-home');home.mkdir(mode=0o700,exist_ok=True)
    environment=os.environ|{'HOME':str(home)}
    with (root/'steam-update.log').open('wb') as output:
        run=subprocess.run([str(root/'steamcmd/steamcmd.sh'),'+force_install_dir',str(root/'server'),
                '+login','anonymous','+app_update','376030','validate','+quit'],cwd=root/'steamcmd',env=environment,
                stdout=output,stderr=subprocess.STDOUT)
    binary=root/'server/ShooterGame/Binaries/Linux/ShooterGameServer'
    native.save(root/'result.json',{'steamcmd_exit':run.returncode,'binary_present':binary.is_file(),
        'binary_sha256':native.digest(binary) if binary.is_file() else None,
        'promoted':False,'saved_imported':False,'in_game_content_verified':False})
    return 0 if run.returncode==0 and binary.is_file() else 1


def stage(image,root=native.CODE_ROOT):
    root=Path(root);engine=Engine();engine.trusted()
    reviewed=json.loads(engine.call('image','inspect',image))[0]
    if reviewed['Config'].get('Labels',{}).get('dev.ark.source-contract')!=contract_id(source_contract()):
        raise ValueError('Build/review an update runtime from the current sources first.')
    image=reviewed['Id']  # Never implicitly pull/update a mutable tag.
    if not (root/'steamcmd/steamcmd.sh').is_file():
        raise ValueError('Install the official SteamCMD bootstrap separately first.')
    identifier=uuid.uuid4().hex
    folder=root/'backups'/('update-candidate-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+identifier[:8])
    folder.mkdir(mode=0o700,parents=True)
    # Preserve symlinks only when entirely contained in the SteamCMD bootstrap.
    for path in (root/'steamcmd').rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to((root/'steamcmd').resolve()):
            raise ValueError('External SteamCMD link requires review.')
    shutil.copytree(root/'steamcmd',folder/'steamcmd',symlinks=True)
    native.save(folder/'plan.json',{'schema':1,'purpose':'isolated_steam_update','app_id':376030})
    name='ark-update-'+identifier
    args=['run','--name',name,'--init','--restart=no','--read-only','--cap-drop=ALL',
          '--security-opt=no-new-privileges','--tmpfs=/tmp:rw,nosuid,nodev,size=512m',
          '--mount',f'type=bind,src={folder},dst=/stage','--entrypoint=/usr/bin/python3',
          image,'-m','scripts.docker_update','--container-job']
    # A failed/uncertain job is retained, never retried or promoted.
    engine.call(*args,timeout=3600)
    return {'candidate':str(folder.relative_to(root)),'result':native.private_json(folder/'result.json'),
            'production_changed':False,'automatic_promotion':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image');p.add_argument('--confirm',action='store_true')
    p.add_argument('--container-job',action='store_true');args=p.parse_args();os.umask(0o077)
    try:
        if args.container_job:return container_job()
        if not args.confirm or not args.image:p.error('--image and --confirm required; only a new candidate is written')
        print(json.dumps(stage(args.image)));return 0
    except Exception:
        print(json.dumps({'ok':False,'error':'Update candidate failed or is uncertain; no retry/promotion.'}));return 1


if __name__=='__main__':sys.exit(main())
