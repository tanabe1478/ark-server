"""Synthetic IPC, never real game, Discord, systemd or Docker."""
from dataclasses import asdict
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from scripts.ark_control import Actor, ControlError, response
from scripts.ark_control_ipc import Dispatch, RemoteAutomation, Server, decode, INTERVAL_SECONDS


class FakeAutomation:
    config=SimpleNamespace(guild_id='100000000000000001')
    def __init__(self):self.calls=[]
    def execute(self,action,params,**kwargs):
        self.calls.append((action,params,kwargs));return response(kwargs['request_id'],action,data={'phase':'stopped'})
    def idle_check(self):self.calls.append('idle');return {'ok':True,'data':{'changed':False}}


class IPCTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.fake=FakeAutomation();self.clock=100.;self.dispatch=Dispatch(self.fake,lambda:self.clock)
        self.actor=Actor('discord','100000000000000003',self.fake.config.guild_id,'100000000000000002')
        self.message={'method':'execute','action':'start','params':{},'request_id':'synthetic-request',
                      'confirmed':False,'actor':asdict(self.actor)}

    def test_duplicate_json_and_nonobject_rejected(self):
        for raw in (b'{"method":1,"method":2}',b'[]'):
            with self.assertRaises(ValueError):decode(raw)

    def test_host_accepts_only_small_action_surface(self):
        self.assertTrue(self.dispatch(self.message)['ok'])
        for action in ('stop','stop.idle','backup','settings.apply','migration.apply','exec','update'):
            with self.subTest(action=action),self.assertRaises(ControlError):self.dispatch(self.message|{'action':action,'confirmed':True})
        self.assertEqual(len(self.fake.calls),1)

    def test_mutation_requires_discord_actor_in_exact_guild(self):
        for actor in (None,asdict(Actor('local','1')),asdict(Actor('discord','1','2','3'))):
            with self.assertRaises(ControlError):self.dispatch(self.message|{'actor':actor})
        self.assertEqual(self.fake.calls,[])

    def test_extra_fields_or_nonboolean_confirmation_rejected(self):
        for value in (self.message|{'extra':1},self.message|{'confirmed':1}):
            with self.assertRaises(ControlError):self.dispatch(value)

    def test_idle_interval_has_initial_delay_and_no_repeat(self):
        self.assertEqual(self.dispatch({'method':'idle_check'})['data']['reason'],'interval_not_due')
        self.clock+=INTERVAL_SECONDS;self.dispatch({'method':'idle_check'});self.dispatch({'method':'idle_check'})
        self.assertEqual(self.fake.calls,['idle'])

    def test_real_local_socket_owner_permissions_and_status(self):
        path=self.root/'control.sock'
        with Server(path,self.dispatch) as server:
            worker=threading.Thread(target=server.serve_forever);worker.start()
            try:
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                client=RemoteAutomation(self.fake.config,path,self.root)
                self.assertTrue(client.execute('status',request_id='synthetic-status')['ok'])
                self.assertEqual(self.fake.calls[0][0],'status')
            finally:server.shutdown();worker.join()

    def test_missing_or_world_accessible_socket_fails_before_send(self):
        path=self.root/'control.sock';client=RemoteAutomation(self.fake.config,path,self.root)
        self.assertEqual(client.execute('start')['error']['code'],'service_unavailable')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as owned:
            owned.bind(str(path));path.chmod(0o666)
            self.assertEqual(client.execute('start')['error']['code'],'service_unavailable')

    def test_broken_reply_after_transmission_is_unknown_and_not_retried(self):
        path=self.root/'control.sock'
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as owned:
            owned.bind(str(path));path.chmod(0o600)
            transport=MagicMock();transport.__enter__.return_value=transport
            stream=MagicMock();stream.__enter__.return_value=stream;stream.readline.return_value=b''
            transport.makefile.return_value=stream
            client=RemoteAutomation(self.fake.config,path,self.root)
            with patch('scripts.ark_control_ipc.socket.socket',return_value=transport):result=client.execute('start')
            self.assertEqual(result['error']['code'],'outcome_unknown')
            self.assertEqual(transport.sendall.call_count,1);self.assertEqual(transport.connect.call_count,1)

    def test_client_refuses_extra_actions_without_connecting(self):
        client=RemoteAutomation(self.fake.config,self.root/'absent',self.root)
        for action in ('stop','exec','backup'):
            self.assertEqual(client.execute(action,confirmed=True)['error']['code'],'invalid_request')


if __name__=='__main__':unittest.main()
