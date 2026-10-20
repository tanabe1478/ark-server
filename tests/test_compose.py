import copy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zlib

from scripts import ark_config as c, ark_shutdown as n, ark_process as p
from scripts.ark_spawn import PersistentSpawner
from scripts import compose_assets as assets, compose_integrity as integrity
from scripts.compose_profiles import matches
from scripts.compose_engine import ComposeEngine, TARGETS
from scripts.compose_update import Updater, ACTIVE
from scripts.compose_release import manifest, EXTRA

ROOT=Path(__file__).resolve().parents[1]


class SpawnerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='linux','Linux parent-death signal semantics')
    def test_request_thread_exit_does_not_kill_persistent_spawn(self):
        program='import ctypes,signal,time; ctypes.CDLL(None).prctl(1,signal.SIGKILL,0,0,0); print("ready",flush=True); time.sleep(20)'
        def launch(spawn):
            result=[]
            def request():
                child=spawn([sys.executable,'-u','-c',program],stdout=subprocess.PIPE,text=True)
                self.assertEqual(child.stdout.readline().strip(),'ready')
                result.append(child)
            thread=threading.Thread(target=request);thread.start();thread.join(10)
            self.assertFalse(thread.is_alive())
            return result[0]
        direct=launch(subprocess.Popen)
        try:self.assertEqual(direct.wait(timeout=5),-signal.SIGKILL)
        finally:
            if direct.poll() is None:direct.kill();direct.wait()
            direct.stdout.close()
        stable=PersistentSpawner()
        child=launch(stable)
        try:
            time.sleep(.1)
            self.assertIsNone(child.poll())
        finally:
            child.terminate();child.wait(timeout=5);child.stdout.close();stable.close()


class FilesFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def file(self,name,data=b'data'):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o600);return path


