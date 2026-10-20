"""SDK-independent Discord scope/confirmation seam. Does not connect to Discord.

Contexts MUST be built from verified SDK interactions, not user-provided JSON or messages.
Run blocking prepare/confirm calls in a worker after deferring the Discord interaction.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import threading
import time

if __package__:
    from .ark_control import ACTIONS, CONFIRM_ACTIONS, READ_ACTIONS, Actor, ControlError, Controller, response
else:
    from ark_control import ACTIONS, CONFIRM_ACTIONS, READ_ACTIONS, Actor, ControlError, Controller, response


def snowflake(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{16,19}", value) is not None


@dataclass(frozen=True)
class Context:
    guild_id: str | None
    channel_id: str | None
    user_id: str
    role_ids: tuple[str, ...] = ()

    def identity(self):
        return self.guild_id, self.channel_id, self.user_id


@dataclass(frozen=True)
class Policy:
    guild_id: str
    channel_id: str
    operator_user_ids: frozenset[str]
    operator_role_ids: frozenset[str]
    viewer_user_ids: frozenset[str]
    viewer_role_ids: frozenset[str]

    @classmethod
    def from_dict(cls, values):
        keys = {"guild_id", "channel_id", "operator_user_ids", "operator_role_ids", "viewer_user_ids", "viewer_role_ids"}
        if not isinstance(values, dict) or set(values) != keys:
            raise ValueError("Discord policy must explicitly define all documented allowlists.")
        if not snowflake(values["guild_id"]) or not snowflake(values["channel_id"]):
            raise ValueError("Discord policy requires valid guild and channel IDs.")
        result = {"guild_id": values["guild_id"], "channel_id": values["channel_id"]}
        for key in keys - {"guild_id", "channel_id"}:
            ids = values[key]
            if not isinstance(ids, list) or len(ids) > 256 or not all(snowflake(value) for value in ids):
                raise ValueError("Discord allowlists must be lists of valid IDs.")
            result[key] = frozenset(ids)
        return cls(**result)

    @classmethod
    def load(cls, path):
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            raise ValueError("Discord policy is missing or invalid; control remains disabled.") from None

    def allows(self, context, action):
        # Internal imports and unattended stopping are never Discord commands.
        if not isinstance(context, Context) or not isinstance(action, str) or action not in ACTIONS or action.startswith('migration.') or action == 'stop.idle':
            return False
        if not all(snowflake(value) for value in context.identity()):
            return False  # Includes DMs and missing channel/guild/user IDs.
        if context.guild_id != self.guild_id or context.channel_id != self.channel_id:
            return False
        if not isinstance(context.role_ids, tuple) or not all(snowflake(value) for value in context.role_ids):
            return False
        roles = set(context.role_ids)
        operator = context.user_id in self.operator_user_ids or bool(roles & self.operator_role_ids)
        viewer = context.user_id in self.viewer_user_ids or bool(roles & self.viewer_role_ids)
        return operator or (action in READ_ACTIONS and viewer)


@dataclass(frozen=True)
class GuildPolicy:
    """Every member in ONE guild; no per-user, role or channel allowlists."""
    guild_id: str

    def __post_init__(self):
        if not snowflake(self.guild_id):
            raise ValueError('A fixed Discord guild ID is required.')

    def allows(self, context, action):
        return (isinstance(context, Context) and isinstance(action, str)
                and action in ('status', 'start', 'restart')
                and all(snowflake(value) for value in context.identity())
                and context.guild_id == self.guild_id)


class DiscordAdapter:
    def __init__(self, policy, controller=None, *, clock=time.monotonic):
        if not isinstance(policy, (Policy, GuildPolicy)):
            raise ValueError("An explicit Discord policy is required.")
        self.policy = policy
        self.controller = Controller() if controller is None else controller
        self.clock = clock
        self.pending = {}
        self.lock = threading.Lock()

    def _actor(self, context):
        return Actor("discord", context.user_id, context.guild_id, context.channel_id)

    def prepare(self, context, action, params=None, *, interaction_id):
        if not self.policy.allows(context, action):
            return response(None, None, code="forbidden")
        if not snowflake(interaction_id):
            return response(None, None, code="invalid_request")
        params = {} if params is None else params
        try:
            self.controller.validate(action, params, interaction_id, True)
            # Keep an immutable JSON copy: callers cannot change a pending command in-place.
            params = json.loads(json.dumps(params))
        except (ControlError, ValueError, TypeError, RecursionError):
            return response(interaction_id, action, code="invalid_request")
        if action not in CONFIRM_ACTIONS:
            return self.controller.execute(action, params, request_id=interaction_id, actor=self._actor(context))
        preview = {"disconnects_players": action in ("stop", "restart", "backup")}
        if action in ('settings.apply', 'settings.restore'):
            plan_action = 'settings.plan' if action == 'settings.apply' else 'settings.restore.plan'
            plan_params = {'changes': params['changes']} if action == 'settings.apply' else {'snapshot': params['snapshot']}
            plan = self.controller.execute(plan_action, plan_params, actor=self._actor(context))
            if not plan["ok"]:
                return plan
            if plan["data"]["revision"] != params["expected_revision"]:
                return response(interaction_id, action, code="stale_configuration")
            preview = plan["data"]
        elif action == 'backup.verify':
            preview = {'disconnects_players': False, 'inspection_only': True, 'backup': params['backup'], 'restore_tested': False}
        with self.lock:
            now = self.clock()
            self.pending = {token: item for token, item in self.pending.items() if item["expires"] > now}
            if len(self.pending) >= 256:
                return response(interaction_id, action, code="confirmation_limit")
            token = secrets.token_urlsafe(24)
            self.pending[token] = {"expires": now + 60, "identity": context.identity(),
                                   "action": action, "params": params, "request_id": interaction_id}
        return response(interaction_id, action, data={"confirmation_required": True,
                        "confirmation_token": token, "expires_in_seconds": 60, "preview": preview})

    def cancel(self, context, token):
        """Discard only this requester's in-memory confirmation; never execute it."""
        if not isinstance(context, Context) or not isinstance(token, str):
            return False
        with self.lock:
            item = self.pending.get(token)
            if item is None or item['identity'] != context.identity():
                return False
            del self.pending[token]
            return True

    def confirm(self, context, token):
        if not isinstance(token, str) or len(token) > 128:
            return response(None, None, code="confirmation_expired")
        with self.lock:
            item = self.pending.get(token)
            if item is None:
                return response(None, None, code="confirmation_expired")
            if self.clock() >= item["expires"]:
                del self.pending[token]
                return response(None, None, code="confirmation_expired")
            if not isinstance(context, Context) or context.identity() != item["identity"]:
                return response(None, None, code="forbidden")
            # Re-read current SDK roles on the confirmation interaction, not the original roles.
            if not self.policy.allows(context, item["action"]):
                del self.pending[token]
                return response(None, None, code="forbidden")
            del self.pending[token]  # Consume before execution, including failed operations.
        return self.controller.execute(item["action"], item["params"], request_id=item["request_id"],
                                       confirmed=True, actor=self._actor(context))
