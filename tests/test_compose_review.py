import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from scripts import ark_shutdown as n,ark_process as p,compose_review as r
from scripts.compose_update import ACTIVE

class InterruptedJobReviewTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.job='a'*32;self.runtime={'schema':1,'deployment_id':'d'*32}
  self.record={'schema':1,'job':self.job,'phase':'commit_requested','commit_sent':True,
   'replacement_server':'controller','images':{'server':'image'}}
  self.history={'job':self.job,'phase':'committed','previous_state':{'InvocationID':'b'*32}}
  self.application={'phase':'applied','saved_restored':False,'runtime':self.runtime}
  self.state={'phase':'starting','invocation_id':'c'*32,'boot_id':'previous-boot','supervisor_returncode':None}
  self.launch={'invocation_id':'c'*32,'boot_id':'previous-boot','runtime':self.runtime}
  self.server={'Id':'controller','Image':'image','State':{'Status':'exited','Running':False,'Pid':0,'ExitCode':255}}
  self.engine=Mock();self.engine.current.return_value=self.server;self.engine.call.return_value=''
  for path,value in [(ACTIVE,self.record),(Path('logs/updates/history')/(self.job+'.json'),self.history),
   (Path('backups')/('update-'+self.job)/'application.json',self.application),(p.CONFIG,self.runtime),(p.STATE,self.state),(p.LAUNCH,self.launch)]:n.save(self.root/path,value)
  n.save(self.root/'logs/automation/operation.json',{'phase':'pending','action':'start'})
  saved=self.root/n.SAVED;saved.mkdir(parents=True);(saved/'world.ark').write_bytes(b'current progress'*1000)
 def validate(self):
  return r.validate_interruption(self.record,self.history,self.application,self.runtime,self.state,self.launch,self.server,'current-boot')
 def test_binds_actual_local_commit_not_only_sent_marker(self):
  self.validate();self.history['phase']='prepared'
  with self.assertRaises(ValueError):self.validate()
 def test_does_not_seal_live_or_different_controller(self):
  for change in ({'Id':'new-controller'},{'Image':'different-image'},{'State':{'Status':'running','Running':True,'Pid':123}}):
   old=self.server;self.server=old|change
   with self.assertRaises(ValueError):self.validate()
   self.server=old
 def test_no_current_boot_or_observed_exit_is_reclassified(self):
  for change in ({'boot_id':'current-boot'},{'phase':'exited','supervisor_returncode':-6},{'supervisor_returncode':134}):
   old=self.state;self.state=old|change
   with self.assertRaises(ValueError):self.validate()
   self.state=old
 def test_runtime_and_invocation_binding_cannot_change(self):
  for change in ({'runtime':{}},{'invocation_id':'e'*32},{'boot_id':'unrelated-boot'}):
   old=self.launch;self.launch=old|change
   with self.assertRaises(ValueError):self.validate()
   self.launch=old
 def test_closure_preserves_game_state_latch_and_current_saved(self):
  before={str(path.relative_to(self.root)):path.read_bytes() for path in (self.root/p.STATE,self.root/p.LAUNCH,self.root/'logs/automation/operation.json',self.root/n.SAVED/'world.ark')}
  with patch.object(n,'boot_id',return_value='current-boot'):result=r.seal(self.root,self.engine,self.job)
  for relative,value in before.items():self.assertEqual((self.root/relative).read_bytes(),value)
  self.assertFalse(result['commit_replayed']);self.assertFalse(result['application_replayed']);self.assertFalse(result['new_start_authorized'])
  self.assertFalse(result['native_exit_qualified']);self.assertEqual(n.private_json(self.root/ACTIVE)['phase'],'owner_closed_interrupted')
  self.engine.api.assert_not_called()
  self.assertEqual((self.root/result['current_saved_snapshot']/'Saved/world.ark').read_bytes(),before[str(n.SAVED/'world.ark')])
  with patch.object(n,'boot_id',return_value='current-boot'),self.assertRaises(ValueError):r.seal(self.root,self.engine,self.job)
 def test_live_same_job_helper_blocks_closure(self):
  self.engine.call.return_value='live-helper'
  with patch.object(n,'boot_id',return_value='current-boot'),self.assertRaises(ValueError):r.seal(self.root,self.engine,self.job)
  self.assertEqual(n.private_json(self.root/ACTIVE),self.record)
 def test_pending_application_or_wrong_job_is_never_closed(self):
  n.save(self.root/r.PENDING,{'job':self.job,'phase':'applying'})
  with self.assertRaises(ValueError):r.seal(self.root,self.engine,self.job)
