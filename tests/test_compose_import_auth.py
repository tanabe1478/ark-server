"""Legacy credentials are preservable only when bound to an existing INI."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import ark_config as c,ark_shutdown as n,compose_setup as s

class PreservedImportAuthenticationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.old=('oldPw_8','EXAMPLE_ADMINISTRATOR_456')
  c.apply_config(self.root,{'settings':dict(c.DEFAULTS),'password':'EXAMPLE_PARTICIPANT_123','admin_password':self.old[1]})
  ini=self.root/c.INI_PATH;ini.write_text(ini.read_text().replace('EXAMPLE_PARTICIPANT_123',self.old[0]))
  self.value=json.loads((n.CODE_ROOT/'examples/compose-bootstrap.example.json').read_bytes())
  self.value.update(password=self.old[0],admin_password=self.old[1])
  self.value['discord']={'guild_id':'555555555555555555','notification_channel_id':'666666666666666666'}
 def test_new_setup_still_rejects_short_password(self):
  with self.assertRaises(ValueError):s.validate(self.value)
 def test_explicit_existing_authentication_accepts_exact_short_value(self):
  self.assertEqual(s.validate(self.value,preserved_authentication=self.old),self.value)
 def test_weak_new_or_mismatched_admin_cannot_use_import_exception(self):
  for change in ({'password':'newPw_8'},{'admin_password':'different_admin_123'}):
   with self.subTest(change=list(change)),self.assertRaises(ValueError):s.validate(self.value|change,preserved_authentication=self.old)
 def test_common_new_password_api_remains_strict(self):
  payload={k:self.value[k] for k in ('settings','password','admin_password')}
  with self.assertRaises(c.ConfigError):c.apply_config(self.root,payload)
  with self.assertRaises(c.ConfigError):c.apply_config(self.root,payload|{'password':'newPw_8'},preserve_existing_authentication=True)
 def test_preservation_requires_an_existing_ini_not_only_a_flag(self):
  other=self.root/'empty'
  with self.assertRaises(c.ConfigError):c.apply_config(other,{k:self.value[k] for k in ('settings','password','admin_password')},preserve_existing_authentication=True)
 def test_configure_preserves_auth_and_world_without_rotating(self):
  saved=self.root/n.SAVED;world=saved/'SavedArks/Genesis.ark';world.parent.mkdir(parents=True);world.write_bytes(b'current world'*1000)
  before=world.read_bytes();s.validate(self.value,preserved_authentication=self.old);s.configure(self.root,self.value)
  self.assertEqual(s.authentication(saved),self.old);self.assertEqual(world.read_bytes(),before)
 def test_strong_new_password_change_is_still_allowed(self):
  value=self.value|{'password':'EXAMPLE_NEW_PARTICIPANT_789'}
  s.validate(value,preserved_authentication=self.old);s.configure(self.root,value)
  self.assertEqual(s.authentication(self.root/n.SAVED),(value['password'],self.old[1]))
 def test_normal_up_after_import_keeps_bound_legacy_policy(self):
  input_path=self.root/'input.json';n.save(input_path,self.value)
  n.save(self.root/s.BOOTSTRAP,{'schema':1})
  n.save(self.root/'config/installation.json',{'schema':1,'input_sha256':s.input_digest(self.value),'authentication_policy':'preserved_import'})
  with patch.object(s,'INPUT',input_path),patch.object(s,'descriptor',return_value={}):
   result=s.initialize(self.root,imported=False)
  self.assertFalse(result['changed']);self.assertFalse(result['saved_overwritten'])