class FileTests(FilesFixture):
    def test_asset_copy_never_overwrites_saved(self):
        source=self.root/'source';destination=self.root/'destination'
        self.file('source/ShooterGame/Binaries/game',b'program')
        self.file('source/ShooterGame/Saved/world.ark',b'do-not-copy')
        target=self.file('destination/ShooterGame/Saved/world.ark',b'newer-progress')
        assets.copy_assets(source,destination)
        self.assertEqual(target.read_bytes(),b'newer-progress')
        self.assertEqual((destination/'ShooterGame/Binaries/game').read_bytes(),b'program')

    def test_asset_copy_rejects_existing_program(self):
        self.file('source/bin');self.file('destination/bin')
        with self.assertRaises(ValueError):assets.copy_assets(self.root/'source',self.root/'destination')

    def test_asset_copy_rejects_external_links(self):
        outside=self.file('outside',b'private')
        source=self.root/'source';source.mkdir();(source/'link').symlink_to(outside)
        with self.assertRaises(ValueError):assets.copy_assets(source,self.root/'destination')

    def test_asset_manifest_rejects_external_links(self):
        outside=self.file('outside')
        self.file('source/bin');(self.root/'source/link').symlink_to(outside)
        with self.assertRaises(ValueError):assets.tree_manifest(self.root/'source')

    def test_asset_copy_rejects_special_files(self):
        source=self.root/'source';source.mkdir();os.mkfifo(source/'pipe')
        with self.assertRaises(ValueError):assets.copy_assets(source,self.root/'destination')

    def archive(self,name='file',data=b'text',kind=None):
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w:gz') as tar:
            info=tarfile.TarInfo(name);info.size=len(data)
            if kind is not None:info.type=kind;info.linkname='outside';info.size=0
            tar.addfile(info,io.BytesIO(data) if info.size else None)
        return out.getvalue()

    def test_tar_extract_regular(self):
        assets.extract_tar(self.archive('a/file'),self.root/'out')
        self.assertEqual((self.root/'out/a/file').read_bytes(),b'text')

    def test_tar_rejects_traversal_absolute_and_links(self):
        for index,(name,kind) in enumerate((('../escape',None),('/escape',None),('link',tarfile.SYMTYPE),('hard',tarfile.LNKTYPE))):
            with self.subTest(name=name),self.assertRaises(ValueError):
                assets.extract_tar(self.archive(name,kind=kind),self.root/str(index))

    def test_tar_expansion_limit(self):
        with self.assertRaises(ValueError):assets.extract_tar(self.archive(data=b'x'*100),self.root/'out',max_bytes=99)

    def test_fstring_ascii_and_utf16(self):
        for value in ('A map','Mód','海のMOD','emoji '+chr(0x1f40b)):
            self.assertEqual(assets.read_string(io.BytesIO(assets.string(value))),value)
        for value in (b'',struct.pack('<i',0),struct.pack('<i',4097),struct.pack('<i',2)+b'xx'):
            with self.assertRaises(ValueError):assets.read_string(io.BytesIO(value))

    def compressed(self,payload,block=64):
        chunks=[payload[i:i+block] for i in range(0,len(payload),block)]
        compressed=[zlib.compress(x) for x in chunks]
        return (struct.pack('<QQQQ',0x9E2A83C1,block,sum(map(len,compressed)),len(payload))
                +b''.join(struct.pack('<QQ',len(a),len(b)) for a,b in zip(compressed,chunks))+b''.join(compressed))

    def test_bounded_ue4_mod_decompression(self):
        payload=b'synthetic asset data'*100
        source=self.file('asset.z',self.compressed(payload));out=self.root/'out'
        assets.decompress(source,out);self.assertEqual(out.read_bytes(),payload)

    def test_ue4_rejects_truncation_and_false_totals(self):
        for index,raw in enumerate((b'x',self.compressed(b'abc')[:-1],struct.pack('<QQQQ',0x9E2A83C1,1,1,assets.MAX_FILE+1))):
            source=self.file('bad'+str(index),raw)
            with self.assertRaises((ValueError,zlib.error)):assets.decompress(source,self.root/('out'+str(index)))

    def test_ue4_rejects_decompression_bomb(self):
        compressed=zlib.compress(b'x'*1000000)
        raw=struct.pack('<QQQQQQ',0x9E2A83C1,1,len(compressed),1,len(compressed),1)+compressed
        with self.assertRaises(ValueError):assets.decompress(self.file('bomb',raw),self.root/'out')

    def test_installed_mod_header_and_expanded_content(self):
        cache=self.root/'cache';folder=cache/'42';folder.mkdir(parents=True)
        (folder/'mod.info').write_bytes(assets.string('InternalMod')+struct.pack('<I',1)+assets.string('DemoMap'))
        (folder/'modmeta.info').write_bytes(struct.pack('<I',0))
        branch=folder/'WindowsNoEditor';branch.mkdir();(branch/'demo.z').write_bytes(self.compressed(b'mod asset'))
        linux=folder/'LinuxNoEditor';linux.mkdir();(linux/'demo.z').write_bytes(self.compressed(b'wrong platform cook'))
        destination=self.root/'mods'
        assets.install_mod(cache,destination,'42','Mód Demo')
        self.assertEqual((destination/'42/demo').read_bytes(),b'mod asset')
        stream=io.BytesIO((destination/'42.mod').read_bytes())
        self.assertEqual(struct.unpack('<Q',stream.read(8))[0],42)
        self.assertEqual(assets.read_string(stream),'Mód Demo')
        self.assertEqual(assets.read_string(stream),'../../../ShooterGame/Content/Mods/42')
        self.assertEqual(struct.unpack('<I',stream.read(4))[0],1)
        self.assertEqual(assets.read_string(stream),'DemoMap')
        self.assertEqual(struct.unpack('<IIB',stream.read(9)),(0xFF22FF33,2,1))

    def test_vdf_nested_and_duplicates(self):
        self.assertEqual(assets.vdf('noise "376030" { "depots" { "branches" { "public" { "buildid" "123" } } } } tail','376030')['depots']['branches']['public']['buildid'],'123')
        with self.assertRaises(ValueError):assets.vdf('"a" "x" "a" "y"')
        with self.assertRaises(ValueError):assets.vdf('"a" { "b" "x"')

    def test_ini_duplicate_arrays_are_not_collapsed(self):
        path=self.file('Game.ini',b'[Server]\nArray=(a=1)\nArray=(a=1)\nArray=(a=2)\n')
        values=integrity.entries(path)
        self.assertEqual(values[('server','array','(a=1)')],2)
        self.assertEqual(sum(values.values()),3)

    def test_ini_startup_preserves_all_entries_and_credentials(self):
        c.apply_config(self.root,{'settings':dict(c.DEFAULTS),'password':'EXAMPLE_PARTICIPANT','admin_password':'EXAMPLE_ADMINISTRATOR'})
        game=self.file(str(c.GAME_INI_PATH),b'[Server]\nArray=(a=1)\nArray=(a=1)\n')
        before=integrity.capture(self.root)
        game.write_text(game.read_text()+'Extra=default\n')
        integrity.verify(self.root,before)
        game.write_text('[Server]\nArray=(a=1)\n')
        with self.assertRaisesRegex(ValueError,'removed or changed'):integrity.verify(self.root,before)

    def test_documented_numeric_default_can_be_elided_but_custom_value_cannot(self):
        c.apply_config(self.root,{'settings':dict(c.DEFAULTS),'password':'EXAMPLE_PARTICIPANT','admin_password':'EXAMPLE_ADMINISTRATOR'})
        ini=self.root/c.INI_PATH
        text=ini.read_text()+'\n[Extra]\nKeep=value\n'
        text=text.replace('[ServerSettings]','[ServerSettings]\nDayCycleSpeedScale=1')
        ini.write_text(text);before=integrity.capture(self.root)
        ini.write_text(text.replace('DayCycleSpeedScale=1\n',''))
        integrity.verify(self.root,before)
        ini.write_text(text.replace('DayCycleSpeedScale=1','DayCycleSpeedScale=2'))
        before=integrity.capture(self.root)
        ini.write_text(text.replace('DayCycleSpeedScale=1\n',''))
        with self.assertRaisesRegex(ValueError,'numeric setting'):integrity.verify(self.root,before)

    def test_startup_progress_rejects_large_world_shrink_or_missing_roster(self):
        c.apply_config(self.root,{'settings':dict(c.DEFAULTS),'password':'EXAMPLE_PARTICIPANT','admin_password':'EXAMPLE_ADMINISTRATOR'})
        world=self.file(str(n.SAVED/'SavedArks/Genesis.ark'),b'x'*10000)
        profile=self.file(str(n.SAVED/'SavedArks/42.arkprofile'),b'profile')
        baseline=integrity.progress(self.root)
        world.write_bytes(b'x'*9000);integrity.verify_progress(self.root,baseline)
        world.write_bytes(b'x'*8999)
        with self.assertRaisesRegex(ValueError,'shrank'):integrity.verify_progress(self.root,baseline)
        world.write_bytes(b'x'*10000);profile.unlink()
        with self.assertRaisesRegex(ValueError,'profile or tribe'):integrity.verify_progress(self.root,baseline)

    def test_configuration_recovery_never_restores_changed_world(self):
        from scripts.compose_configure import check_non_ini
        self.file(str(n.SAVED/'SavedArks/Genesis.ark'),b'x'*10000)
        ini=self.file(str(c.INI_PATH),b'[S]\nK=old\n')
        original=n.capture(self.root/n.SAVED,self.root/'snapshot')
        ini.write_bytes(b'[S]\nK=new\n');check_non_ini(self.root,original)
        (self.root/n.SAVED/'SavedArks/Genesis.ark').write_bytes(b'y'*10000)
        with self.assertRaises(ValueError):check_non_ini(self.root,original)

    def test_private_preparation_check_does_not_prompt_or_connect(self):
        from scripts import compose_init
        template=json.loads((n.CODE_ROOT/'examples/compose-bootstrap.example.json').read_bytes())
        template.update(password='EXAMPLE_PARTICIPANT_123',admin_password='EXAMPLE_ADMINISTRATOR_456')
        template['discord']={'guild_id':'555555555555555555','notification_channel_id':'666666666666666666'}
        n.save(self.root/'config/compose/bootstrap.json',template)
        self.file('config/compose/discord-bot.token',b'a'*32+b'\n')
        with patch.object(compose_init.getpass,'getpass',side_effect=AssertionError('must not prompt')):
            self.assertEqual(compose_init.prepare(self.root,check=True),{'configuration_valid':True,'discord_connection_tested':False})
        (self.root/'config/compose').chmod(0o755)
        with self.assertRaises(ValueError):compose_init.prepare(self.root,check=True)

    def test_live_qualification_timeout_holds_parent_instead_of_pdeath_kill(self):
        from scripts import compose_qualify as q
        n.save(self.root/'logs/process/state.json',{'phase':'starting','supervisor_returncode':None})
        n.save(self.root/'logs/process/launch.json',{'schema':1})
        with patch.object(n,'CODE_ROOT',self.root),patch.object(q,'qualify',side_effect=TimeoutError),patch.object(n,'save') as save,patch.object(q.time,'sleep',side_effect=KeyboardInterrupt) as held:
            with self.assertRaises(KeyboardInterrupt):q.main()
        held.assert_called_once_with(1)
        self.assertTrue(save.call_args.args[1]['native_launch_intent_created'])
        self.assertIsNone(save.call_args.args[1]['last_process_state']['supervisor_returncode'])

    def test_hidden_token_warning_cannot_fall_back_to_echo(self):
        from scripts import compose_init
        import warnings
        n.save(self.root/'config/compose/bootstrap.json',{})
        def unsafe_fallback(*args):
            warnings.warn('no hidden terminal',compose_init.getpass.GetPassWarning)
            raise AssertionError('Echo fallback must never be reached')
        with patch.object(compose_init.sys.stdin,'isatty',return_value=True),patch.object(compose_init.getpass,'getpass',side_effect=unsafe_fallback):
            with self.assertRaises(compose_init.getpass.GetPassWarning):compose_init.prepare(self.root)
        self.assertFalse((self.root/'config/compose/discord-bot.token').exists())

    def test_workshop_uses_force_install_dir_not_steam_client_directory(self):
        self.file('stage/server/ShooterGame/Binaries/Linux/ShooterGameServer',b'ELF')
        self.file('stage/server/steamapps/workshop/appworkshop_346110.acf',b'"AppWorkshop" { "WorkshopItemsInstalled" { "42" { "manifest" "123" } } }')
        versions={'build_id':'1','mods':{'42':{'title':'Example','time_updated':1}}}
        with patch.object(assets,'steam',return_value='Success. Downloaded item 42'), \
                patch.object(assets,'workshop_details',return_value=versions['mods']),patch.object(assets,'prepare_sdk'),patch.object(assets,'install_mod') as install:
            assets.download(self.root/'stage',self.root/'steam',{'schema':1,'game':False,'mods':['42'],'versions':versions})
        self.assertEqual(install.call_args.args[0],self.root/'stage/server/steamapps/workshop/content/346110')

    def test_asset_size_estimate_excludes_saved(self):
        self.file('source/ShooterGame/Binaries/game',b'game')
        self.file('source/ShooterGame/Saved/world',b'private world')
        self.assertEqual(assets.asset_bytes(self.root/'source'),4)

    def test_ini_utf16_and_utf8_bom(self):
        for index,data in enumerate(('[S]\nK=海\n'.encode('utf-16'),'\ufeff[S]\nK=海\n'.encode())):
            self.assertEqual(integrity.entries(self.file(str(index),data))[('s','k','海')],1)

    def test_release_identity_excludes_installation_and_git(self):
        for name in EXTRA:self.file(name,b'public')
        self.file('scripts/code.py',b'# public\n');self.file('scripts/trace.gdb',b'quit\n')
        first=manifest(self.root)
        self.file('config/private.json',b'private');self.file('.git/config',b'private')
        self.assertEqual(manifest(self.root),first)
        self.file('scripts/code.py',b'# changed\n')
        self.assertNotEqual(manifest(self.root)['digest'],first['digest'])

    def test_owner_source_is_digest_bound_public_export_only(self):
        from scripts import compose_update as u
        for name in EXTRA:self.file('stage/bundle/'+name,b'public\n')
        self.file('stage/bundle/scripts/code.py',b'# public\n')
        source=self.root/'stage/bundle';release=manifest(source)
        destination=self.root/'stage/source'/release['digest'];destination.parent.mkdir();source.rename(destination)
        updater=Updater(self.root,Mock())
        with patch.object(u,'SOURCE_STAGE',self.root/'stage'):
            self.assertEqual(updater.owner_source('source/'+release['digest'])['release'],release)
            self.file(str(destination.relative_to(self.root)/'config/private.json'),b'private')
            with self.assertRaises(ValueError):updater.owner_source('source/'+release['digest'])
            with self.assertRaises(ValueError):updater.owner_source('source/../escape')

    def test_only_explicit_prelaunch_failure_can_resume_unchanged_release(self):
        from scripts.compose_update import definite_prelaunch_failure as safe
        self.assertTrue(safe({'native_launch_intent_created':False,'last_process_state':None}))
        for detail in ({},{'native_launch_intent_created':False}, {'native_launch_intent_created':True,'last_process_state':None},
                       {'native_launch_intent_created':False,'last_process_state':{}},None):
            with self.subTest(detail=detail):self.assertFalse(safe(detail))

    def test_recovery_distinguishes_main_controller_from_running_job_helper(self):
        from scripts import compose_update as u
        job='a'*32;n.save(self.root/u.ACTIVE,{'schema':1,'job':job,'phase':'new_controller_started','commit_sent':False})
        n.save(self.root/u.PENDING,{'job':job,'phase':'prepared','files':{},'previous_state':{'InvocationID':'b'*32}})
        engine=Mock();engine.project='fixture';engine.call.return_value='';engine.inspect.return_value={'State':{'Running':True}}
        updater=Updater(self.root,engine)
        with patch('scripts.ark_docker.verify_files'),patch.object(u,'call',return_value={'ok':True}):
            self.assertEqual(updater.recover(job)['phase'],'candidate_rejected')
        self.assertIn('label=dev.ark.purpose',engine.call.call_args.args)

    def test_shutdown_cannot_turn_an_unfinished_handoff_into_completed(self):
        updater=Updater(self.root,Mock());updater.record={'schema':1,'job':'a'*32,'phase':'committed'};updater.external_shutdown=True
        updater.finish_services({'bot':'b','updater':'u'},self.root/'archive')
        self.assertEqual(updater.record['phase'],'platform_stopped');self.assertFalse(updater.record['handoff_completed'])
        updater.engine.replace.assert_not_called()

    def test_recovered_source_update_still_requires_updater_handoff(self):
        engine=Mock();engine.current.side_effect=lambda role:{'Image':'old-'+role,'Id':role}
        updater=Updater(self.root,engine);updater.record={'schema':1,'job':'a'*32,'phase':'committed'};updater.handoff=Mock()
        updater.finish_services({'bot':'new-bot','updater':'new-updater'},self.root/'archive')
        updater.handoff.assert_called_once_with('new-updater',{'Image':'old-updater','Id':'updater'})
        self.assertTrue(updater.closed);self.assertNotEqual(updater.record['phase'],'completed')

    def test_platform_witness_is_generation_bound_and_typed(self):
        from scripts import compose_platform as p
        value={'schema':1,'generation':'a'*32,'boot_id':'boot','status':'active','heartbeat':100.0}
        with patch.object(p,'STATE',self.root/'platform/state.json'),patch.object(p,'boot',return_value='boot'),patch.object(p.time,'monotonic',return_value=101):
            p.write(p.STATE,value);self.assertEqual(p.active(),'a'*32)
            with self.assertRaises(ValueError):p.active('b'*32)
            for change in ({'schema':True},{'heartbeat':False},{'heartbeat':90},{'boot_id':'old'}):
                p.write(p.STATE,value|change)
                with self.subTest(change=change),self.assertRaises(ValueError):p.state()
            p.write(p.STATE,value|{'status':'closing','heartbeat':0})
            self.assertEqual(p.state()['status'],'closing')
            with self.assertRaises(ValueError):p.active()

    def test_platform_drain_must_match_current_controller_not_an_old_replacement(self):
        from scripts import compose_platform as p
        with patch.object(p,'EVIDENCE',self.root/'evidence'):
            p.write(p.EVIDENCE/'broker-health.json',{'platform_generation':'a'*32,'hostname':'new','closing':True,'broker_run_id':'c'*32})
            p.write(p.EVIDENCE/'platform-drained.json',{'platform_generation':'a'*32,'hostname':'old','stopped':True,'broker_run_id':'c'*32})
            self.assertFalse(p.drained('a'*32))
            p.write(p.EVIDENCE/'platform-drained.json',{'platform_generation':'a'*32,'hostname':'new','stopped':True,'broker_run_id':'c'*32})
            self.assertTrue(p.drained('a'*32));self.assertFalse(p.drained('b'*32))
            p.write(p.EVIDENCE/'platform-drained.json',{'platform_generation':'a'*32,'hostname':'new','stopped':True,'broker_run_id':'d'*32})
            self.assertFalse(p.drained('a'*32))

    def test_uncertain_update_is_not_automatically_replayed(self):
        n.save(self.root/ACTIVE,{'schema':1,'job':'a'*32,'phase':'blocked'})
        engine=Mock()
        updater=Updater(self.root,engine)
        self.assertEqual(updater.perform()['reason'],'previous_update_outcome_unknown')
        engine.current.assert_not_called()


