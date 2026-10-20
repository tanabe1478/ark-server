"""Docker contracts only. No daemon, systemd, game or network calls."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import ark_config, ark_shutdown as native, ark_docker as d


class DockerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        ark_config.apply_config(self.root,{'settings':ark_config.DEFAULTS.copy(),'password':'SYNTHETIC_PARTICIPANT','admin_password':'SYNTHETIC_ADMIN'})
        binary=self.root/native.BINARY;binary.parent.mkdir(parents=True);binary.write_bytes(b'FAKE_DO_NOT_EXECUTE')
        patcher=patch.object(native,'BINARY_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest())
        patcher.start();self.addCleanup(patcher.stop);native.install_policy(self.root)
        self.config={'schema':1,'deployment_id':'a'*32,'container_name':'ark-fixture','image':'sha256:'+'b'*64,
                     'source_contract':d.source_contract(),'bind_address':'127.0.0.1'}
        native.save(self.root/d.CONFIG,self.config)
        self.stopped={'ActiveState':'inactive','MainPID':0,'Result':'success','ExecMainStatus':0,'ExecMainCode':0,'InvocationID':''}
        self.backend=SimpleNamespace(instance=lambda:None)
        self.guard=d.DockerShutdownGuard(self.root,self.backend)
        self.obj={'Id':'c'*64,'Image':self.config['image'],'Config':{'StopSignal':'SIGINT','Labels':{
            'dev.ark.deployment':'a'*32,'dev.ark.invocation':'e'*32,'dev.ark.contract':d.contract_id(self.config['source_contract'])}},
            'HostConfig':{'RestartPolicy':{'Name':'no'},'Privileged':False,'CapAdd':None,'CapDrop':['ALL'],
                'ReadonlyRootfs':True,'Init':True,'SecurityOpt':['no-new-privileges'],'NetworkMode':'bridge','IpcMode':'private',
                'PidMode':'','PortBindings':{str(p)+'/udp':[{'HostIp':'127.0.0.1','HostPort':str(p)}] for p in (7777,7778,27015)}},
            'Mounts':[{'Type':'bind','Source':str(src),'Destination':dest,'RW':rw} for src,dest,rw in d.mounts(self.root)]}
        # Accidental real calls fail loudly.
        patcher=patch.object(d.Engine,'call',side_effect=AssertionError('No real Docker'))
        patcher.start();self.addCleanup(patcher.stop)

    def test_runtime_load_and_typed_schema(self):
        self.assertEqual(d.load(self.root),self.config)
        for schema in (True,1.0,'1'):
            native.save(self.root/d.CONFIG,self.config|{'schema':schema})
            with self.assertRaises(native.ShutdownError):d.load(self.root)

    def test_source_change_and_pending_cutover_block(self):
        native.save(self.root/d.CONFIG,self.config|{'source_contract':{}})
        with self.assertRaises(native.ShutdownError):d.load(self.root)
        native.save(self.root/d.CONFIG,self.config)
        native.save(self.root/'config/docker-cutover.pending.json',{'incomplete':True})
        with self.assertRaises(native.ShutdownError):d.load(self.root)
        with self.assertRaises(ark_config.ConfigRecoveryError):ark_config.launch_url(self.root)

    def test_instance_security_contract(self):
        d.validate_instance(self.root,self.config,self.obj)
        mutations=[('Privileged',True),('CapAdd',['SYS_PTRACE']),('ReadonlyRootfs',False),('Init',False),
                   ('SecurityOpt',['no-new-privileges','seccomp=unconfined']),('NetworkMode','host'),
                   ('PidMode','host'),('IpcMode','host'),('Devices',[{}]),('DeviceRequests',[{}])]
        for key,value in mutations:
            obj=copy.deepcopy(self.obj);obj['HostConfig'][key]=value
            with self.subTest(key=key),self.assertRaises(native.ShutdownError):d.validate_instance(self.root,self.config,obj)

    def test_extra_mount_wrong_image_and_extra_port_rejected(self):
        for change in ('mount','image','port'):
            obj=copy.deepcopy(self.obj)
            if change=='mount':obj['Mounts'].append({'Type':'volume','Destination':'/extra'})
            elif change=='image':obj['Image']='sha256:'+'f'*64
            else:obj['HostConfig']['PortBindings']['27020/tcp']=[{'HostPort':'27020'}]
            with self.subTest(change=change),self.assertRaises(native.ShutdownError):d.validate_instance(self.root,self.config,obj)

    def test_generated_command_exposes_only_game_udp_and_no_secrets(self):
        args=d.create_args(self.root,self.config,'e'*32)
        for arg in ('--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--restart=no','--network=bridge'):self.assertIn(arg,args)
        ports=[args[i+1] for i,a in enumerate(args) if a=='--publish']
        self.assertEqual(ports,['127.0.0.1:7777:7777/udp','127.0.0.1:7778:7778/udp','127.0.0.1:27015:27015/udp'])
        command=' '.join(args)
        for forbidden in ('discord-bot.token','/var/run/docker.sock','SYNTHETIC_PARTICIPANT','SYNTHETIC_ADMIN','--privileged'):self.assertNotIn(forbidden,command)

    def test_expected_exit_requires_exact_typed_native_transport_result(self):
        state={'ActiveState':'failed','Result':'exit-code','MainPID':0,'ExecMainStatus':134,'ExecMainCode':1,
               'ContainerStatus':'exited','ContainerExitCode':134,'ContainerOOMKilled':False}
        self.assertTrue(d.exited_as_expected(state))
        for key,value in [('MainPID',False),('ExecMainStatus',134.0),('ExecMainCode',True),('ContainerExitCode',134.0),
                          ('ContainerOOMKilled',True),('ContainerOOMKilled',0),('Result','timeout'),('ContainerExitCode',137)]:
            with self.subTest(key=key,value=value):self.assertFalse(d.exited_as_expected(state|{key:value}))

    def approve(self):
        d.approve_cutover(self.root,self.config,{'synthetic_review':True})

    def test_first_launch_uses_explicit_bound_review(self):
        with self.assertRaises((OSError,native.ShutdownError)):self.guard.verify_startable(self.stopped)
        self.approve();self.assertEqual(self.guard.verify_startable(self.stopped)['warning'],'reviewed_runtime_cutover')
        with self.assertRaises(native.ShutdownError):self.guard.verify_startable(self.stopped|{'MainPID':False})
        with self.assertRaises(native.ShutdownError):self.guard.verify_startable(self.stopped|{'ActiveState':'active'})

    def test_missing_previous_container_never_reuses_bootstrap(self):
        self.approve();native.save(self.root/d.STATE/'launch.json',{'phase':'issued_before_crash'})
        with self.assertRaises(native.ShutdownError):self.guard.verify_startable(self.stopped)

    def test_bootstrap_saved_native_instance_and_schema_changes_block(self):
        for change in ('saved','native_instance','schema'):
            self.approve()
            if change=='saved':
                target=self.root/native.SAVED/'new.arkprofile';target.write_bytes(b'CHANGED')
            elif change=='native_instance':native.save(self.root/native.DIRECTORY/'live.json',{'run_id':'f'*32})
            else:
                p=self.root/d.STATE/'bootstrap.json';value=native.private_json(p);native.save(p,value|{'schema':True})
            with self.subTest(change=change),self.assertRaises(native.ShutdownError):self.guard.verify_startable(self.stopped)
            if change=='saved':target.unlink()
            if change=='native_instance':(self.root/native.DIRECTORY/'live.json').unlink()

    def test_engine_error_not_misclassified_as_missing(self):
        e=d.Engine()
        with patch.object(e,'call',side_effect=native.ShutdownError('unavailable')):
            with self.assertRaises(native.ShutdownError):e.inspect('ark-fixture')

    def test_local_lease_fault_invalidates_observation(self):
        native.save(self.root/native.DIRECTORY/'live.json',{'run_id':'e'*32})
        native.save(self.root/native.DIRECTORY/'runtime'/('e'*32+'.json'),{'phase':'exited'})
        native.save(self.root/d.STATE/'lease-fault.json',{'run_id':'e'*32})
        with self.assertRaises(native.ShutdownError):self.guard.observation()


if __name__=='__main__':unittest.main()
