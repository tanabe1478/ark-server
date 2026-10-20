"""SDK/transport tests with fake interactions. NEVER connects to Discord/systemd."""
import asyncio
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from scripts import ark_config
from scripts.ark_control import Controller
from scripts.ark_automation import Automation, INTERVAL_SECONDS
from scripts.ark_bot_config import BotConfig
from test_automation import MetricsBackend, GUILD, CHANNEL, USER

SDK_AVAILABLE = importlib.util.find_spec('discord') is not None


@unittest.skipUnless(SDK_AVAILABLE, 'Run in .venv-discord for Discord SDK tests')
class DiscordBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from scripts.discord_bot import build_client
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ark_config.apply_config(self.root, {'settings': ark_config.DEFAULTS.copy(),
                                           'password': 'participant-private', 'admin_password': 'administrator-private'})
        self.backend = MetricsBackend()
        self.controller = Controller(self.root, self.backend, probe=lambda *a, **kw: {
            'players': 0, 'map': 'Genesis', 'password_required': True, 'max_players': 13})
        self.config = BotConfig(guild_id=GUILD)
        self.automation = Automation(self.config, self.controller)
        self.client = build_client(self.config, self.automation)
        self.addAsyncCleanup(self.client.close)
        import discord
        self.group = self.client.tree.get_commands(guild=discord.Object(id=int(GUILD)))[0]

    def interaction(self, *, guild=GUILD, user=USER, iid='423456789012345678'):
        return SimpleNamespace(
            id=int(iid), guild_id=int(guild) if guild else None, channel_id=int(CHANNEL),
            user=SimpleNamespace(id=int(user)),
            channel=SimpleNamespace(guild=SimpleNamespace(id=int(guild)) if guild else None, send=AsyncMock()),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: True, edit_message=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))

    async def test_only_three_guild_commands_no_global_or_privileged_intents(self):
        self.assertEqual(self.client.tree.get_commands(), [])
        self.assertEqual(self.group.name, 'ark')
        self.assertEqual({command.name for command in self.group.commands}, {'status', 'start', 'restart'})
        self.assertIsNone(self.group.default_permissions)
        self.assertFalse(self.client.intents.members)
        self.assertFalse(self.client.intents.message_content)
        self.assertFalse(self.client.intents.presences)

    async def test_status_has_no_mutation(self):
        interaction = self.interaction()
        await self.group.get_command('status').callback(interaction)
        self.assertEqual(self.backend.calls, [])
        self.assertIn('ARK:', interaction.edit_original_response.call_args.kwargs['content'])

    async def test_every_member_can_start_and_get_ready_notification(self):
        interaction = self.interaction(user='523456789012345678')
        await self.group.get_command('start').callback(interaction)
        interaction.response.defer.assert_awaited_once()
        self.assertEqual(self.backend.calls, ['start'])
        text = interaction.channel.send.call_args.args[0]
        self.assertIn('<@523456789012345678>', text)
        self.assertIn('準備が整いました', text)
        self.assertNotIn('private', text)

    async def test_wrong_guild_and_dms_denied_before_defer_or_mutation(self):
        for guild in (None, '623456789012345678'):
            interaction = self.interaction(guild=guild)
            await self.group.get_command('start').callback(interaction)
            interaction.response.send_message.assert_awaited_once()
            interaction.response.defer.assert_not_awaited()
        self.assertEqual(self.backend.calls, [])

    async def test_failed_notification_does_not_repeat_start(self):
        interaction = self.interaction()
        interaction.channel.send.side_effect = RuntimeError('private-http-url-token')
        interaction.followup.send.side_effect = RuntimeError('private-http-url-token')
        with patch('builtins.print') as output:
            await self.group.get_command('start').callback(interaction)
        self.assertEqual(self.backend.calls, ['start'])
        self.assertNotIn('private-http-url-token', str(output.call_args_list))

    async def test_restart_disabled_before_validation(self):
        interaction = self.interaction()
        await self.group.get_command('restart').callback(interaction)
        self.assertIn('未検証', interaction.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(self.backend.calls, [])

    async def test_confirmation_is_one_shot_and_bound_to_requester(self):
        self.backend.active = 'active'
        self.automation.config = BotConfig(guild_id=GUILD, shutdown_verified=True)
        from scripts.discord_bot import build_client
        client = build_client(self.automation.config, self.automation)
        self.addAsyncCleanup(client.close)
        import discord
        group = client.tree.get_commands(guild=discord.Object(id=int(GUILD)))[0]
        initial = self.interaction()
        await group.get_command('restart').callback(initial)
        view = initial.edit_original_response.call_args.kwargs['view']
        self.assertIn('現在の人数: 0', initial.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(self.backend.calls, [])
        other = self.interaction(user='523456789012345678')
        self.assertFalse(await view.interaction_check(other))
        same = self.interaction(iid='723456789012345678')
        self.assertTrue(await view.interaction_check(same))
        await view.children[0].callback(same)
        self.assertEqual(self.backend.calls, ['stop', 'start'])
        replay = self.interaction(iid='823456789012345678')
        await view.children[0].callback(replay)
        self.assertEqual(self.backend.calls, ['stop', 'start'])
        replay.response.send_message.assert_awaited_once()

    async def test_player_count_change_requires_a_new_confirmation(self):
        self.backend.active = 'active'
        self.automation.config = BotConfig(guild_id=GUILD, shutdown_verified=True)
        from scripts.discord_bot import build_client
        client = build_client(self.automation.config, self.automation)
        self.addAsyncCleanup(client.close)
        import discord
        group = client.tree.get_commands(guild=discord.Object(id=int(GUILD)))[0]
        initial = self.interaction()
        await group.get_command('restart').callback(initial)
        view = initial.edit_original_response.call_args.kwargs['view']
        self.controller.probe = lambda *a, **kw: {'players': 1, 'map': 'Genesis', 'max_players': 13, 'password_required': True}
        confirm = self.interaction(iid='723456789012345678')
        await view.children[0].callback(confirm)
        self.assertEqual(self.backend.calls, [])
        self.assertIn('人数が変わった', confirm.edit_original_response.call_args.kwargs['content'])
        self.assertIn('現在の人数: 1', confirm.edit_original_response.call_args.kwargs['content'])
        fresh = confirm.edit_original_response.call_args.kwargs['view']
        await fresh.children[0].callback(self.interaction(iid='823456789012345678'))
        self.assertEqual(self.backend.calls, ['stop', 'start'])
        # A late cancellation cannot falsely claim that an accepted restart was cancelled.
        late = self.interaction(iid='923456789012345678')
        await fresh.children[1].callback(late)
        late.response.send_message.assert_awaited_once()
        late.response.edit_message.assert_not_awaited()

    async def test_confirmation_timeout_updates_message_without_mutating(self):
        self.backend.active = 'active'
        self.automation.config = BotConfig(guild_id=GUILD, shutdown_verified=True)
        from scripts.discord_bot import build_client
        client = build_client(self.automation.config, self.automation)
        self.addAsyncCleanup(client.close)
        import discord
        group = client.tree.get_commands(guild=discord.Object(id=int(GUILD)))[0]
        initial = self.interaction()
        await group.get_command('restart').callback(initial)
        view = initial.edit_original_response.call_args.kwargs['view']
        view.stop()
        await view.on_timeout()
        self.assertIn('期限切れ', initial.edit_original_response.call_args.kwargs['content'])
        self.assertIsNone(initial.edit_original_response.call_args.kwargs['view'])
        self.assertEqual(self.backend.calls, [])

    async def test_disconnected_hourly_tick_never_stops(self):
        self.client.is_ready = lambda: False
        self.automation.idle_check = Mock(side_effect=AssertionError('must not execute'))
        seen = []
        async def wait_for(awaitable, *, timeout):
            seen.append(timeout)
            awaitable.close()
            self.client.stop_event.set()
            raise asyncio.TimeoutError
        with patch('scripts.discord_bot.asyncio.wait_for', side_effect=wait_for), patch('builtins.print'):
            await self.client.idle_loop()
        self.assertEqual(seen, [INTERVAL_SECONDS])
        self.automation.idle_check.assert_not_called()

    async def test_initially_disabled_idle_loop_can_enable_without_reconnect(self):
        from scripts.compose_bot import LiveConfig
        from scripts import ark_shutdown as n
        from scripts.discord_bot import build_client
        from dataclasses import asdict,replace
        path=self.root/'live-bot.json';base=BotConfig(guild_id=GUILD,notification_channel_id=CHANNEL)
        n.save(path,asdict(base));client=build_client(LiveConfig(path),self.automation)
        self.addAsyncCleanup(client.close)
        client.is_ready=lambda:True;client.gateway_connected=True;client.notification_channel=AsyncMock()
        self.automation.idle_check=Mock(return_value={'ok':True,'data':{'changed':False}})
        ticks=0
        async def wait_for(awaitable,*,timeout):
            nonlocal ticks
            awaitable.close();ticks+=1
            if ticks==2:n.save(path,asdict(replace(base,shutdown_verified=True,auto_stop_enabled=True)))
            if ticks==3:return
            raise asyncio.TimeoutError
        with patch('scripts.discord_bot.asyncio.wait_for',side_effect=wait_for),patch('builtins.print'):
            await client.idle_loop()
        self.automation.idle_check.assert_called_once_with();client.notification_channel.assert_awaited_once()

    async def test_shutdown_drains_accepted_jobs_without_cancellation(self):
        accepted = asyncio.Event()
        finish = asyncio.Event()
        async def job():
            accepted.set()
            await finish.wait()
        task = asyncio.create_task(job())
        self.client.jobs.add(task)
        await accepted.wait()
        draining = asyncio.create_task(self.client.drain())
        await asyncio.sleep(0)
        self.assertFalse(draining.done())
        self.assertFalse(self.client.accepting)
        finish.set()
        await draining
        self.assertFalse(task.cancelled())