class ProfileTests(unittest.TestCase):
    def test_relocation_mask_does_not_mask_control_flow(self):
        block=b'ABC'+b'1234'+b'DEF'
        normalized=b'ABC'+b'\0'*4+b'DEF'
        pattern={'prefix':b'ABC'.hex(),'length':len(block),'delta':0,'zero_ranges':[(3,4)],'sha256':hashlib.sha256(normalized).hexdigest()}
        self.assertEqual(matches(b'prefix'+block,pattern),[6])
        self.assertEqual(matches(b'ABCxxxxDEF',pattern),[0])
        self.assertEqual(matches(b'ABCxxxxDEG',pattern),[])
        self.assertEqual(matches(block+block,pattern),[0,len(block)])

    def test_compose_policy_rejects_boolean_schema_and_enabled_integer(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            expected={'schema':2,'profile':'test','enabled':True,'native_points':{'force_abort':100,'exit_caller':200}}
            with patch.dict(os.environ,{'ARK_COMPOSE':'1'}),patch('scripts.compose_profiles.expected',return_value=expected):
                for field,value in (('schema',True),('enabled',1)):
                    bad=expected|{field:value};n.save(root/n.POLICY,bad)
                    with self.assertRaises(ValueError):n.policy(root)
                n.save(root/n.POLICY,expected);self.assertEqual(n.policy(root),expected)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.guard=p.ProcessShutdownGuard(self.root,Mock())
        self.runtime={'schema':1,'deployment_id':'a'*32};self.policy={'enabled':True};self.config={'ini':'hash'}
        self.current=patch.object(self.guard,'_current',return_value=(self.runtime,self.policy,self.config));self.current.start();self.addCleanup(self.current.stop)
        self.files=patch('scripts.ark_process.verify_files');self.files.start();self.addCleanup(self.files.stop)
        self.state={'ActiveState':'inactive','Result':'not-launched','MainPID':0,'InvocationID':'','Runtime':'compose-process','OOMKilled':False}
        n.save(self.root/p.BOOTSTRAP,{'schema':1,'purpose':'explicit_initial_compose_setup','runtime':self.runtime,'native_policy':self.policy,'configuration':self.config,'files':{}})
    def test_explicit_fresh_bootstrap(self):
        self.assertEqual(self.guard.verify_startable(self.state)['warning'],'reviewed_initial_setup')
    def test_false_pid_is_not_zero(self):
        with self.assertRaises(ValueError):self.guard.verify_startable(self.state|{'MainPID':False})
    def test_bootstrap_boolean_schema_rejected(self):
        value=n.private_json(self.root/p.BOOTSTRAP);value['schema']=True;n.save(self.root/p.BOOTSTRAP,value)
        with self.assertRaises(ValueError):self.guard.verify_startable(self.state)
    def test_launch_ledger_never_falls_back_to_bootstrap(self):
        n.save(self.root/p.LAUNCH,{'invocation_id':'b'*32})
        with self.assertRaises((OSError,ValueError)):self.guard.verify_startable(self.state)
    def test_unknown_state_never_falls_back_to_bootstrap(self):
        n.save(self.root/p.STATE,{'phase':'starting'})
        with self.assertRaises(ValueError):self.guard.verify_startable(self.state)
    def transition(self):
        n.save(self.root/p.TRANSITION,{'schema':1,'purpose':'qualified_update','runtime':self.runtime,'native_policy':self.policy,
            'configuration':self.config,'previous_state':self.state,'files':{},'qualification':{'two_cycles_passed':True}})
    def test_never_started_update_can_remain_stopped_and_start_later(self):
        self.transition()
        self.assertEqual(self.guard.verify_startable(self.state)['warning'],'qualified_runtime_update')
    def test_transition_consumed_by_new_invocation(self):
        self.state=self.state|{'InvocationID':'b'*32};self.transition()
        n.save(self.root/p.LAUNCH,{'invocation_id':'c'*32})
        with self.assertRaises(ValueError):self.guard.verify_startable(self.state)
    def test_owner_configuration_is_explicit_and_cannot_change_runtime(self):
        self.transition();value=n.private_json(self.root/p.TRANSITION)
        value.update(purpose='owner_configuration',explicit_owner_confirmation=True,previous_runtime=self.runtime,previous_native_policy=self.policy)
        n.save(self.root/p.TRANSITION,value)
        self.assertEqual(self.guard.verify_startable(self.state)['warning'],'owner_configuration_change')
        for key,bad in (('explicit_owner_confirmation',1),('previous_runtime',{'different':True}),('previous_native_policy',{})):
            n.save(self.root/p.TRANSITION,value|{key:bad})
            with self.subTest(key=key),self.assertRaises(ValueError):self.guard.verify_startable(self.state)

    def test_non_compose_state_cannot_use_first_start_approval(self):
        with self.assertRaises(ValueError):self.guard.verify_startable(self.state|{'Runtime':'systemd'})

    def test_bare_abort_exit_never_qualifies(self):
        n.save(self.root/p.LAUNCH,{'invocation_id':'b'*32})
        with self.assertRaises((OSError,ValueError)):self.guard.verify_stopped(self.state|{'Result':'signal','SupervisorReturnCode':-6})


class EngineTests(unittest.TestCase):
    def fixture(self,role='server'):
        project='example';engine=ComposeEngine.__new__(ComposeEngine);engine.project=project;engine.image=Mock(return_value={})
        mounts=[{'Destination':target,'Type':'volume','Name':project+'_'+suffix,'RW':writable} for target,(suffix,writable) in TARGETS[role].items()]
        if role=='bot':mounts.append({'Destination':'/opt/ark/config/discord-bot.token','Type':'bind','Source':'/private/token','RW':False})
        value={'Config':{'User':'0:0','Labels':{'com.docker.compose.project':project,'com.docker.compose.service':role,'com.docker.compose.container-number':'1'}},
            'HostConfig':{'Privileged':False,'ReadonlyRootfs':True,'Init':True,'CapDrop':['ALL'],'CapAdd':None,'SecurityOpt':['no-new-privileges:true'],
                'PidMode':'','IpcMode':'private','NetworkMode':project+'_default','Devices':[],'DeviceRequests':None,'GroupAdd':None,
                'RestartPolicy':{'Name':'on-failure','MaximumRetryCount':5} if role=='bot' else {'Name':'no'},'PortBindings':{str(port)+'/udp':[{'HostIp':'127.0.0.1','HostPort':str(port)}] for port in (7777,7778,27015)} if role=='server' else {}},
            'Image':'sha256:'+'a'*64,'Mounts':mounts}
        image_config={'Entrypoint':['/usr/bin/example'],'Cmd':None,'Env':['ARK_COMPOSE=1']}
        engine.image.return_value={'Config':image_config}
        value['Config'].update(Entrypoint=image_config['Entrypoint'],Cmd=None)
        return engine,value
    def test_held_native_job_reports_unknown_without_stopping_helper(self):
        from scripts.ark_control import ControlError
        engine,_=self.fixture();engine.inspect=Mock(return_value=None);engine.api=Mock()
        def command(*args):
            if args[0]=='container':return 'd'*64
            if args[0]=='inspect':return json.dumps([{'State':{'Status':'running'}}])
            return ''
        engine.call=Mock(side_effect=command)
        with self.assertRaises(ControlError):engine.run_job('image','c'*32,'qualification','scripts.compose_qualify',[],[],held=lambda:True)
        engine.api.assert_not_called()
        self.assertFalse(any(call.args[0] in ('stop','kill','rm') for call in engine.call.call_args_list))

    def test_stable_witness_image_is_not_the_automatically_retagged_server_image(self):
        try:import yaml
        except ImportError:self.skipTest('PyYAML deployment-test dependency is unavailable')
        services=yaml.safe_load((n.CODE_ROOT/'compose.yaml').read_bytes())['services']
        self.assertNotEqual(services['platform']['image'],services['server']['image'])
        self.assertEqual(services['platform']['network_mode'],'none')
        self.assertEqual(services['platform']['volumes'],['platform:/run/ark-platform','evidence:/opt/ark/logs:ro'])

    def test_reviewed_server_shape(self):
        engine,value=self.fixture();engine.validate(value,'server')
    def test_rejects_privilege_capability_host_pid_restart_and_tcp(self):
        for key,value in (('Privileged',True),('CapAdd',['SYS_PTRACE']),('PidMode','host'),('RestartPolicy',{'Name':'always'}),('PortBindings',{'1234/tcp':[]})):
            engine,record=self.fixture();record['HostConfig'][key]=value
            with self.subTest(field=key),self.assertRaises(ValueError):engine.validate(record,'server')
    def test_bot_never_receives_engine_or_saved(self):
        for target in ('/run/ark-engine.sock','/opt/ark/server/ShooterGame/Saved'):
            engine,value=self.fixture('bot');value['Mounts'].append({'Destination':target,'Type':'bind','RW':True})
            with self.assertRaises(ValueError):engine.validate(value,'bot')
    def test_service_command_override_is_rejected(self):
        engine,value=self.fixture();value['Config']['Entrypoint']=['/bin/sh']
        with self.assertRaises(ValueError):engine.validate(value,'server')

    def test_replacement_preserves_long_bind_and_validates_before_start(self):
        from types import SimpleNamespace
        engine,old=self.fixture('bot');old.update(Id='a'*64,State={'Status':'exited','ExitCode':0,'OOMKilled':False})
        old['Config'].update(Env=['ARK_DISCORD_OFFLINE=1'],StopTimeout=1500)
        old['HostConfig']['Mounts']=[{'Type':'bind','Source':'/private/token','Target':'/opt/ark/config/discord-bot.token','ReadOnly':True}]
        new=copy.deepcopy(old);new['Id']='b'*64;new['Image']='sha256:'+'b'*64
        engine.endpoint='unix:///test';engine.inspect=Mock(return_value=old)
        engine.call=Mock(side_effect=lambda *args:json.dumps([old if args[1]==old['Id'] else new]) if args[0]=='inspect' else '')
        events=[];original=engine.validate
        def validate(value,role):events.append(('validate',value['Id']));return original(value,role)
        engine.validate=validate
        def api(method,path,value=None):
            if '/create?' in path:
                self.assertEqual(value['HostConfig']['Mounts'],old['HostConfig']['Mounts'])
                self.assertIn('ARK_DISCORD_OFFLINE=1',value['Env'])
                self.assertEqual(value['StopTimeout'],1500)
                return {'Id':new['Id']}
            if path.endswith('/start'):self.assertIn(('validate',new['Id']),events)
        engine.api=Mock(side_effect=api)
        with tempfile.TemporaryDirectory() as folder,patch('scripts.compose_engine.subprocess.run',return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(engine.replace(old,'bot',new['Image'],'c'*32,Path(folder)),new['Id'])

    def test_bot_token_is_not_writable(self):
        engine,value=self.fixture('bot');value['Mounts'][-1]['RW']=True
        with self.assertRaises(ValueError):engine.validate(value,'bot')


class CertificateTests(unittest.TestCase):
    def fixture(self):
        world='SavedArks/Genesis.ark';files={world:{'bytes':10000,'sha256':'a'*64},'SavedArks/42.arkprofile':{'bytes':100,'sha256':'b'*64}}
        runtime={'deployment_id':'a'*32};policy={'native_points':{}}
        cycles=[]
        for index in (1,2):
            invocation=str(index)*32;run=str(index+2)*32
            state={'ActiveState':'failed','Result':'signal','Runtime':'compose-process','MainPID':0,'SupervisorReturnCode':-6,
                   'OOMKilled':False,'PrivateBootstrap':True,'InvocationID':invocation}
            receipt={'schema':4,'clean_exit':False,'warning':'expected_native_abort','service_record':state,'native_policy':policy,'runtime':runtime,
                     'snapshot':'backups/process-shutdown-'+run,'intent':{'run_id':run,'invocation_id':invocation},'observation':{},'world':world}
            cycles.append({'state':state,'receipt':receipt})
        from scripts.compose_qualify import digest_value
        certificate={'schema':1,'two_cycles_passed':True,'progress_shape_verified':True,'image_build':{},'source_contract':{},
                     'runtime':runtime,'native_policy':policy,'in_game_content_verified':False,'cycles':cycles,
                     'input_saved':files,'input_saved_digest':digest_value(files),'input_configuration':{}}
        return certificate,{'files':files,'configuration':{}}

    def verify(self,certificate,pending,shrink=False):
        from scripts import compose_apply as a
        def read(path,*args):
            if path.name=='receipt.json':return next(cycle['receipt'] for cycle in certificate['cycles'] if Path(cycle['receipt']['snapshot']).name==path.parent.name)
            files=copy.deepcopy(pending['files'])
            if shrink and path.parent.name=='after':files['SavedArks/Genesis.ark']['bytes']=8000
            return {'files':files}
        with patch.object(a,'build_identity',return_value={}),patch.object(a,'contract',return_value={}), \
                patch.object(a.n,'private_json',side_effect=read),patch.object(a.n,'observed_proof',return_value=True):
            a.validate_certificate(certificate,pending)

    def test_full_certificate_binding(self):
        certificate,pending=self.fixture();self.verify(certificate,pending)

    def test_false_pid_and_oom_never_qualify(self):
        for field,value in (('MainPID',False),('OOMKilled',True),('OOMKilled',None),('PrivateBootstrap',False),('SupervisorReturnCode',134)):
            certificate,pending=self.fixture();certificate['cycles'][0]['state'][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):self.verify(certificate,pending)

    def test_duplicate_cycles_are_not_two_reloads(self):
        certificate,pending=self.fixture();certificate['cycles'][1]=copy.deepcopy(certificate['cycles'][0])
        with self.assertRaises(ValueError):self.verify(certificate,pending)

    def test_progress_shape_and_original_input_are_required(self):
        certificate,pending=self.fixture()
        with self.assertRaises(ValueError):self.verify(certificate|{'progress_shape_verified':False},pending)
        with self.assertRaises(ValueError):self.verify(certificate,pending,shrink=True)
        with self.assertRaises(ValueError):self.verify(certificate|{'input_saved_digest':'c'*64},pending)


class BotProxyTests(FilesFixture):
    def test_safety_flags_reload_without_reconnect_and_scope_changes_fail_closed(self):
        from scripts.compose_bot import LiveConfig
        from scripts.ark_bot_config import BotConfig
        from dataclasses import asdict,replace
        path=self.root/'bot.json';base=BotConfig(guild_id='3'*18,notification_channel_id='4'*18)
        n.save(path,asdict(base));live=LiveConfig(path)
        self.assertFalse(live.shutdown_verified)
        n.save(path,asdict(replace(base,shutdown_verified=True,auto_stop_enabled=True)))
        self.assertTrue(live.shutdown_verified);self.assertTrue(live.auto_stop_enabled)
        n.save(path,asdict(replace(base,guild_id='5'*18)))
        with self.assertRaises(ValueError):_ = live.guild_id

    def test_offline_mode_never_reads_token_or_calls_sdk(self):
        from scripts import compose_bot as b
        from contextlib import nullcontext,redirect_stdout
        handlers={}
        with patch.dict(os.environ,{'ARK_DISCORD_OFFLINE':'1'}),patch.object(b,'LiveConfig'),patch.object(b,'exclusive_lock',return_value=nullcontext()), \
                patch.object(b,'load_token') as token,patch('scripts.discord_bot.run') as sdk,patch.object(b,'RemoteAutomation') as remote, \
                patch.object(b.signal,'signal',side_effect=lambda number,handler:handlers.update({number:handler})), \
                patch.object(b.time,'sleep',side_effect=lambda seconds:handlers[signal.SIGTERM](signal.SIGTERM,None)), \
                patch.object(b.n,'save'),patch.object(b.os,'umask'),redirect_stdout(io.StringIO()):
            remote.return_value.execute.return_value={'ok':True}
            self.assertEqual(b.main(),0)
            remote.return_value.execute.assert_called_once_with('status');token.assert_not_called();sdk.assert_not_called()

    def test_owner_guarded_stop_preserves_existing_automation_latch(self):
        from scripts.compose_server import AdminDispatch,DESIRED
        from contextlib import nullcontext
        from types import SimpleNamespace
        latch=self.file('logs/automation/operation.json',b'{"phase":"failed","unsafe":true}')
        old=latch.read_bytes();controller=Mock();controller.execute.return_value={'ok':True,'data':{'changed':True}}
        a=SimpleNamespace(root=self.root,closing=False,controller=controller,_exclusive=lambda:nullcontext(),problem=Mock(return_value='automation_blocked'))
        self.assertTrue(AdminDispatch(a)({'method':'stop'})['ok'])
        self.assertEqual(latch.read_bytes(),old);self.assertFalse(n.private_json(self.root/DESIRED)['running'])
        a.problem.assert_not_called();controller.execute.assert_called_once_with('stop',confirmed=True)


class ProcessRaceTests(FilesFixture):
    def test_poll_does_not_assign_old_exit_to_new_launch(self):
        from types import SimpleNamespace
        from contextlib import nullcontext
        entered=threading.Event();release=threading.Event();done=threading.Event()
        child=SimpleNamespace(pid=123,returncode=None,poll=lambda:None)
        def spawn(*args,**kwargs):entered.set();release.wait(5);return child
        backend=p.ProcessBackend(self.root,popen=spawn);self.addCleanup(backend.popen.close)
        backend.child=SimpleNamespace(pid=122,returncode=-6,poll=lambda:-6)
        n.save(self.root/p.STATE,{'phase':'exited','supervisor_returncode':-6,'oom_killed':False})
        guard=Mock();guard.verify_startable.return_value={}
        with patch.object(backend,'shutdown_guard',return_value=guard),patch.object(p.c,'launch_url'),patch('scripts.compose_integrity.capture',return_value={}),patch('scripts.compose_integrity.progress',return_value={}), \
                patch.object(p.c,'server_lock',return_value=nullcontext()),patch.object(p,'descriptor',return_value={}), \
                patch.object(p,'oom_kills',return_value=0),patch('scripts.ark_process_worker.check'):
            start=threading.Thread(target=backend._launch);start.start();self.assertTrue(entered.wait(5))
            observed=[]
            def poll():observed.append(backend.state());done.set()
            poller=threading.Thread(target=poll);poller.start()
            self.assertFalse(done.wait(.05));release.set();start.join(5);poller.join(5)
            self.assertTrue(done.is_set());self.assertEqual(observed[0]['ActiveState'],'active')
            self.assertIsNone(observed[0]['SupervisorReturnCode'])
            self.assertEqual(n.private_json(self.root/p.STATE)['phase'],'starting')

    def test_oom_counter_and_missing_evidence_never_become_false(self):
        from types import SimpleNamespace
        backend=p.ProcessBackend(self.root);self.addCleanup(backend.popen.close)
        backend.child=SimpleNamespace(pid=123,returncode=None,poll=lambda:None)
        n.save(self.root/p.STATE,{'phase':'starting','supervisor_returncode':None,'oom_kill_baseline':4,'invocation_id':'a'*32})
        with patch('scripts.ark_process_worker.check'):
            for count,expected in ((4,False),(5,True),(None,None),(3,None)):
                with self.subTest(count=count),patch.object(p,'oom_kills',return_value=count):self.assertIs(backend.state()['OOMKilled'],expected)


class LifecycleTests(FilesFixture):
    def test_shutdown_marker_is_durable_job_bound_and_generation_aware(self):
        from scripts import compose_lifecycle as l
        job='a'*32
        with patch.object(l.time,'monotonic',return_value=100):l.mark_shutdown(self.root,job)
        with patch.object(l.time,'monotonic',return_value=200):l.mark_shutdown(self.root,job)
        self.assertTrue(l.stopped(self.root,job));self.assertTrue(l.stopped(self.root,job,99))
        self.assertFalse(l.stopped(self.root,job,101));self.assertFalse(l.stopped(self.root,'b'*32))
        with patch.object(l.n,'boot_id',return_value='another-boot'):self.assertFalse(l.stopped(self.root,job,0))

    def test_controller_marks_only_its_own_replacement_shutdown(self):
        from scripts import compose_lifecycle as l
        job='a'*32;identity='b'*64
        n.save(self.root/ACTIVE,{'job':job,'phase':'handoff','replacement_server':identity})
        with patch.object(l.socket,'gethostname',return_value='c'*12):l.controller_stopping(self.root)
        self.assertFalse(l.stopped(self.root,job))
        with patch.object(l.socket,'gethostname',return_value='b'*12):l.controller_stopping(self.root)
        self.assertTrue(l.stopped(self.root,job))

    def handoff(self,fail=False,shutdown=False):
        from scripts import compose_update as u,compose_lifecycle as l
        job='a'*32
        record={'schema':1,'job':job,'phase':'handoff','old_updater':'old','next_updater_image':'new-image'}
        n.save(self.root/ACTIVE,record)
        engine=Mock();engine.project='example';engine.inspect.return_value=None;engine.current.side_effect=lambda role:({'Id':'old','State':{'Running':False}} if role=='updater' and not getattr(engine,'replaced',False) else
            {'Id':'new','State':{'Running':True},'Config':{'Hostname':'new-host'}} if role=='updater' else {'State':{'Running':True}})
        # A real bool, not Mock's dynamically-created truthy attribute.
        engine.replaced=False
        def replace(*args,**kwargs):
            if fail:raise u.ControlError('outcome_unknown')
            engine.replaced=True
            n.save(self.root/l.READY,{'hostname':'new-host','boot_id':n.boot_id()})
            return 'new'
        engine.replace.side_effect=replace
        if shutdown:l.mark_shutdown(self.root,job)
        with patch.object(u.n,'CODE_ROOT',self.root),patch.object(u,'ComposeEngine',return_value=engine), \
                patch.dict(os.environ,{'ARK_PROJECT':'example'}),patch.object(u,'call',return_value={'ok':True}):
            if fail:
                with self.assertRaises(u.ControlError):u.complete_handoff(job)
            else:u.complete_handoff(job)
        return n.private_json(self.root/ACTIVE),engine

    def test_handoff_completes_only_after_real_replacement_ready(self):
        record,engine=self.handoff();self.assertEqual(record['phase'],'completed');self.assertTrue(record['updater_handoff_completed']);engine.replace.assert_called_once()

    def test_failed_handoff_is_not_completed(self):
        record,_=self.handoff(fail=True);self.assertEqual(record['phase'],'blocked');self.assertFalse(record['updater_handoff_completed'])

    def test_platform_shutdown_does_not_create_late_updater(self):
        record,engine=self.handoff(shutdown=True);self.assertEqual(record['phase'],'platform_stopped');engine.replace.assert_not_called()


if __name__=='__main__':unittest.main()
