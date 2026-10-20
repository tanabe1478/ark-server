"""Local Docker deployment commands. Builds/installations never implicitly start ARK."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import ark_config, ark_shutdown as native
from .ark_docker import (Engine, CONFIG, STATE, source_contract, contract_id,
                         install_descriptor, approve_cutover)
from .ark_bot_container import contract as bot_contract, install as install_bot
from .ark_bot_config import BotConfig
from .ark_automation import Automation
from .ark_control import Controller, SystemdBackend, exclusive_lock, ControlError

ROOT = native.CODE_ROOT


def render(name, data_root=ROOT):
    return (ROOT/'deploy/docker'/name).read_text().replace('@ROOT@', str(ROOT)).replace('@DATA_ROOT@',str(data_root))


def unit_path(name):
    return Path.home()/'.config/systemd/user'/name


def stopped(unit):
    out = subprocess.check_output(['/usr/bin/systemctl','--user','show',unit,'-p','ActiveState','-p','MainPID'],text=True,timeout=10)
    values = dict(line.split('=',1) for line in out.splitlines() if '=' in line)
    if values.get('ActiveState') not in ('inactive','failed') or values.get('MainPID') != '0':
        raise native.ShutdownError('Stop the receiver/control service explicitly before changing its deployment.')


def preserve(label):
    folder = ROOT/'backups'/(label+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    return folder


def install_unit(name, template, folder=None):
    path = unit_path(name)
    if path.is_symlink():
        raise native.ShutdownError('A regular user unit is required.')
    if path.exists():
        if folder is None and path.read_text() != render(template):
            raise native.ShutdownError('Existing infrastructure unit differs; review it locally.')
        if folder is not None:
            shutil.copyfile(path,folder/name);(folder/name).chmod(0o600)
    native.ark_config.atomic_write(path,render(template).encode())


def daemon_reload():
    subprocess.run(['/usr/bin/systemctl','--user','daemon-reload'],check=True,timeout=15)


def build(kind):
    engine = Engine(); engine.trusted()
    args = ['build','--platform=linux/amd64','--pull=false','--file',f'deploy/docker/Dockerfile.{kind}']
    name,value = ('SOURCE_CONTRACT',contract_id(source_contract())) if kind=='runtime' else ('BOT_CONTRACT',bot_contract())
    args += ['--build-arg',name+'='+value,'--tag','ark-'+kind+':local','.']
    folder = ROOT/'logs/docker-build';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (folder/(kind+'.log')).open('w') as log:
        run = subprocess.run(['/usr/bin/docker','--host',engine.endpoint,*args],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    if run.returncode:
        raise native.ShutdownError('Image build failed; inspect private build output.')
    return {'built':kind,'image':engine.call('image','inspect','ark-'+kind+':local','--format','{{.Id}}').strip()}


def activate_game(image, bind_address):
    # Initial native->Docker cutover only. Never overwrite an existing Docker
    # deployment contract after changing code; that requires a separate review.
    if (ROOT/CONFIG).exists() or (ROOT/CONFIG).is_symlink():
        raise native.ShutdownError('A Docker runtime is already configured; no implicit upgrade/review.')
    stopped('ark-discord.service');stopped('ark-control.service')
    with exclusive_lock(ROOT/'.automation.lock'), exclusive_lock(ROOT/'.control.lock'), exclusive_lock(ROOT/'.backup.lock'):
        backend = SystemdBackend()
        state = backend.state()
        if Automation(BotConfig.load()).problem():
            raise native.ShutdownError('A pending/blocked operation requires independent review.')
        decision = Controller(ROOT,backend=backend).verify_operational_stopped(state)
        native.policy(ROOT) or native.install_policy(ROOT)
        folder = preserve('docker-cutover')
        with ark_config.server_lock(ROOT):
            entries = native.capture(ROOT/native.SAVED,folder/'stopped')
            native.save(folder/'review.json',{'service_record':state,'decision':decision,
                'configuration':native.configuration(ROOT),'in_game_content_verified':False})
            for path in ('config/discord-bot.json','config/shutdown-policy.json','logs/automation/operation.json'):
                src=ROOT/path
                if src.exists():
                    target=folder/Path(path).name;shutil.copyfile(src,target);target.chmod(0o600)
            native.save(ROOT/'config/docker-cutover.pending.json',{'schema':1,'preservation':str(folder.relative_to(ROOT)),
                        'purpose':'incomplete_runtime_cutover_blocks_start'})
            runtime = install_descriptor(ROOT,image,'ark-aquatica',bind_address)
            approve_cutover(ROOT,runtime,decision|{'source_service_record':state,'preservation':str(folder.relative_to(ROOT))})
            config=BotConfig.load();replace(config,auto_stop_enabled=False,shutdown_verified=False).save(ROOT)
            install_unit('ark-genesis.service','ark-genesis.service.in',folder)
        daemon_reload()
        (ROOT/'config/docker-cutover.pending.json').unlink()
        return {'configured':True,'started':False,'shutdown_verified':False,'auto_stop_enabled':False,
                'preservation':str(folder.relative_to(ROOT))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','build-runtime','build-bot','install-infrastructure','install-bot','activate-game'))
    parser.add_argument('--image')
    parser.add_argument('--bind-address',choices=('0.0.0.0','127.0.0.1'),default='0.0.0.0')
    parser.add_argument('--confirm',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    try:
        if args.action=='check':
            e=Engine();e.trusted();result={'rootless_engine':True,'game_binary_known':native.digest(ROOT/native.BINARY)==native.BINARY_SHA256,
                                          'docker_game_configured':(ROOT/CONFIG).exists()}
        elif args.action.startswith('build-'):
            result=build(args.action.removeprefix('build-'))
        else:
            if not args.confirm:
                parser.error('Installation/cutover requires --confirm; it still does not start the game.')
            if args.action=='install-infrastructure':
                install_unit('ark-docker.service','ark-docker.service.in')
                install_unit('ark-control.service','ark-control.service.in')
                daemon_reload();result={'installed':True,'started':False,'enabled':False}
            elif args.action=='install-bot':
                if not args.image:parser.error('--image is required')
                stopped('ark-discord.service')
                folder=preserve('docker-bot-install')
                install_bot(ROOT,args.image)
                install_unit('ark-discord.service','ark-discord.service.in',folder)
                daemon_reload();result={'configured':True,'started':False,'game_changed':False}
            else:
                if not args.image:parser.error('--image is required')
                result=activate_game(args.image,args.bind_address)
        print(json.dumps(result));return 0
    except (OSError,ValueError,ControlError,subprocess.SubprocessError):
        print(json.dumps({'ok':False,'error':'Local Docker setup failed; no automatic retry. Inspect private state.'}));return 1


if __name__=='__main__':
    sys.exit(main())
