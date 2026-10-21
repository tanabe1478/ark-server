"""Normally automatic staged game/MOD/source-image updates for rootless Compose.

No production game is started merely to update it. Promotion happens only after
idle/grace rechecks, a qualified old stop, and two latest-Saved-copy cycles.
An uncertain transmission, crash or interrupted application is never replayed.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_control import ControlError, exclusive_lock
from .compose_assets import fetch, extract_tar, asset_bytes
from .compose_engine import ComposeEngine
from .compose_qualify import digest_value
from .compose_release import manifest
from .compose_server import call, settings, PENDING
from .compose_lifecycle import READY, mark_shutdown, stopped as platform_stopped
import socket

ACTIVE = Path('logs/updates/current.json')
SOURCE_STAGE=Path('/stage')
REQUEST = Path('logs/updates/request.json')
TERMINAL = {'disabled','no_update','deferred','completed','check_failed','candidate_rejected','candidate_blocked','platform_stopped','owner_closed_interrupted'}


def persist(root,record):
    from datetime import datetime,timezone
    record['updated_at']=datetime.now(timezone.utc).isoformat()
    n.save(root/ACTIVE,record)
    n.save(root/'logs/updates/jobs'/(record['job']+'.json'),record)
    n.save(root/'logs/updates/events'/record['job']/(uuid.uuid4().hex+'.json'),record)


def definite_prelaunch_failure(detail):
    return (isinstance(detail,dict) and detail.get('native_launch_intent_created') is False
            and 'last_process_state' in detail and detail['last_process_state'] is None)


def memory_available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    return 0


def submit(action, job=None):
    if action not in ('check','validate-current','validate-source','recover'):
        raise ValueError('Invalid update request.')
    if action not in ('recover','validate-source') and job is not None:raise ValueError('Unexpected update argument.')
    if action=='recover':
        import re
        if not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job):
            raise ValueError('Recovery requires the exact preserved job ID.')
    if action=='validate-source':
        import re
        if not isinstance(job,str) or not re.fullmatch('source/[0-9a-f]{64}',job):
            raise ValueError('Owner source must be an exported source/<release-digest> bundle.')
    root = n.CODE_ROOT
    (root/'logs/updates').mkdir(mode=0o700,parents=True,exist_ok=True)
    with exclusive_lock(root / 'logs/updates/request.lock'):
        if (root / REQUEST).exists():raise ControlError('busy')
        value = {'id':uuid.uuid4().hex,'action':action}
        if job is not None:value['source' if action=='validate-source' else 'job']=job
        n.save(root / REQUEST,value)
    return value


class Updater:
    def __init__(self, root=n.CODE_ROOT, engine=None):
        self.root = Path(root)
        self.engine = engine or ComposeEngine(os.environ['ARK_PROJECT'])
        self.closed = False
        self.external_shutdown = False
        self.engine.cancel=lambda:self.external_shutdown
        self.record = None
        self.folder = None
        self.started=time.monotonic()
        previous=n.private_json(self.root/ACTIVE) if (self.root/ACTIVE).exists() else {}
        self.handoff_job=previous.get('job') if previous.get('phase') in ('handoff','handoff_replacing') else None

    def save(self, phase, **extra):
        self.record.update(phase=phase,**extra)
        persist(self.root,self.record)

    def mount(self, relative, target, readonly=False):
        return ('staging','jobs/'+self.record['job']+'/'+relative,target,readonly)

    def asset_mount(self):
        mode=self.record.get('asset_mode','staged')
        if mode=='installed':return ('game','','/opt/ark/server',True)
        if mode=='staged':return self.mount('candidate/server','/opt/ark/server',True)
        raise ValueError('Invalid asset mode.')

    def metadata(self, image, role):
        folder = self.folder / ('meta-'+role)
        folder.mkdir(mode=0o700)
        result = self.engine.run_job(image,self.record['job'],'meta-'+role,'scripts.compose_meta',[],
                                     [self.mount('meta-'+role,'/result')],timeout=120)
        if result['exit_code'] != 0 or result['oom_killed']:
            raise ValueError('Image metadata unavailable.')
        return n.private_json(folder / 'image.json')

    def upstream(self, current):
        commit = json.loads(fetch('https://api.github.com/repos/tanabe1478/ark-server/commits/main',2*1024**2))['sha']
        import re
        if not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError('Invalid public release revision.')
        source = self.folder / 'source-archive'
        source.mkdir(mode=0o700)
        raw = fetch('https://api.github.com/repos/tanabe1478/ark-server/tarball/'+commit,16*1024**2)
        extract_tar(raw,source,max_bytes=64*1024**2,max_files=2000)
        roots = list(source.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():raise ValueError('Invalid public source archive.')
        source = roots[0]
        if not (source / 'deploy/docker/Dockerfile.compose').is_file():
            return None  # A legacy/incompatible upstream is not a downgrade.
        release = manifest(source)
        if release['digest'] == current['release']['digest']:return None
        return {'commit':commit,'source':source,'release':release}

    def owner_source(self, relative):
        import re
        if not isinstance(relative,str) or not re.fullmatch('source/[0-9a-f]{64}',relative):raise ValueError('Invalid exported source bundle.')
        source=SOURCE_STAGE/relative
        inventory=n.inventory(source)
        from .public_export import candidates, inspect, private_values
        allowed={str(path.relative_to(source)) for path in candidates(source)}
        if (set(inventory)!=allowed or len(inventory)>2000 or sum(v['bytes'] for v in inventory.values())>64*1024**2):
            raise ValueError('Owner source must contain only a bounded public export.')
        secrets=private_values(self.root)
        if any(inspect((source/path).read_bytes(),secrets) for path in inventory):raise ValueError('Owner source failed the public-source scan.')
        release=manifest(source)
        if source.name!=release['digest']:raise ValueError('Exported source digest changed.')
        return {'source':source,'release':release,'commit':None,'origin':'explicit_owner_source'}

    def build(self, source, role, target=None):
        dockerfile = 'deploy/docker/Dockerfile.compose-bot' if role == 'bot' else 'deploy/docker/Dockerfile.compose'
        label = self.engine.project+'-candidate-'+role+':'+self.record['job']
        iid = self.folder / (role+'-image-id.txt')
        command = ['/usr/bin/docker','--host',self.engine.endpoint,'build','--pull','--iidfile',str(iid),'-t',label,'-f',dockerfile]
        if target:command += ['--target',target]
        command += ['.']
        environment = {key:os.environ[key] for key in ('HOME','PATH','LANG') if key in os.environ}
        with (self.folder / (role+'-build.log')).open('wb') as output:
            result = subprocess.run(command,cwd=source,env=environment,stdout=output,stderr=subprocess.STDOUT,timeout=3600)
        if result.returncode:raise ValueError('Candidate image build failed.')
        identity = iid.read_text().strip()
        self.engine.image(identity)
        return identity

    def installer(self, image, operation, plan):
        input_folder = self.folder / ('input-'+operation)
        input_folder.mkdir(mode=0o700)
        n.save(input_folder / 'plan.json',plan)
        target = 'check' if operation == 'check' else 'candidate'
        (self.folder / target).mkdir(mode=0o700,exist_ok=True)
        mounts = [self.mount('input-'+operation,'/job',True),self.mount(target,'/candidate'),('steam','','/steam',False)]
        if operation == 'download':mounts += [('game','','/source',True)]
        result = self.engine.run_job(image,self.record['job'],'steam-'+operation,'scripts.compose_install',[],mounts)
        self.save('checking' if operation == 'check' else 'downloaded',installer=result['container'])
        if result.get('cancelled'):raise ControlError('automation_blocked')
        if result['exit_code'] != 0 or result['oom_killed']:raise ValueError('Isolated Steam job failed.')

    def prepare(self):
        result = call({'method':'prepare_update','job':self.record['job']})
        if not result['ok']:
            raise ControlError(result.get('error',{}).get('code') or 'outcome_unknown')
        if not result['data'].get('changed'):
            self.save('deferred',reason=result['data'].get('reason'))
            return False
        self.save('prepared',was_running=result['data']['was_running'])
        return True

    def qualify(self, image):
        trial = self.folder / 'trial'
        for directory in ('config','logs','backups','result'):
            (trial / directory).mkdir(mode=0o700,parents=True)
        with c.server_lock(self.root):
            pending = n.private_json(self.root / PENDING)
            from .ark_docker import verify_files
            verify_files(self.root,pending['files'])
            shutil.copytree(self.root / n.SAVED,trial / 'Saved')
            shutil.copy2(self.root / 'config/server.json',trial / 'config/server.json')
            verify_files(self.root,pending['files'])
        mounts = [self.asset_mount(),
                  self.mount('trial/Saved','/opt/ark/server/ShooterGame/Saved')]
        mounts += [self.mount('trial/'+name,'/opt/ark/'+name) for name in ('config','logs','backups')]
        mounts += [self.mount('trial/result','/result')]
        self.save('qualifying')
        def held():
            failure=trial/'result/failure.json'
            return failure.exists() and n.private_json(failure).get('native_launch_intent_created') is True
        result = self.engine.run_job(image,self.record['job'],'qualification','scripts.compose_qualify',[],mounts,timeout=1800,held=held)
        self.save('qualification_finished',qualification_container=result['container'])
        if result.get('cancelled'):
            stopped=call({'method':'cancel_update','job':self.record['job']})
            if not stopped['ok']:raise ControlError('outcome_unknown')
            self.save('deferred',reason='platform_shutdown')
            return None
        if result['oom_killed']:raise ControlError('outcome_unknown')
        if result['exit_code'] != 0:
            failure=trial/'result/failure.json'
            detail=n.private_json(failure) if failure.exists() else {}
            # Only a definite failure BEFORE any native launch can resume the
            # old release automatically. OOM, timeout, observer loss or ANY
            # incomplete native trial stays held for explicit owner recovery.
            if not definite_prelaunch_failure(detail):
                raise ControlError('outcome_unknown')
            abort = call({'method':'cancel_update' if self.external_shutdown else 'abort_update','job':self.record['job']})
            if not abort['ok']:raise ControlError('outcome_unknown')
            self.save('candidate_rejected',reason='independent_qualification_failed',saved_restored=False)
            return False
        cert = n.private_json(trial / 'result/qualification.json',8*1024**2)
        if cert.get('two_cycles_passed') is not True or cert.get('progress_shape_verified') is not True:raise ControlError('outcome_unknown')
        self.save('qualified',in_game_content_verified=False)
        return True

    def apply(self, image, recovering=False):
        mounts = [('game','','/opt/ark/server',self.record.get('asset_mode','staged')=='installed'),('saved','','/opt/ark/server/ShooterGame/Saved',True),
            ('settings','','/opt/ark/config',False),('evidence','','/opt/ark/logs',False),('backups','','/opt/ark/backups',False),
            self.mount('candidate','/candidate',True),self.mount('trial/result','/result',True),self.mount('trial','/validation',True)]
        purpose = 'apply-recovery-'+uuid.uuid4().hex[:8] if recovering else 'application'
        result = self.engine.run_job(image,self.record['job'],purpose,'scripts.compose_apply',[],mounts,timeout=7200)
        self.save('applied' if result['exit_code']==0 and not result['oom_killed'] else 'blocked',application_container=result['container'])
        if result['exit_code'] != 0 or result['oom_killed']:raise ControlError('outcome_unknown')

    def finish(self, images, old):
        # Old native game was already qualified/stopped by prepare_update. Stop
        # its persistent controller BEFORE replacing source/assets/contracts.
        self.engine.api('POST','/containers/'+old['server']['Id']+'/stop?t=1500')
        stopped = self.engine.inspect(self.engine.project+'-server-1')
        pending = n.private_json(self.root / PENDING)
        expected = 134 if pending['previous_state'].get('SupervisorReturnCode') == -6 else 0
        if (stopped['Id'] != old['server']['Id'] or stopped['State']['Status'] != 'exited'
                or stopped['State']['ExitCode'] != expected or stopped['State']['OOMKilled']):
            raise ControlError('outcome_unknown')
        archive=self.root/'backups'/('update-'+self.record['job'])
        # Compose down may remove the stopped old ID while application runs.
        # Retain its actual stopped inspect BEFORE any assets are changed.
        n.save(archive/'controller-before-apply.json',stopped)
        self.save('applying',images=images,old_containers={key:value['Id'] for key,value in old.items()})
        self.apply(images['server'])
        if self.external_shutdown:
            self.save('blocked',reason='platform_shutdown_after_application')
            return
        archive = self.root / 'backups' / ('update-'+self.record['job'])
        for role,image in images.items():
            self.engine.call('image','tag',image,self.engine.project+'-'+role+':local')
        replacement=self.engine.replace(old['server'],'server',images['server'],self.record['job'],archive)
        self.save('new_controller_started',replacement_server=replacement)
        if self.external_shutdown:return
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            result = call({'method':'status'},timeout=5)
            if result['ok']:break
            time.sleep(1)
        else:raise ControlError('outcome_unknown')
        self.save('commit_requested',commit_sent=True)
        result = call({'method':'commit_update','job':self.record['job']})
        if not result['ok']:raise ControlError('outcome_unknown')
        self.save('committed',saved_restored=False,in_game_content_verified=False)
        self.finish_services(images,archive)

    def finish_services(self,images,archive):
        if self.external_shutdown:
            self.save('platform_stopped',handoff_completed=False,saved_restored=False);return
        bot=self.engine.current('bot')
        if bot['Image']!=images['bot']:self.engine.replace(bot,'bot',images['bot'],self.record['job'],archive)
        if self.external_shutdown:
            self.save('platform_stopped',handoff_completed=False,saved_restored=False);return
        updater=self.engine.current('updater')
        if updater['Image']!=images['updater']:
            self.handoff(images['updater'],updater);self.closed=True
        else:self.save('completed',handoff_completed=True,saved_restored=False)

    def handoff(self, image, old):
        # Only a trusted maintenance helper inherits the maintenance socket.
        name=self.engine.project+'-handoff-'+self.record['job'][:16]
        if self.engine.inspect(name) is not None:raise ControlError('outcome_unknown')
        command=['container','create','--name',name,'--init','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--restart=no','--user=0:0','--network=none','--pids-limit=128',
            '--label','dev.ark.job='+self.record['job'],'--label','dev.ark.purpose=maintenance-handoff',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--tmpfs','/run/ark:rw,nosuid,nodev,size=16m',
            '--volumes-from',old['Id'],'--env','ARK_PROJECT='+self.engine.project,
            '--entrypoint','/usr/bin/python3',image,'-m','scripts.compose_update','handoff',self.record['job']]
        self.save('handoff',next_updater_image=image,old_updater=old['Id'])
        identity=self.engine.call(*command).strip()
        self.engine.call('start',identity)

    def perform(self, force=False, source=None):
        previous = n.private_json(self.root / ACTIVE) if (self.root / ACTIVE).exists() else None
        if previous and previous.get('phase') not in TERMINAL:
            return {'phase':'blocked','reason':'previous_update_outcome_unknown'}
        job=uuid.uuid4().hex
        self.folder=SOURCE_STAGE/'jobs'/job;self.folder.mkdir(mode=0o700,parents=True)
        self.record={'schema':1,'job':job,'phase':'checking','saved_restored':False}
        self.save('checking')
        config=settings(self.root)
        if not force and source is None and not any(config[key] for key in ('update_game','update_mods','update_images')):
            self.save('disabled');return self.record
        prepared=False
        try:
            old={role:self.engine.current(role) for role in ('server','bot','updater')}
            current=self.metadata(old['server']['Image'],'current')
            installed=n.private_json(self.root/'config/assets.json')['versions']
            active=c.load_editable_settings(self.root)['active_mods']
            versions=json.loads(json.dumps(installed))
            changed_mods=[];game=False;upstream=None
            if not force:
                if source is None and (config['update_game'] or config['update_mods']):
                    self.installer(old['server']['Image'],'check',{'operation':'check','active_mods':active if config['update_mods'] else []})
                    latest=n.private_json(self.folder/'check/versions.json')
                    game=config['update_game'] and latest['build_id']!=installed['build_id']
                    if game:versions['build_id']=latest['build_id']
                    if config['update_mods']:
                        changed_mods=[x for x in active if installed.get('mods',{}).get(x,{}).get('time_updated')!=latest['mods'][x]['time_updated']]
                        for identity in changed_mods:versions.setdefault('mods',{})[identity]=latest['mods'][identity]
                if source is not None:upstream=self.owner_source(source)
                elif config['update_images']:upstream=self.upstream(current)
            if not force and not game and not changed_mods and upstream is None:
                self.save('no_update');return self.record
            fingerprint=digest_value({'versions':versions,'release':upstream['release']['digest'] if upstream else current['release']['digest']})
            rejected=self.root/'logs/updates/rejected'/(fingerprint+'.json')
            if rejected.exists() and not force and source is None:
                self.save('candidate_blocked',candidate=fingerprint);return self.record
            # Source-only (including validate-current) never changes programs.
            # Qualifiers receive the installed game READ ONLY, not a 24GB copy.
            asset_mode='staged' if game or changed_mods else 'installed'
            self.save('staging',candidate=fingerprint,asset_mode=asset_mode)
            images={role:old[role]['Image'] for role in old}
            if upstream:
                images={role:self.build(upstream['source'],role,('runtime' if role=='server' else 'updater') if role!='bot' else None)
                        for role in ('server','bot','updater')}
                candidate_metadata=self.metadata(images['server'],'candidate')
                if candidate_metadata['release']!=upstream['release']:raise ValueError('Built release identity differs.')
            if asset_mode=='installed':
                folder=self.folder/'candidate';folder.mkdir(mode=0o700)
                n.save(folder/'asset-manifest.json',{'schema':1,'asset_mode':'installed','versions':versions})
            else:
                self.installer(images['server'],'download',{'operation':'download','install':{
                    'schema':1,'game':game,'mods':changed_mods,'versions':versions}})
            # A new compiler/layout must not stop the old server just to fail
            # discovery. Probe with no Saved mount before native-cycle proof.
            (self.folder/'profile').mkdir(mode=0o700)
            probe=self.engine.run_job(images['server'],job,'native-profile','scripts.compose_profile_probe',[],
                [self.asset_mount(),self.mount('profile','/result')],timeout=120)
            if probe.get('cancelled'):
                self.save('deferred',reason='platform_shutdown');return self.record
            if probe['exit_code']!=0 or probe['oom_killed']:
                n.save(rejected,{'job':job,'phase':'unsupported_native_layout'})
                self.save('candidate_rejected',reason='unsupported_native_layout',production_touched=False)
                return self.record
            if memory_available()<config['minimum_free_memory_bytes']:
                self.save('deferred',reason='insufficient_validation_memory');return self.record
            saved_bytes=sum(value['bytes'] for value in n.inventory(self.root/n.SAVED).values())
            required_space=(asset_bytes(self.root/'server') if asset_mode=='staged' else 0)+12*saved_bytes+4*1024**3
            if shutil.disk_usage(self.folder).free<required_space:
                self.save('deferred',reason='insufficient_preservation_space');return self.record
            if self.external_shutdown:
                self.save('deferred',reason='platform_shutdown');return self.record
            if not self.prepare():return self.record
            prepared=True
            if self.external_shutdown:
                cancelled=call({'method':'cancel_update','job':job})
                if not cancelled['ok']:raise ControlError('outcome_unknown')
                self.save('deferred',reason='platform_shutdown');return self.record
            qualified=self.qualify(images['server'])
            if qualified is not True:
                if qualified is False:n.save(rejected,{'job':job,'phase':'independent_qualification_failed'})
                return self.record
            if self.external_shutdown:
                cancelled=call({'method':'cancel_update','job':job})
                if not cancelled['ok']:raise ControlError('outcome_unknown')
                self.save('deferred',reason='platform_shutdown');return self.record
            self.finish(images,old)
            return self.record
        except Exception as error:
            # Never delete a pending maintenance hold or silently restart after
            # uncertain native/container/API outcomes. Definite failed trials
            # resume the unchanged old runtime only through abort_update above.
            unknown=isinstance(error,subprocess.TimeoutExpired) or isinstance(error,ControlError) and error.code=='outcome_unknown'
            if self.external_shutdown and not prepared and not (self.root/PENDING).exists():
                self.save('deferred',reason='platform_shutdown',failure_type=type(error).__name__)
            else:
                self.save('blocked' if unknown or prepared or (self.root/PENDING).exists() else 'check_failed',
                    failed_phase=self.record.get('phase'),failure_type=type(error).__name__,
                    error_code=error.code if isinstance(error,ControlError) else None,unknown_outcome=bool(unknown))
            return self.record

    def recover(self, job):
        record=n.private_json(self.root/ACTIVE)
        if record.get('job')!=job or record.get('phase') in TERMINAL or record.get('commit_sent'):
            raise ControlError('outcome_unknown')
        pending=n.private_json(self.root/PENDING)
        if pending.get('job')!=job or pending.get('phase') not in ('prepared','applying'):
            raise ControlError('outcome_unknown')
        # A lost reply must not race a still-running independent invocation.
        running=self.engine.call('container','ls','--filter','label=dev.ark.job='+job,
            '--filter','label=dev.ark.purpose','--format','{{.ID}}').strip()
        if running:raise ControlError('outcome_unknown')
        from .ark_docker import verify_files
        from .ark_process import LAUNCH
        verify_files(self.root,pending['files'])
        if (self.root/LAUNCH).exists() and n.private_json(self.root/LAUNCH).get('invocation_id')!=pending['previous_state']['InvocationID']:
            raise ControlError('outcome_unknown')
        attempt=uuid.uuid4().hex
        n.save(self.root/'logs/updates/recoveries'/attempt/'before.json',record)
        self.record=record;self.folder=SOURCE_STAGE/'jobs'/job
        self.save('owner_recovery',recovery_attempt=attempt)
        server=self.engine.inspect(self.engine.project+'-server-1')
        if pending['phase']=='prepared':
            if server is None:raise ControlError('outcome_unknown')
            self.engine.validate(server,'server')
            if not server['State']['Running']:
                if server['State']['OOMKilled'] or server['State']['ExitCode'] not in (0,134):raise ControlError('outcome_unknown')
                self.engine.api('POST','/containers/'+server['Id']+'/start')
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    if call({'method':'status'},timeout=5)['ok']:break
                    time.sleep(1)
                else:raise ControlError('outcome_unknown')
            result=call({'method':'abort_update','job':job})
            if not result['ok']:raise ControlError('outcome_unknown')
            self.save('candidate_rejected',owner_recovered_unchanged_release=True)
            return self.record
        images=record['images']
        for identity in images.values():self.engine.image(identity)
        if server is not None:
            self.engine.validate(server,'server')
            if server['State']['Running']:
                self.engine.api('POST','/containers/'+server['Id']+'/stop?t=1500')
            server=self.engine.inspect(self.engine.project+'-server-1')
            if server['State']['OOMKilled'] or server['State']['ExitCode'] not in (0,134):raise ControlError('outcome_unknown')
        else:
            archive=self.root/'backups'/('update-'+job)
            preserved=archive/'controller-before-apply.json'
            if preserved.exists():server=n.private_json(preserved)
            else:
                candidates=[n.private_json(path) for path in archive.glob('containers/*/server-previous-container.json')]
                candidates=[value for value in candidates if value['Id']==record['old_containers']['server']]
                if len(candidates)!=1:raise ControlError('outcome_unknown')
                server=candidates[0]
            if (server['Id']!=record['old_containers']['server'] or server['State']['Status']!='exited'
                    or server['State']['OOMKilled'] or server['State']['ExitCode'] not in (0,134)):
                raise ControlError('outcome_unknown')
        self.apply(images['server'],recovering=True)
        for role,identity in images.items():self.engine.call('image','tag',identity,self.engine.project+'-'+role+':local')
        archive=self.root/'backups'/('update-'+job)
        replacement=self.engine.replace(server,'server',images['server'],job,archive,allow_missing=True)
        self.save('new_controller_started',replacement_server=replacement,owner_requested_recovery=True)
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if call({'method':'status'},timeout=5)['ok']:break
            time.sleep(1)
        else:raise ControlError('outcome_unknown')
        self.save('commit_requested',commit_sent=True)
        result=call({'method':'commit_update','job':job})
        if not result['ok']:raise ControlError('outcome_unknown')
        self.save('committed',owner_requested_recovery=True,saved_restored=False)
        self.finish_services(images,archive)
        return self.record

    def shutdown(self):
        self.external_shutdown=True
        self.closed=True
        job=self.record['job'] if self.record is not None else self.handoff_job
        if job is not None:mark_shutdown(self.root,job)

    def quiesce_replacement(self):
        # Compose may have captured the OLD container IDs before asking the
        # updater to stop. Quiesce our newer replacement too, through its
        # guarded controller, so a concurrent `compose down` cannot orphan ARK.
        from .compose_platform import state
        try:all_roles=state()['status']=='closing'
        except (OSError,ValueError):all_roles=False
        if not self.record and not all_roles:return
        for role in ('server','bot','updater'):
            value=self.engine.inspect(self.engine.project+'-'+role+'-1')
            if (value and (all_roles or value['Config'].get('Labels',{}).get('dev.ark.update')==self.record['job'])
                    and value['State']['Running'] and not value['Id'].startswith(socket.gethostname())):
                self.engine.validate(value,role)
                self.engine.api('POST','/containers/'+value['Id']+'/stop?t=1500')

    def run(self):
        from .compose_platform import active,state
        import threading
        generation=active()
        def watch():
            while not self.closed:
                try:closing=state(generation)['status']=='closing'
                except (OSError,ValueError):closing=True
                if closing:self.shutdown();return
                time.sleep(.25)
        threading.Thread(target=watch,daemon=True).start()
        next_check=0
        n.save(self.root/READY,{'hostname':socket.gethostname(),'boot_id':n.boot_id(),'started_monotonic':self.started})
        while not self.closed:
            if self.handoff_job and platform_stopped(self.root,self.handoff_job,self.started):
                self.shutdown();break
            request_path=self.root/REQUEST
            if request_path.exists():
                request=n.private_json(request_path)
                import re
                action=request.get('action')
                shape={'id','action','job'} if action=='recover' else {'id','action','source'} if action=='validate-source' else {'id','action'}
                if (set(request)!=shape or action not in ('check','validate-current','validate-source','recover')
                        or not isinstance(request.get('id'),str) or not re.fullmatch('[0-9a-f]{32}',request['id'])
                        or action=='recover' and (not isinstance(request.get('job'),str) or not re.fullmatch('[0-9a-f]{32}',request['job']))
                        or action=='validate-source' and (not isinstance(request.get('source'),str) or not re.fullmatch('source/[0-9a-f]{64}',request['source']))):
                    raise ValueError('Malformed explicit update request.')
                history=self.root/'logs/updates/requests'/(request['id']+'.json')
                if history.exists():raise ControlError('outcome_unknown')
                n.save(history,request|{'phase':'accepted'})
                request_path.unlink();c.sync_directory(request_path.parent)
                if request['action']=='recover':
                    try:result=self.recover(request['job'])
                    except Exception:
                        if self.record is not None:self.save('blocked',automatic_retry_performed=False)
                        result={'phase':'blocked','reason':'preserved_state_does_not_allow_recovery'}
                else:result=self.perform(force=request['action']=='validate-current',source=request.get('source'))
                n.save(history,request|{'phase':'completed','result':result})
                next_check=time.monotonic()+settings(self.root)['update_interval_seconds']
            elif time.monotonic()>=next_check:
                self.perform()
                next_check=time.monotonic()+settings(self.root)['update_interval_seconds']
            time.sleep(1)
        if self.external_shutdown:self.quiesce_replacement()


def complete_handoff(job):
    import re
    if not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job):raise ValueError('Invalid handoff job.')
    root=n.CODE_ROOT;engine=ComposeEngine(os.environ['ARK_PROJECT'])
    record=n.private_json(root/ACTIVE)
    if record.get('job')!=job or record.get('phase')!='handoff':raise ControlError('outcome_unknown')
    archive=root/'backups'/('update-'+job)
    def save(phase,**extra):
        record.update(phase=phase,**extra);persist(root,record)
    try:
        old=engine.current('updater')
        if old['Id']!=record['old_updater']:raise ControlError('outcome_unknown')
        deadline=time.monotonic()+60
        while old['State']['Running'] and time.monotonic()<deadline:
            time.sleep(1);old=engine.current('updater')
        if old['State']['Running']:raise ControlError('outcome_unknown')
        if platform_stopped(root,job):
            save('platform_stopped',updater_handoff_completed=False);return
        save('handoff_replacing')
        identity=engine.replace(old,'updater',record['next_updater_image'],job,archive)
        record['replacement_updater']=identity
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            value=engine.current('updater')
            if value['Id']!=identity or not value['State']['Running']:raise ControlError('outcome_unknown')
            ready=n.private_json(root/READY) if (root/READY).exists() else {}
            if ready.get('hostname')==value['Config']['Hostname'] and ready.get('boot_id')==n.boot_id():break
            time.sleep(1)
        else:raise ControlError('outcome_unknown')
        if platform_stopped(root,job):
            engine.api('POST','/containers/'+identity+'/stop?t=1500')
            save('platform_stopped',updater_handoff_completed=True);return
        if not engine.current('server')['State']['Running'] or not call({'method':'status'},timeout=10)['ok']:
            raise ControlError('outcome_unknown')
        save('completed',updater_handoff_completed=True,saved_restored=False)
        n.save(archive/'updater-handoff.json',{'phase':'completed','saved_restored':False})
    except Exception as error:
        save('blocked',failed_phase=record.get('phase'),failure_type=type(error).__name__,updater_handoff_completed=False)
        raise
    finally:
        if platform_stopped(root,job):
            value=engine.inspect(engine.project+'-updater-1')
            if value and value['State']['Running'] and value['Config'].get('Labels',{}).get('dev.ark.update')==job:
                engine.validate(value,'updater');engine.api('POST','/containers/'+value['Id']+'/stop?t=1500')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('run','status','check','validate-current','validate-source','recover','handoff'))
    parser.add_argument('job',nargs='?')
    parser.add_argument('--confirm',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    if args.action=='status':
        record=n.private_json(n.CODE_ROOT/ACTIVE) if (n.CODE_ROOT/ACTIVE).exists() else {'phase':'not_checked'}
        print(json.dumps(record));return 0
    if args.action=='handoff':complete_handoff(args.job);return 0
    if args.action!='run':
        if args.action in ('validate-current','validate-source','recover') and not args.confirm:
            raise ControlError('confirmation_required')
        print(json.dumps({'accepted':True,'request':submit(args.action,args.job)}));return 0
    from .compose_server import rootless
    rootless()
    (n.CODE_ROOT/'logs/updates').mkdir(mode=0o700,parents=True,exist_ok=True)
    with exclusive_lock(n.CODE_ROOT/'logs/updates/updater.lock'):
        updater=Updater()
        for number in (signal.SIGINT,signal.SIGTERM):
            signal.signal(number,lambda number,frame:updater.shutdown())
        updater.run()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Compose updater blocked; private evidence retained. No automatic retry of an uncertain operation.',file=sys.stderr)
        raise SystemExit(70)
