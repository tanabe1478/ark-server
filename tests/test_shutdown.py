"""All data and processes are synthetic; never operate systemd or a real game."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import ark_config, ark_shutdown as shutdown
from scripts.ark_control import Controller
from scripts.ark_automation import Automation
from scripts.ark_bot_config import BotConfig


class Backend:
    def __init__(self, root, corrupt=None):
        self.root, self.corrupt, self.calls = root, corrupt, []
        self.current = {'ActiveState': 'active', 'SubState': 'running', 'Result': 'success',
                        'MainPID': 123, 'ExecMainStatus': 0, 'ExecMainCode': 0, 'InvocationID': 'a'*32}
    def state(self):
        return self.current.copy()
    def metrics(self):
        return {'pid': self.current['MainPID'], 'uptime_seconds': 7200}
    def operate(self, action):
        self.calls.append(action)
        if action == 'start':
            self.current.update(ActiveState='active', MainPID=124, InvocationID='b'*32)
            return
        assert action == 'stop'
        intent = shutdown.private_json(self.root / shutdown.DIRECTORY / 'intent.json')
        world = self.root / shutdown.SAVED / 'SavedArks/Genesis.ark'
        old = world.stat()
        world.write_bytes(b'NEW_SYNTHETIC_WORLD' + b'\0'*8192)
        os.utime(world, ns=(old.st_atime_ns, old.st_mtime_ns+1000000))
        info = world.stat()
        stamp = intent['monotonic']
        point = {'nonce': intent['nonce'], 'thread': 1, 'game_mode': 123456}
        observed = {'run_id': intent['run_id'], 'pid': intent['game_pid'], 'invocation_id': intent['invocation_id'],
                    'phase': 'exited', 'exit_signal': 6, 'exit_code': None, 'exited_monotonic': stamp+.4,
                    'save_begin': point | {'monotonic': stamp+.1},
                    'save_complete': point | {'monotonic': stamp+.2, 'world_bytes': info.st_size, 'world_mtime_ns': info.st_mtime_ns},
                    'abort': point | {'monotonic': stamp+.3, 'pc': shutdown.FORCE_ABORT, 'caller': shutdown.EXIT_CALLER}}
        self.current.update(ActiveState='failed', SubState='failed', Result='core-dump', MainPID=0, ExecMainStatus=6, ExecMainCode=3)
        if self.corrupt:
            self.corrupt(observed, self)
        shutdown.save(self.root / shutdown.DIRECTORY / 'runtime' / (intent['run_id']+'.json'), observed)


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        ark_config.apply_config(self.root, {'settings': ark_config.DEFAULTS.copy(), 'password': 'PRIVATE_PARTICIPANT', 'admin_password': 'PRIVATE_ADMIN'})
        binary = self.root / shutdown.BINARY
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'FAKE_BINARY_NEVER_EXECUTE')
        patcher = patch.object(shutdown, 'BINARY_SHA256', hashlib.sha256(binary.read_bytes()).hexdigest())
        patcher.start(); self.addCleanup(patcher.stop)
        shutdown.install_policy(self.root)
        directory = self.root / shutdown.SAVED / 'SavedArks'
        directory.mkdir(parents=True)
        (directory/'Genesis.ark').write_bytes(b'OLD_SYNTHETIC_WORLD'+b'\0'*8192)
        (directory/'player.arkprofile').write_bytes(b'SYNTHETIC_PROFILE')
        (directory/'tribe.arktribe').write_bytes(b'SYNTHETIC_TRIBE')
        self.backend = Backend(self.root)
        self.controller = Controller(self.root, backend=self.backend, probe=lambda *a,**k: {'map':'Genesis','players':0})
        self.guard = shutdown.ShutdownGuard(self.root)
        self.run = 'c'*32
        shutdown.save(self.guard.directory/'live.json', {'run_id':self.run,'supervisor_pid':123,'debugger_pid':456,'invocation_id':'a'*32,'boot_id':shutdown.boot_id()})
        shutdown.save(self.guard.directory/'runtime'/(self.run+'.json'), {'run_id':self.run,'phase':'running','pid':789})
        patcher = patch.object(shutdown, 'process_matches', return_value=True)
        patcher.start();self.addCleanup(patcher.stop)
        # Any accidental real service call fails loudly.
        patcher = patch('scripts.ark_control.SystemdBackend._run', side_effect=AssertionError('No real systemd'))
        patcher.start();self.addCleanup(patcher.stop)

    def test_qualified_stop_preserves_failed_state_and_snapshots(self):
        result = self.controller.execute('stop', confirmed=True)
        self.assertTrue(result['ok'], result)
        data = result['data']
        self.assertFalse(data['shutdown']['clean_exit'])
        self.assertFalse(data['shutdown']['in_game_content_verified'])
        self.assertEqual(data['warnings'], ['expected_native_abort'])
        self.assertEqual(data['service']['Result'], 'core-dump')
        self.assertEqual(data['service']['ExecMainStatus'], 6)
        self.assertEqual(self.backend.calls, ['stop'])
        snapshot = self.root / data['shutdown']['snapshot']
        self.assertTrue((snapshot/'before/Saved/SavedArks/Genesis.ark').is_file())
        self.assertTrue((snapshot/'after/Saved/SavedArks/Genesis.ark').is_file())
        self.assertEqual((snapshot/'receipt.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertFalse(self.guard.verify_stopped(self.backend.state())['clean_exit'])

    def test_restart_retains_warning_and_never_uses_systemctl_restart(self):
        result = self.controller.execute('restart', confirmed=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.backend.calls, ['stop','start'])
        self.assertIn('expected_native_abort', result['data']['warnings'])

    def test_unproven_abort_or_missing_save_never_starts(self):
        cases = (
            lambda o,b:o.pop('save_complete'),
            lambda o,b:o['abort'].update(caller=0),
            lambda o,b:o['save_complete'].update(nonce='wrong'),
            lambda o,b:o['save_begin'].update(monotonic=float('nan')),
            lambda o,b:o['save_complete'].update(monotonic=o['abort']['monotonic']+1),
            lambda o,b:o['save_complete'].update(world_bytes=1),
            lambda o,b:o.update(exit_signal=9),
            lambda o,b:o.update(exit_code=0),
            lambda o,b:o.update(observation_fault=True),
            lambda o,b:b.current.update(Result='timeout',ExecMainStatus=9),
            lambda o,b:(b.root/shutdown.SAVED/'SavedArks/player.arkprofile').unlink(),
        )
        for number, corrupt in enumerate(cases):
            with self.subTest(case=number):
                self.setUp()
                self.backend.corrupt = corrupt
                result = self.controller.execute('restart', confirmed=True)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'shutdown_failed')
                self.assertEqual(self.backend.calls, ['stop'])
                self.assertFalse((self.guard.directory/'last-stop.json').exists())

    def test_changed_saved_invalidates_start_permission(self):
        self.assertTrue(self.controller.execute('stop',confirmed=True)['ok'])
        (self.root/shutdown.SAVED/'SavedArks/player.arkprofile').write_bytes(b'CHANGED')
        result = self.controller.execute('start')
        self.assertFalse(result['ok'])
        self.assertEqual(self.backend.calls,['stop'])

    def test_new_invocation_or_unknown_failed_state_never_reuses_receipt(self):
        self.assertTrue(self.controller.execute('stop',confirmed=True)['ok'])
        self.backend.current['InvocationID']='d'*32
        self.assertFalse(self.controller.execute('start')['ok'])
        self.assertEqual(self.backend.calls,['stop'])

    def test_changed_build_and_observer_contract_fail_closed(self):
        for value in ('binary_sha256','observer_contract'):
            with self.subTest(value=value):
                approved = shutdown.private_json(self.root/shutdown.POLICY)
                approved[value]='wrong'
                shutdown.save(self.root/shutdown.POLICY,approved)
                self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
                self.assertEqual(self.backend.calls,[])
                shutdown.install_policy(self.root)

    def test_idle_last_check_skips_stop_after_preservation(self):
        result = self.guard.stop(self.backend,lambda:'players_present')
        self.assertFalse(result['changed'])
        self.assertEqual(self.backend.calls,[])
        self.assertFalse((self.guard.directory/'intent.json').exists())

    def test_duplicate_intent_and_insufficient_space_never_stop(self):
        shutdown.save(self.guard.directory/'intent.json',{'run_id':self.run})
        self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
        self.assertEqual(self.backend.calls,[])
        (self.guard.directory/'intent.json').unlink()
        with patch.object(shutdown.shutil,'disk_usage',return_value=type('Space',(),{'free':0})()):
            self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
        self.assertEqual(self.backend.calls,[])

    def test_symlink_and_nonprivate_evidence_rejected(self):
        path=self.guard.directory/'live.json';content=path.read_bytes()
        path.chmod(0o644)
        self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
        path.unlink();target=self.root/'outside.json';target.write_bytes(content);path.symlink_to(target)
        self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
        self.assertEqual(self.backend.calls,[])

    def test_unrelated_unknown_failure_does_not_gain_an_initial_review(self):
        self.backend.current.update(ActiveState='failed',MainPID=0,Result='core-dump',ExecMainStatus=6,ExecMainCode=3)
        self.assertFalse(self.controller.execute('start')['ok'])
        automation=Automation(BotConfig(),self.controller)
        automation._save('stop','old','blocked','shutdown_failed')
        self.assertEqual(automation.problem(),'automation_blocked')
        self.assertFalse(automation.execute('start')['ok'])

    def test_explicit_initial_review_is_data_bound_and_not_a_clean_stop(self):
        self.backend.current.update(ActiveState='failed', SubState='failed', MainPID=0,
                                    Result='core-dump', ExecMainStatus=6, ExecMainCode=3)
        automation=Automation(BotConfig(), self.controller)
        automation._save('stop','original','blocked','shutdown_failed')
        original=automation.record.read_bytes()
        folder=self.root/'backups/shutdown-validation-test'
        files=[{'path':'Saved/'+name,'bytes':entry['bytes'],'sha256':shutdown.digest(self.root/shutdown.SAVED/name)}
               for name,entry in shutdown.inventory(self.root/shutdown.SAVED).items()]
        shutdown.save(folder/'after/manifest.json',{'files':files})
        shutdown.save(folder/'report.json',{'service_after':{'InvocationID':'a'*32},
                      'protected_hashes_before':{str(self.root/ark_config.INI_PATH):shutdown.digest(self.root/ark_config.INI_PATH)}})
        result=self.guard.review_initial_failure(self.backend.state(),folder)
        self.assertFalse(result['clean_exit'])
        self.assertEqual(automation.record.read_bytes(),original)
        self.assertIsNone(automation.problem())
        self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
        started=self.controller.execute('start')
        self.assertTrue(started['ok'],started)
        self.assertEqual(started['data']['warnings'],['reviewed_previous_failure'])
        self.backend.current.update(ActiveState='failed', MainPID=0)
        self.assertFalse(self.controller.execute('start')['ok'])
        self.assertEqual(self.backend.calls,['start'])

    def test_receipt_storage_failure_and_nonfinite_evidence_stay_blocked(self):
        real_save=shutdown.save
        def fail(path,value):
            if Path(path).name=='last-stop.json':raise OSError('PRIVATE_STORAGE_ERROR')
            return real_save(path,value)
        with patch.object(shutdown,'save',side_effect=fail):
            result=self.controller.execute('restart',confirmed=True)
        self.assertFalse(result['ok'])
        self.assertEqual(self.backend.calls,['stop'])
        self.assertFalse(self.controller.execute('start')['ok'])
        self.assertNotIn('PRIVATE_STORAGE_ERROR',json.dumps(result))

    def test_reboot_factory_success_does_not_erase_unknown_runtime(self):
        self.backend.current.update(ActiveState='inactive',SubState='dead',Result='success',MainPID=0,ExecMainStatus=0,ExecMainCode=0,InvocationID='')
        self.assertFalse(self.controller.execute('start')['ok'])
        self.assertEqual(self.backend.calls,[])

    def test_reboot_can_reuse_only_qualified_unchanged_historical_save(self):
        self.assertTrue(self.controller.execute('stop',confirmed=True)['ok'])
        self.backend.current.update(ActiveState='inactive',SubState='dead',Result='success',MainPID=0,ExecMainStatus=0,ExecMainCode=0,InvocationID='')
        with patch.object(shutdown,'boot_id',return_value='new-boot'):
            result=self.controller.execute('start')
        self.assertTrue(result['ok'],result)
        self.assertIn('expected_native_abort',result['data']['warnings'])
        self.assertEqual(self.backend.calls,['stop','start'])

    def test_historical_success_boolean_cannot_masquerade_as_exit_zero(self):
        shutdown.save(self.guard.directory/'runtime'/(self.run+'.json'),{'run_id':self.run,'invocation_id':'a'*32,
                      'phase':'exited','exit_signal':None,'exit_code':False})
        self.backend.current.update(ActiveState='inactive',SubState='dead',Result='success',MainPID=0,ExecMainStatus=0,ExecMainCode=0,InvocationID='')
        self.assertFalse(self.controller.execute('start')['ok'])
        self.assertEqual(self.backend.calls,[])

    def test_saved_symlink_stops_before_any_operation(self):
        (self.root/shutdown.SAVED/'SavedArks/bad').symlink_to(self.root/'config')
        self.assertFalse(self.controller.execute('stop',confirmed=True)['ok'])
        self.assertEqual(self.backend.calls,[])
