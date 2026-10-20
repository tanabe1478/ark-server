"""Trusted rootless Compose maintenance plane; never imported by the Bot."""
import copy
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid

from . import ark_shutdown as n
from .ark_docker import Engine
from .ark_control import ControlError

TARGETS = {
 'server': {'/opt/ark/server':('game',False), '/opt/ark/server/ShooterGame/Saved':('saved',True),
  '/opt/ark/config':('settings',False), '/opt/ark/logs':('evidence',True), '/opt/ark/backups':('backups',True),
  '/run/ark-locks':('locks',True), '/run/ark-bot-config':('bot_config',True),
  '/run/ark-control':('bot_ipc',True), '/run/ark-admin':('admin_ipc',True), '/run/ark-platform':('platform',False)},
 'bot': {'/run/ark-bot-config':('bot_config',False), '/run/ark-control':('bot_ipc',False),
         '/run/ark-bot-lock':('bot_locks',True)},
 'updater': {'/opt/ark/server':('game',True), '/opt/ark/server/ShooterGame/Saved':('saved',False),
  '/opt/ark/config':('settings',True), '/opt/ark/logs':('evidence',True), '/opt/ark/backups':('backups',True),
  '/stage':('staging',True), '/run/ark-locks':('locks',True), '/run/ark-admin':('admin_ipc',False), '/run/ark-platform':('platform',False)},
}


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class ComposeEngine(Engine):
    def __init__(self, project, endpoint='unix:///run/ark-engine.sock'):
        if not isinstance(project, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,47}', project):
            raise ValueError('Invalid Compose project.')
        self.project, self.endpoint = project, endpoint
        self.trusted()
        options=json.loads(self.call('info','--format','{{json .SecurityOptions}}'))
        if 'name=seccomp,profile=builtin' not in options:
            raise ValueError('The rootless engine must retain its built-in default seccomp profile.')

    def api(self, method, path, value=None, timeout=1500):
        client = UnixHTTP(self.endpoint.removeprefix('unix://'), timeout)
        try:
            raw = None if value is None else json.dumps(value, separators=(',', ':')).encode()
            client.request(method, '/v1.45' + path, body=raw, headers={'Content-Type':'application/json'})
            response = client.getresponse()
            data = response.read(1024**2 + 1)
            already_stopped=response.status==304 and method=='POST' and '/stop?' in path
            if response.status >= 300 and not already_stopped or len(data) > 1024**2:
                raise ControlError('operation_failed')
            return json.loads(data) if data else None
        except (OSError, http.client.HTTPException):
            raise ControlError('outcome_unknown') from None
        finally:
            client.close()

    def current(self, role):
        if role not in ('server','bot','updater'):
            raise ValueError('Unknown service.')
        value = self.inspect(self.project + '-' + role + '-1')
        if value is None:
            raise ControlError('service_unavailable')
        self.validate(value, role)
        return value

    def image(self, identity):
        value = json.loads(self.call('image','inspect',identity))[0]
        labels = value['Config'].get('Labels') or {}
        if labels.get('dev.ark.compose.schema') != '1' or labels.get('dev.ark.compose.application') != 'ark-server':
            raise ValueError('Unqualified image family.')
        return value

    def validate(self, value, role):
        labels = value['Config'].get('Labels') or {}
        host = value['HostConfig']
        if (labels.get('com.docker.compose.project') != self.project or labels.get('com.docker.compose.service') != role
                or labels.get('com.docker.compose.container-number') != '1'
                or value['Config'].get('User') != '0:0' or host.get('Privileged') is not False
                or host.get('ReadonlyRootfs') is not True or host.get('Init') is not True
                or host.get('CapAdd') or host.get('CapDrop') != ['ALL']
                or host.get('SecurityOpt') not in (['no-new-privileges'],['no-new-privileges:true'])
                or host.get('PidMode') not in ('',None) or host.get('UsernsMode') not in ('',None) or host.get('IpcMode') != 'private'
                or host.get('Devices') or host.get('DeviceRequests') or host.get('GroupAdd')
                or host.get('NetworkMode') != self.project + '_default'):
            raise ValueError('Service isolation changed; refuse automatic replacement.')
        image_config=self.image(value['Image']).get('Config',{})
        if (value['Config'].get('Entrypoint')!=image_config.get('Entrypoint')
                or value['Config'].get('Cmd')!=image_config.get('Cmd')):
            raise ValueError('Service command differs from the immutable image.')
        restart=host.get('RestartPolicy',{})
        if role in ('server','updater') and restart.get('Name') != 'no':
            raise ValueError('Game and updater restart policies must remain disabled.')
        if role=='bot' and (restart.get('Name')!='on-failure' or restart.get('MaximumRetryCount')!=5):
            raise ValueError('Unexpected Bot restart policy.')
        if role in TARGETS:
            expected = TARGETS[role]
            mounts = {m['Destination']:m for m in value['Mounts']}
            if len(mounts)!=len(value['Mounts']):raise ValueError('Duplicate service mounts.')
            extras = set(mounts) - set(expected)
            allowed_extras=({'/opt/ark/config/discord-bot.token'} if role=='bot' else {'/run/ark-engine.sock'} if role=='updater' else set())
            if extras != allowed_extras:
                raise ValueError('Unexpected credential or control mount.')
            for target, (suffix, writable) in expected.items():
                m = mounts.get(target,{})
                if m.get('Type') != 'volume' or m.get('Name') != self.project + '_' + suffix or m.get('RW') is not writable:
                    raise ValueError('Unexpected service volume.')
            if role in ('bot','updater'):
                target='/opt/ark/config/discord-bot.token' if role=='bot' else '/run/ark-engine.sock'
                token=mounts[target]
                if token.get('Type')!='bind' or token.get('RW') is not False:
                    raise ValueError('Authority must remain an explicit read-only private bind mount.')
        if role == 'server':
            ports = host.get('PortBindings') or {}
            if set(ports) != {'7777/udp','7778/udp','27015/udp'}:
                raise ValueError('Unexpected game port exposure.')
            for items in ports.values():
                if len(items) != 1 or items[0]['HostIp'] not in ('0.0.0.0','127.0.0.1') or not 1024 <= int(items[0]['HostPort']) <= 65535:
                    raise ValueError('Unsafe published game ports.')
        elif host.get('PortBindings'):
            raise ValueError('Management services must not publish ports.')

    def run_job(self, image, job, purpose, module, args, mounts, *, env=None, timeout=7200, held=None):
        if not re.fullmatch('[0-9a-f]{32}', job) or not re.fullmatch('[a-z][a-z0-9-]{0,30}', purpose):
            raise ValueError('Invalid maintenance job.')
        self.image(image)
        name = self.project + '-' + purpose + '-' + job[:16]
        if self.inspect(name) is not None:
            raise ControlError('outcome_unknown')  # Never replay an existing invocation.
        command = ['container','create','--name',name,'--init','--restart=no','--read-only',
            '--cap-drop=ALL','--security-opt=no-new-privileges','--user=0:0','--network=bridge',
            '--pids-limit=512','--ulimit','core=0','--shm-size=256m','--no-healthcheck',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=512m','--tmpfs','/run/ark:rw,nosuid,nodev,size=128m',
            '--tmpfs','/run/ark-locks:rw,nosuid,nodev,size=1m,mode=700',
            '--label','dev.ark.job='+job,'--label','dev.ark.purpose='+purpose,
            '--log-driver=local','--log-opt=max-size=10m','--log-opt=max-file=3']
        for suffix, subpath, target, readonly in mounts:
            if (not re.fullmatch('[a-z_]+',suffix) or ',' in target or not target.startswith('/')
                    or subpath and (not re.fullmatch('jobs/[0-9a-f]{32}/[a-zA-Z0-9_./-]+',subpath) or '..' in Path(subpath).parts)):
                raise ValueError('Unsafe staging mount.')
            spec = 'type=volume,src='+self.project+'_'+suffix+',dst='+target
            if subpath:spec += ',volume-subpath='+subpath
            if readonly:spec += ',readonly'
            command += ['--mount',spec]
        for key, value in (env or {}).items():
            if key not in ('ARK_INSTALL_OPERATION',):raise ValueError('Unexpected job environment.')
            command += ['--env',key+'='+value]
        command += ['--entrypoint','/usr/bin/python3',image,'-m',module,*args]
        identity = self.call(*command).strip()
        if not re.fullmatch('[0-9a-f]{64}',identity):raise ControlError('outcome_unknown')
        self.call('start',identity)
        deadline=time.monotonic()+timeout
        cancelled=False
        while True:
            inspected=json.loads(self.call('inspect',identity))[0]
            if inspected['State']['Status']=='exited':break
            if getattr(self,'cancel',lambda:False)():
                # Auxiliary data only: no production Saved/token/engine mount.
                # Interrupted qualification is retained, NEVER accepted as proof.
                self.api('POST','/containers/'+identity+'/stop?t=15')
                inspected=json.loads(self.call('inspect',identity))[0]
                cancelled=True
                break
            if held is not None and held():raise ControlError('outcome_unknown') # leave live helper untouched
            if time.monotonic()>=deadline:raise ControlError('outcome_unknown')
            time.sleep(1)
        if inspected['State']['Status']!='exited' or type(inspected['State']['ExitCode']) is not int:
            raise ControlError('outcome_unknown')
        return {'container':identity,'exit_code':inspected['State']['ExitCode'],'oom_killed':inspected['State']['OOMKilled'],
                'cancelled':cancelled,'inspect':inspected}

    def replace(self, old, role, image, job, archive, *, allow_missing=False):
        self.validate(old,role)
        self.image(image)
        identity = old['Id']
        current=self.inspect(self.project+'-'+role+'-1')
        if current is None:
            if not allow_missing or old['State']['Status']!='exited':
                raise ControlError('outcome_unknown')
            stopped=old
        else:
            if current['Id']!=identity:raise ControlError('outcome_unknown')
            self.api('POST','/containers/'+identity+'/stop?t=1500')
            stopped = json.loads(self.call('inspect',identity))[0]
        if stopped['State']['Status'] != 'exited' or stopped['State']['OOMKilled']:
            raise ControlError('outcome_unknown')
        if role == 'server' and stopped['State']['ExitCode'] not in (0,134):
            raise ControlError('outcome_unknown')
        archive=archive/'containers'/uuid.uuid4().hex
        archive.mkdir(mode=0o700,parents=True)
        n.save(archive / (role+'-previous-container.json'),stopped)
        if current is not None:
            path = archive / (role+'-previous-console.txt')
            environment={key:os.environ[key] for key in ('HOME','PATH','LANG') if key in os.environ}
            with path.open('xb') as output:
                os.fchmod(output.fileno(),0o600)
                result=subprocess.run(['/usr/bin/docker','--host',self.endpoint,'logs','--timestamps',identity],
                    stdout=output,stderr=subprocess.STDOUT,env=environment,timeout=60)
            if result.returncode:raise ControlError('outcome_unknown')
        host = copy.deepcopy(stopped['HostConfig'])
        allowed = {'Binds','Mounts','PortBindings','NetworkMode','ReadonlyRootfs','CapDrop','CapAdd','SecurityOpt','Init',
            'PidsLimit','ShmSize','Ulimits','Tmpfs','LogConfig','Memory','MemorySwap','NanoCpus','CpuQuota',
            'CpuPeriod','CpusetCpus','OomKillDisable','OomScoreAdj','Dns','DnsOptions','DnsSearch','ExtraHosts',
            'IpcMode','AutoRemove','RestartPolicy','CgroupnsMode','MaskedPaths','ReadonlyPaths'}
        host = {key:value for key,value in host.items() if key in allowed}
        labels = dict(stopped['Config']['Labels']);labels['com.docker.compose.image']=image
        labels['dev.ark.update']=job
        image_config = self.image(image)['Config']
        environment = {x.split('=',1)[0]:x.split('=',1)[1] for x in image_config.get('Env',[])}
        for raw in stopped['Config'].get('Env',[]):
            key,value=raw.split('=',1)
            if key in ('ARK_DISCORD_OFFLINE','ARK_PROJECT','ARK_COMPOSE','ARK_LOCK_DIRECTORY','ARK_CONTROL_SOCKET'):environment[key]=value
        spec = {'Image':image,'User':'0:0','WorkingDir':'/opt/ark','Env':[k+'='+v for k,v in environment.items()],
                'Labels':labels,'HostConfig':host,'StopSignal':'SIGTERM',
                'Entrypoint':image_config['Entrypoint'],'Cmd':image_config.get('Cmd'),
                'NetworkingConfig':{'EndpointsConfig':{self.project+'_default':{'Aliases':[role,self.project+'-'+role+'-1']}}}}
        if role=='server':spec['ExposedPorts']={key:{} for key in host['PortBindings']}
        if 'Healthcheck' in image_config:spec['Healthcheck']=image_config['Healthcheck']
        if 'StopTimeout' in stopped['Config']:spec['StopTimeout']=stopped['Config']['StopTimeout']
        if current is not None:
            self.call('rm',identity)  # Stopped, archived; never --force or volume/image pruning.
        created=self.api('POST','/containers/create?name='+self.project+'-'+role+'-1',spec)
        candidate=json.loads(self.call('inspect',created['Id']))[0]
        self.validate(candidate,role)  # Validate reconstructed mounts/security BEFORE start.
        self.api('POST','/containers/'+created['Id']+'/start')
        return created['Id']
