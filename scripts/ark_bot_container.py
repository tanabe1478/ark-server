"""Run the outbound Bot with ONLY its private config/token, singleton lock and IPC."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from . import ark_shutdown as native
from .ark_docker import Engine

CONFIG = Path('config/docker-bot.json')


def contract():
    paths = sorted(p.relative_to(native.CODE_ROOT).as_posix() for p in (native.CODE_ROOT/'scripts').glob('*.py'))
    paths += ['deploy/docker/Dockerfile.bot', 'requirements-discord.lock']
    values = {p:native.digest(native.CODE_ROOT/p) for p in paths}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def install(root, image):
    e = Engine(); e.trusted()
    obj = json.loads(e.call('image','inspect',image))[0]
    current = contract()
    if obj['Config'].get('Labels',{}).get('dev.ark.bot-contract') != current:
        raise native.ShutdownError('Bot image does not match reviewed sources.')
    native.save(Path(root)/CONFIG, {'schema':1,'image':obj['Id'],'contract':current,'name':'ark-discord'})


def main():
    os.umask(0o077)
    root = native.CODE_ROOT
    value = native.private_json(root/CONFIG)
    if (set(value) != {'schema','image','contract','name'} or type(value['schema']) is not int or value['schema'] != 1
            or not re.fullmatch('sha256:[0-9a-f]{64}',value['image']) or value['name'] != 'ark-discord'
            or value['contract'] != contract()):
        return 78
    e = Engine();e.trusted()
    old = e.inspect(value['name'])
    if old is not None:
        if old['Config'].get('Labels',{}).get('dev.ark.bot') != str(root) or old['State']['Running']:
            return 78
        native.save(root/'logs/docker-bot'/('retired-'+old['Id']+'.json'),
                    {'container_id':old['Id'],'exit_code':old['State']['ExitCode'],'image':old['Image']})
        e.call('rm',old['Id'])
    args = ['create','--name',value['name'],'--init','--restart=no','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--stop-timeout=1400','--pids-limit=128',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=64m','--log-driver=local','--log-opt=max-size=10m','--log-opt=max-file=3',
            '--label=dev.ark.bot='+str(root)]
    for source,target,rw in ((root/'config/discord-bot.json','/opt/ark/config/discord-bot.json',False),
                            (root/'config/discord-bot.token','/opt/ark/config/discord-bot.token',False),
                            (root/'.discord.lock','/opt/ark/.discord.lock',True),
                            (Path(f'/run/user/{os.getuid()}/ark-control'),'/run/ark-control',False)):
        if source.is_symlink() or not source.exists() or ',' in str(source):
            return 78
        args += ['--mount',f'type=bind,src={source},dst={target}'+('' if rw else ',readonly')]
    identity = e.call(*args,value['image']).strip()
    if not re.fullmatch('[0-9a-f]{64}',identity):
        return 70
    stopping = []
    def requested(number,frame):stopping.append(number)
    signal.signal(signal.SIGINT,requested);signal.signal(signal.SIGTERM,requested)
    # CLI environment carries no secrets; the container receives no token ENV/args.
    process = subprocess.Popen(['/usr/bin/docker','--host',e.endpoint,'start','--attach',identity])
    forwarded = False
    while process.poll() is None:
        if stopping and not forwarded:
            forwarded = True
            e.call('stop','--timeout=1400',identity,timeout=1430)
        time.sleep(.2)
    inspected = e.inspect(value['name'])
    if inspected is None or inspected['Id'] != identity:
        return 70
    if inspected['State']['Running']:
        e.call('stop','--timeout=1400',identity,timeout=1430)
        return 70
    return inspected['State']['ExitCode']


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception:
        print('Bot container operation failed; raw errors and secrets are suppressed.',flush=True)
        sys.exit(1)
