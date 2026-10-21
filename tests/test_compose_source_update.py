"""Source-only promotion must never copy or write installed program bytes."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import ark_shutdown as n
from scripts import compose_assets as assets, compose_apply as a, compose_update as u


class ReadOnlySourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.server=self.root/'server';self.saved=self.root/n.SAVED
        self.saved.mkdir(parents=True)
        (self.saved/'world.ark').write_bytes(b'newer progress'*1000)
        (self.server/'program').write_bytes(b'installed program')
        self.versions={'build_id':'123','mods':{}}
        n.save(self.root/'config/assets.json',{'schema':1,'versions':self.versions})
        self.pending={'job':'a'*32,'phase':'prepared','previous_state':{'InvocationID':'b'*32},
            'configuration':{},'files':n.capture(self.saved,self.root/'snapshot')}
        n.save(self.root/u.PENDING,self.pending)
        n.save(self.root/a.LAUNCH,{'invocation_id':'b'*32})
        self.candidate=self.root/'candidate'
        self.artifact={'schema':1,'asset_mode':'installed','versions':self.versions}
        n.save(self.candidate/'asset-manifest.json',self.artifact)
        self.cert={'native_policy':{},'assets_digest':a.digest_value(a.asset_files(self.server))}
        self.certificate=self.root/'certificate.json';n.save(self.certificate,self.cert)

    def apply(self, forbid_program_writes=True, readonly=True):
        with ExitStack() as stack:
            stack.enter_context(patch.object(a.os,'statvfs',return_value=Mock(f_flag=os.ST_RDONLY if readonly else 0)))
            stack.enter_context(patch.object(a,'validate_certificate'))
            stack.enter_context(patch.object(n,'configuration',return_value={}))
            stack.enter_context(patch.object(n,'install_policy',return_value={}))
            stack.enter_context(patch.object(a,'contract',return_value={}))
            stack.enter_context(patch.object(a,'build_identity',return_value={}))
            stack.enter_context(patch('scripts.ark_config.load_editable_settings',return_value={'active_mods':[]}))
            if forbid_program_writes:
                stack.enter_context(patch.object(a,'clear_programs',side_effect=AssertionError('No installed program removal')))
                stack.enter_context(patch.object(a,'copy_assets',side_effect=AssertionError('No installed program copying')))
            return a.apply(self.root,candidate=self.candidate,certificate_path=self.certificate)

    def test_source_only_reuses_exact_program_inode_and_saved(self):
        before=(self.server/'program').stat();world=(self.saved/'world.ark').read_bytes()
        self.apply()
        after=(self.server/'program').stat()
        self.assertEqual((before.st_ino,before.st_mtime_ns),(after.st_ino,after.st_mtime_ns))
        self.assertEqual((self.saved/'world.ark').read_bytes(),world)
        archive=self.root/'backups'/('update-'+'a'*32)
        self.assertFalse((archive/'previous-server').exists())
        self.assertTrue((archive/'previous-config/assets.json').is_file())
        result=n.private_json(archive/'application.json')
        self.assertTrue(result['programs_reused_readonly']);self.assertEqual(result['program_bytes_copied'],0)
        self.assertIs(result['saved_restored'],False)

    def test_source_only_requires_kernel_readonly_not_just_a_manifest_claim(self):
        with self.assertRaisesRegex(ValueError,'kernel read-only'):self.apply(readonly=False)
        self.assertEqual(n.private_json(self.root/u.PENDING)['phase'],'prepared')

    def test_source_only_rejects_changed_program_before_mutation(self):
        (self.server/'program').write_bytes(b'unqualified changed program')
        with self.assertRaisesRegex(ValueError,'assets changed'):self.apply()
        self.assertEqual(n.private_json(self.root/u.PENDING)['phase'],'prepared')

    def test_source_only_cannot_claim_new_game_or_mod_version(self):
        n.save(self.candidate/'asset-manifest.json',self.artifact|{'versions':{'build_id':'124','mods':{}}})
        with self.assertRaisesRegex(ValueError,'may not change'):self.apply()
        self.assertFalse((self.root/'backups'/('update-'+'a'*32)).exists())

    def test_source_only_explicit_recovery_has_no_program_writes(self):
        self.apply();self.apply()
        self.assertEqual((self.server/'program').read_bytes(),b'installed program')

    def test_newer_invocation_blocks_application(self):
        n.save(self.root/a.LAUNCH,{'invocation_id':'c'*32})
        with self.assertRaisesRegex(ValueError,'later invocation'):self.apply()

    def test_asset_mode_cannot_change_during_recovery(self):
        self.apply()
        assets.copy_assets(self.server,self.candidate/'server')
        n.save(self.candidate/'asset-manifest.json',self.artifact|{'asset_mode':'staged'})
        with self.assertRaisesRegex(ValueError,'original asset preservation'):self.apply()

    def test_game_mod_update_still_preserves_and_copies_assets(self):
        (self.server/'program').write_bytes(b'old program')
        assets.copy_assets(self.server,self.candidate/'server')
        (self.candidate/'server/program').write_bytes(b'new qualified program')
        n.save(self.candidate/'asset-manifest.json',self.artifact|{'asset_mode':'staged'})
        n.save(self.certificate,self.cert|{'assets_digest':a.digest_value(a.asset_files(self.candidate/'server'))})
        world=(self.saved/'world.ark').read_bytes();self.apply(forbid_program_writes=False)
        self.assertEqual((self.server/'program').read_bytes(),b'new qualified program')
        self.assertEqual((self.root/'backups'/('update-'+'a'*32)/'previous-server/program').read_bytes(),b'old program')
        self.assertEqual((self.saved/'world.ark').read_bytes(),world)

    def test_asset_hash_prunes_saved_before_reading_not_after(self):
        original=n.digest;read=[]
        def digest(path):
            read.append(Path(path));return original(path)
        with patch.object(n,'digest',side_effect=digest):value=assets.tree_manifest(self.server,exclude_saved=True)
        self.assertEqual(set(value),{'program'})
        self.assertFalse(any(self.saved in path.parents for path in read))

    def test_source_only_apply_and_trial_mount_game_readonly(self):
        engine=Mock();engine.run_job.return_value={'exit_code':0,'oom_killed':False,'container':'helper'}
        updater=u.Updater(self.root,engine);updater.record={'job':'a'*32,'asset_mode':'installed'}
        updater.save=Mock()
        self.assertEqual(updater.asset_mount(),('game','','/opt/ark/server',True))
        updater.apply('image')
        mounts=engine.run_job.call_args.args[5]
        self.assertIn(('game','','/opt/ark/server',True),mounts)
        self.assertIn(('saved','','/opt/ark/server/ShooterGame/Saved',True),mounts)
        updater.record['asset_mode']='staged';updater.apply('image')
        self.assertIn(('game','','/opt/ark/server',False),engine.run_job.call_args.args[5])

    def test_validate_current_never_runs_steam_or_creates_game_copy(self):
        engine=Mock();engine.project='example';engine.current.return_value={'Image':'image'}
        engine.run_job.return_value={'exit_code':0,'oom_killed':False}
        (self.root/u.PENDING).unlink()
        updater=u.Updater(self.root,engine);updater.metadata=Mock(return_value={'release':{'digest':'d'*64}})
        updater.installer=Mock(side_effect=AssertionError('No source-only Steam job'))
        updater.prepare=Mock(return_value=False)
        config={'minimum_free_memory_bytes':1024**3}
        with patch.object(u,'SOURCE_STAGE',self.root/'stage'),patch.object(u,'settings',return_value=config),patch.object(u.c,'load_editable_settings',return_value={'active_mods':[]}),patch.object(u,'memory_available',return_value=64*1024**3),patch.object(u.shutil,'disk_usage',return_value=Mock(free=64*1024**3)):
            record=updater.perform(force=True)
        self.assertEqual(record['asset_mode'],'installed');updater.installer.assert_not_called()
        self.assertFalse((updater.folder/'candidate/server').exists())
        self.assertEqual(n.private_json(updater.folder/'candidate/asset-manifest.json')['versions'],self.versions)
        self.assertIn(('game','','/opt/ark/server',True),engine.run_job.call_args.args[5])
