#!/usr/bin/env python3
"""Outbound-only Discord Gateway bot: /ark status, start, restart. No HTTP listener."""
import asyncio
import logging
import os
import signal
import sys

from .ark_automation import Automation, INTERVAL_SECONDS
from .discord_messages import JoinInfo, JoinResolver, failure_text, format_status, ready_text, stopped_text
from .ark_bot_config import BotConfig, BotConfigError, load_token
from .ark_control import ControlError, exclusive_lock
from .discord_adapter import Context, DiscordAdapter, GuildPolicy


def build_client(config, automation, *, join=None):
    join = JoinInfo() if join is None else join
    # Library error strings can contain interaction/webhook URLs. Suppress raw logs.
    for name in ('discord', 'aiohttp'):
        logger = logging.getLogger(name)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
    import discord
    from discord import app_commands

    adapter = DiscordAdapter(GuildPolicy(config.require_guild()), automation)
    guild = discord.Object(id=int(config.guild_id))

    async def connection_info(*, force=False):
        if isinstance(join, JoinResolver):
            return await asyncio.to_thread(join.resolve, force=force)
        return join

    class Bot(discord.Client):
        def __init__(self):
            intents = discord.Intents.none()
            intents.guilds = True  # No privileged member, presence or message-content intent.
            super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
            self.tree = app_commands.CommandTree(self)
            self.accepting = True
            self.gateway_connected = False
            self.jobs = set()
            self.stop_event = asyncio.Event()
            self.idle_task = None
            self.control_unavailable_notified = False
            self.mutation = asyncio.Lock()

        async def setup_hook(self):
            await self.tree.sync(guild=guild)  # Guild-only commands, not global commands.
            if config.auto_stop_enabled:
                await self.notification_channel()

        async def notification_channel(self):
            channel = self.get_channel(int(config.notification_channel_id)) or await self.fetch_channel(int(config.notification_channel_id))
            if getattr(getattr(channel, 'guild', None), 'id', None) != int(config.guild_id):
                raise ValueError('Wrong notification scope')
            return channel

        def context(self, interaction):
            return Context(str(interaction.guild_id) if interaction.guild_id else None,
                           str(interaction.channel_id) if interaction.channel_id else None,
                           str(interaction.user.id))

        async def permit(self, interaction, action):
            if not self.accepting or len(self.jobs) >= 16:
                await interaction.response.send_message('処理中または終了準備中です。操作は開始していません。', ephemeral=True)
                return False
            if not adapter.policy.allows(self.context(interaction), action):
                await interaction.response.send_message('指定したDiscordサーバー内で利用してください。', ephemeral=True)
                return False
            return True

        async def notify(self, interaction, content):
            # A fresh channel message also works after interaction tokens expire.
            # No content from users, INIs, raw exceptions or journal text is interpolated.
            try:
                channel = interaction.channel
                if channel is None or getattr(getattr(channel, 'guild', None), 'id', None) != int(config.guild_id):
                    raise ValueError('Wrong notification scope')
                await channel.send(f'<@{interaction.user.id}> ' + content,
                                   allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=False, everyone=False, replied_user=False))
            except Exception:
                print('Completion notification failed; the control operation will NOT be repeated.', flush=True)
                try:
                    await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass

        async def perform(self, interaction, action, token=None, *, deferred=False):
            if self.mutation.locked():
                text = '別の起動・停止・再起動が進行中です。重複実行しません。'
                if deferred:
                    await interaction.edit_original_response(content=text, view=None)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
                return
            task = asyncio.current_task()
            self.jobs.add(task)
            try:
                async with self.mutation:
                    if not deferred:
                        await interaction.response.defer(thinking=True)
                    await interaction.edit_original_response(content='起動依頼を受け付けました。準備完了後に通知します。' if action == 'start' else '再起動依頼を受け付けました。停止結果と準備完了を確認します。', view=None)
                    if token is None:
                        result = await asyncio.to_thread(adapter.prepare, self.context(interaction), action, interaction_id=str(interaction.id))
                    else:
                        result = await asyncio.to_thread(adapter.confirm, self.context(interaction), token)
                    if result.get('ok') is True:
                        self.control_unavailable_notified = False
                    info = await connection_info(force=result.get('data', {}).get('changed') is True) if result.get('ok') is True else JoinInfo()
                    await self.notify(interaction, ready_text(result, action, info))
            except Exception:
                await self.notify(interaction, '結果を通知できませんでした。操作を繰り返さず /ark status で確認し、管理者に連絡してください。')
            finally:
                self.jobs.discard(task)

        async def on_disconnect(self):
            self.gateway_connected = False

        async def on_resumed(self):
            self.gateway_connected = True

        async def on_ready(self):
            self.gateway_connected = True
            print('Discord Gateway connected. Guild members may use status/start/restart; no user or role allowlist.', flush=True)
            if self.idle_task is None:
                self.idle_task = asyncio.create_task(self.idle_loop())

        async def idle_loop(self):
            # Wait a FULL hour before the first check; reconnecting does not reset the loop.
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=INTERVAL_SECONDS)
                    return
                except asyncio.TimeoutError:
                    pass
                if not self.is_ready() or not self.gateway_connected or self.mutation.locked():
                    print('Idle check skipped: Discord disconnected or another operation active.', flush=True)
                    continue
                try:
                    if not config.auto_stop_enabled:
                        continue  # LiveConfig may become enabled after first private qualification.
                    await self.notification_channel()
                except Exception:
                    print('Idle check skipped: configuration or notification scope is unavailable.', flush=True)
                    continue
                try:
                    async with self.mutation:
                        result = await asyncio.to_thread(automation.idle_check)
                except Exception:
                    print('Idle check failed; no control retry was attempted.', flush=True)
                    continue
                # Missing IPC before transmission is not a failed game stop. Report
                # its purpose once per outage; never suppress uncertain sent requests.
                unavailable = (not result['ok'] and result.get('error', {}).get('code') == 'service_unavailable'
                               and result.get('action') is None and result.get('request_id') is None)
                if unavailable:
                    if self.control_unavailable_notified:
                        print('Idle check paused: control unavailable; outage already reported.', flush=True)
                        continue
                    text = ('ARKサーバーの管理機能が一時的に応答していません。自動更新などのメンテナンスでサーバーを入れ替えている間によく起こり、'
                            '通常は10〜20分ほどで戻ります（遊んでいる人がいる間は自動更新をしません）。その間は無人時の自動停止をお休みします。'
                            '1時間以上たっても /ark status で状態を確認できない場合は、管理者に連絡してください。')
                else:
                    if result['ok']:
                        self.control_unavailable_notified = False
                    if result['ok'] and not result['data'].get('changed'):
                        print('Idle check: no stop performed.', flush=True)
                        continue
                    text = stopped_text(result)
                try:
                    channel = await self.notification_channel()
                    await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                    if unavailable:
                        self.control_unavailable_notified = True
                except Exception:
                    print('Auto-stop notification failed; no control retry was attempted.', flush=True)

        async def on_error(self, event, *args, **kwargs):
            print('Discord event handler failed; raw payloads and tracebacks are suppressed.', flush=True)

        async def drain(self):
            self.accepting = False
            self.stop_event.set()
            # Never cancel a to_thread mutation: finish recording the accepted operation.
            if self.idle_task is not None:
                await self.idle_task
            pending = [job for job in self.jobs if job is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    client = Bot()
    group = app_commands.Group(name='ark', description='ARKの状態確認・起動・再起動')

    @group.command(name='status', description='ARKの稼働状況・参加人数・Steamからの参加方法を確認')
    async def status(interaction: discord.Interaction):
        if not await client.permit(interaction, 'status'):
            return
        task = asyncio.current_task()
        client.jobs.add(task)
        try:
            await interaction.response.defer(thinking=True)
            result = await asyncio.to_thread(automation.execute, 'status')
            if result.get('ok') is True:
                client.control_unavailable_notified = False
            await interaction.edit_original_response(content=format_status(result, await connection_info()), allowed_mentions=discord.AllowedMentions.none())
        finally:
            client.jobs.discard(task)

    @group.command(name='start', description='停止中のARKを起動し、準備完了時に通知')
    async def start(interaction: discord.Interaction):
        if await client.permit(interaction, 'start'):
            await client.perform(interaction, 'start')

    def restart_warning(count):
        people = str(count) if type(count) is int else '未取得（0人とは扱いません）'
        return f'再起動すると接続が切れます。現在の人数: {people}。60秒以内に確認してください。'

    class RestartConfirmation(discord.ui.View):
        def __init__(self, origin, token, count):
            super().__init__(timeout=60)
            self.origin, self.owner, self.token, self.count = origin, origin.user.id, token, count

        async def on_timeout(self):
            adapter.cancel(client.context(self.origin), self.token)
            try:
                await self.origin.edit_original_response(content='確認が期限切れです。再起動していません。', view=None)
            except Exception:
                pass

        async def on_error(self, interaction, error, item):
            adapter.cancel(client.context(self.origin), self.token)
            await client.notify(interaction, '確認処理を完了できませんでした。操作を繰り返さず /ark status で確認し、管理者に連絡してください。')

        async def interaction_check(self, interaction: discord.Interaction):
            if interaction.user.id != self.owner:
                await interaction.response.send_message('この確認は依頼した本人が操作してください。', ephemeral=True)
                return False
            return await client.permit(interaction, 'restart')

        @discord.ui.button(label='確認して再起動', style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.is_finished():
                await interaction.response.send_message('この確認は期限切れ・使用済みです。', ephemeral=True)
                return
            self.stop()  # Claim this button before any await; repeated clicks cannot race.
            task = asyncio.current_task()
            client.jobs.add(task)
            try:
                await interaction.response.defer(thinking=True, ephemeral=True)
                await self.origin.edit_original_response(content='人数を再確認しています。まだ再起動していません。', view=None)
                current = await asyncio.to_thread(automation.execute, 'status')
                if not current['ok']:
                    adapter.cancel(client.context(interaction), self.token)
                    await interaction.edit_original_response(content=failure_text(current), view=None)
                    return
                count = (current['data'].get('game') or {}).get('players')
                if count != self.count:
                    adapter.cancel(client.context(interaction), self.token)
                    prepared = adapter.prepare(client.context(interaction), 'restart', interaction_id=str(interaction.id))
                    if not prepared['ok']:
                        await interaction.edit_original_response(content=failure_text(prepared), view=None)
                        return
                    view = RestartConfirmation(interaction, prepared['data']['confirmation_token'], count)
                    await interaction.edit_original_response(content='人数が変わったため、もう一度確認してください。\n' + restart_warning(count), view=view)
                    return
                await client.perform(interaction, 'restart', self.token, deferred=True)
            finally:
                client.jobs.discard(task)

        @discord.ui.button(label='キャンセル', style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.is_finished():
                await interaction.response.send_message('この確認は期限切れ・使用済みです。', ephemeral=True)
                return
            self.stop()
            adapter.cancel(client.context(interaction), self.token)
            await interaction.response.edit_message(content='キャンセルしました。再起動していません。', view=None)

    @group.command(name='restart', description='確認後にARKを再起動（接続中のプレイヤーは切断）')
    async def restart(interaction: discord.Interaction):
        if not await client.permit(interaction, 'restart'):
            return
        task = asyncio.current_task()
        client.jobs.add(task)
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
            if not config.shutdown_verified:
                await interaction.edit_original_response(content='停止・保存の安全性が未検証のため、再起動はまだ無効です。')
                return
            current = await asyncio.to_thread(automation.execute, 'status')
            if not current['ok']:
                await interaction.edit_original_response(content=failure_text(current))
                return
            prepared = await asyncio.to_thread(adapter.prepare, client.context(interaction), 'restart', interaction_id=str(interaction.id))
            if not prepared['ok']:
                await interaction.edit_original_response(content=failure_text(prepared))
                return
            count = (current['data'].get('game') or {}).get('players')
            view = RestartConfirmation(interaction, prepared['data']['confirmation_token'], count)
            await interaction.edit_original_response(content=restart_warning(count), view=view)
        finally:
            client.jobs.discard(task)

    @client.tree.error
    async def command_error(interaction: discord.Interaction, error):
        # Exceptions can contain interaction tokens or raw payloads; never log them.
        try:
            if interaction.response.is_done():
                await interaction.followup.send('処理を完了できませんでした。状態を確認してください。', ephemeral=True)
            else:
                await interaction.response.send_message('処理を完了できませんでした。状態を確認してください。', ephemeral=True)
        except Exception:
            pass
        print('Command handler failed; no automatic control retry.', flush=True)

    client.tree.add_command(group, guild=guild)
    return client


def automation_for(config):
    path = os.environ.get('ARK_CONTROL_SOCKET')
    if path:
        from .ark_control_ipc import RemoteAutomation
        from .ark_config import ROOT
        return RemoteAutomation(config, path, ROOT)
    return Automation(config)


async def run(config, token):
    client = build_client(config, automation_for(config), join=JoinResolver.from_environment())
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda loop, context: print('Background task failed; sensitive exception details are suppressed.', flush=True))
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, event.set)
    running = asyncio.create_task(client.start(token, reconnect=True))
    stopping = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait((running, stopping), return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            await running
    finally:
        stopping.cancel()
        await client.drain()
        await client.close()
        await asyncio.gather(running, return_exceptions=True)


def main():
    os.umask(0o077)
    try:
        config = BotConfig.load()
        config.require_guild()
        token = load_token()
        with exclusive_lock(automation_for(config).root / '.discord.lock'):
            asyncio.run(run(config, token))
    except (BotConfigError, ImportError):
        print('Discord bot is not configured. Run the local setup; tokens are never displayed.', file=sys.stderr)
        return 78
    except ControlError:
        print('Another Discord bot instance is running or local control is unavailable.', file=sys.stderr)
        return 78
    except Exception as error:
        print('Discord connection or permissions failed. Inspect setup; raw errors and tokens are suppressed.', file=sys.stderr)
        return 78 if type(error).__name__ in ('LoginFailure', 'Forbidden', 'PrivilegedIntentsRequired') else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
