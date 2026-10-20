"""No Discord SDK/token/network is used: trusted-context adapter tests only."""
import copy
import json
import unittest

from scripts import ark_control
from scripts.discord_adapter import Context, DiscordAdapter, Policy

GUILD = "111111111111111111"
CHANNEL = "222222222222222222"
OPERATOR = "333333333333333333"
ROLE = "444444444444444444"
VIEWER = "555555555555555555"
OTHER = "666666666666666666"
INTERACTION = "777777777777777777"


class SpyController:
    validate = ark_control.Controller.validate

    def __init__(self):
        self.calls = []

    def execute(self, action, params=None, **kwargs):
        self.calls.append((action, copy.deepcopy(params), kwargs))
        if action in ('settings.plan', 'settings.restore.plan'):
            data = {'revision': 'a' * 64, 'changes': params.get('changes', {'taming_speed': {'before': 3, 'after': 1}}), 'changed': True}
        else:
            data = {"changed": action in ark_control.MUTATING_ACTIONS}
        return ark_control.response(kwargs.get("request_id"), action, data=data)


class DiscordTests(unittest.TestCase):
    def setUp(self):
        self.values = {"guild_id": GUILD, "channel_id": CHANNEL, "operator_user_ids": [OPERATOR],
                       "operator_role_ids": [ROLE], "viewer_user_ids": [VIEWER], "viewer_role_ids": []}
        self.policy = Policy.from_dict(self.values)
        self.controller = SpyController()
        self.now = 0
        self.adapter = DiscordAdapter(self.policy, self.controller, clock=lambda: self.now)
        self.operator = Context(GUILD, CHANNEL, OPERATOR)

    def prepare(self, action="stop", params=None, context=None, **kwargs):
        return self.adapter.prepare(self.operator if context is None else context, action, params,
                                    interaction_id=kwargs.pop("interaction_id", INTERACTION), **kwargs)

    def token(self, result):
        self.assertTrue(result["ok"], result)
        return result["data"]["confirmation_token"]

    def test_default_empty_allowlists_deny_everyone_and_bad_policy_fails_closed(self):
        empty = self.values | {key: [] for key in ("operator_user_ids", "operator_role_ids", "viewer_user_ids", "viewer_role_ids")}
        policy = Policy.from_dict(empty)
        self.assertFalse(policy.allows(self.operator, "start"))
        for values in ({}, self.values | {"guild_id": "*"}, self.values | {"operator_user_ids": ["*"]}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Policy.from_dict(values)
        with self.assertRaises(ValueError):
            Policy.load("/does/not/exist/discord-policy.json")

    def test_wrong_guild_channel_dm_user_and_unknown_action_are_denied(self):
        contexts = [Context(None, None, OPERATOR), Context(OTHER, CHANNEL, OPERATOR),
                    Context(GUILD, OTHER, OPERATOR), Context(GUILD, CHANNEL, OTHER),
                    Context(GUILD, CHANNEL, OPERATOR, ("*",))]
        for context in contexts:
            with self.subTest(context=context):
                self.assertEqual(self.prepare("start", context=context)["error"]["code"], "forbidden")
        self.assertEqual(self.prepare("shell")["error"]["code"], "forbidden")
        self.assertEqual(self.controller.calls, [])

    def test_world_import_is_not_implicitly_exposed_to_bot_operators_or_viewers(self):
        for action in ('migration.plan', 'migration.apply', 'migration.recover.plan', 'migration.recover'):
            for context in (self.operator, Context(GUILD, CHANNEL, VIEWER)):
                self.assertEqual(self.prepare(action, {'package_path': '/private/package'}, context=context)['error']['code'], 'forbidden')
        self.assertEqual(self.controller.calls, [])

    def test_viewers_are_read_only_and_operator_role_can_start(self):
        viewer = Context(GUILD, CHANNEL, VIEWER)
        self.assertTrue(self.prepare("status", context=viewer)["ok"])
        self.assertEqual(self.prepare("start", context=viewer)["error"]["code"], "forbidden")
        by_role = Context(GUILD, CHANNEL, OTHER, (ROLE,))
        self.assertTrue(self.prepare("start", context=by_role)["ok"])
        self.assertEqual([call[0] for call in self.controller.calls], ["status", "start"])
        actor = self.controller.calls[-1][2]["actor"]
        self.assertEqual(actor.subject_id, OTHER)
        self.assertEqual(actor.source, "discord")

    def test_confirmation_is_one_use_and_bound_to_user_channel_and_guild(self):
        token = self.token(self.prepare())
        self.assertEqual(self.controller.calls, [])
        for context in (Context(GUILD, CHANNEL, OTHER, (ROLE,)), Context(GUILD, OTHER, OPERATOR), Context(OTHER, CHANNEL, OPERATOR)):
            self.assertEqual(self.adapter.confirm(context, token)["error"]["code"], "forbidden")
        self.assertTrue(self.adapter.confirm(self.operator, token)["ok"])
        self.assertEqual(self.controller.calls[-1][2]["request_id"], INTERACTION)
        self.assertTrue(self.controller.calls[-1][2]["confirmed"])
        self.assertEqual(self.adapter.confirm(self.operator, token)["error"]["code"], "confirmation_expired")
        self.assertEqual(len(self.controller.calls), 1)

    def test_confirmation_expires_without_operation(self):
        token = self.token(self.prepare("restart"))
        self.now = 60
        self.assertEqual(self.adapter.confirm(self.operator, token)["error"]["code"], "confirmation_expired")
        self.assertEqual(self.controller.calls, [])

    def test_current_roles_are_checked_again_on_confirmation(self):
        role_user = Context(GUILD, CHANNEL, OTHER, (ROLE,))
        token = self.token(self.prepare(context=role_user))
        lost_role = Context(GUILD, CHANNEL, OTHER)
        self.assertEqual(self.adapter.confirm(lost_role, token)["error"]["code"], "forbidden")
        self.assertEqual(self.controller.calls, [])

    def test_settings_confirmation_binds_exact_copy_and_revision(self):
        params = {"changes": {"session_name": "New name"}, "expected_revision": "a" * 64}
        token = self.token(self.prepare("settings.apply", params))
        params["changes"]["session_name"] = "Mutated after preview"
        self.assertTrue(self.adapter.confirm(self.operator, token)["ok"])
        self.assertEqual(self.controller.calls[-1][1]["changes"]["session_name"], "New name")
        stale = {"changes": {"max_players": 15}, "expected_revision": "b" * 64}
        self.assertEqual(self.prepare("settings.apply", stale)["error"]["code"], "stale_configuration")

    def test_restore_preview_and_inspection_use_existing_confirmation_boundary(self):
        name = 'config-20260101T000000Z-' + 'a' * 32
        params = {'snapshot': name, 'expected_revision': 'a' * 64}
        prepared = self.prepare('settings.restore', params)
        self.assertEqual(prepared['data']['preview']['changes']['taming_speed']['after'], 1)
        self.assertEqual(self.controller.calls[-1][0], 'settings.restore.plan')
        token = self.token(prepared)
        params['snapshot'] = 'changed-after-prepare'
        self.assertTrue(self.adapter.confirm(self.operator, token)['ok'])
        self.assertEqual(self.controller.calls[-1][1]['snapshot'], name)
        stale = {'snapshot': name, 'expected_revision': 'b' * 64}
        self.assertEqual(self.prepare('settings.restore', stale)['error']['code'], 'stale_configuration')
        prepared = self.prepare('backup.verify', {'backup': 'genesis-20260101-000000.tar.gz'})
        self.assertTrue(prepared['data']['preview']['inspection_only'])
        self.assertFalse(prepared['data']['preview']['disconnects_players'])

    def test_no_raw_confirm_or_arbitrary_commands_can_be_injected(self):
        result = self.prepare("stop", {"confirmed": True})
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.prepare("start", interaction_id="../../escape")["error"]["code"], "invalid_request")

    def test_pending_confirmations_are_bounded(self):
        for _ in range(256):
            self.assertTrue(self.prepare()["ok"])
        self.assertEqual(self.prepare()["error"]["code"], "confirmation_limit")
        self.now = 61
        self.assertTrue(self.prepare()["ok"])
        self.assertEqual(len(self.adapter.pending), 1)
        self.assertEqual(self.controller.calls, [])

    def test_error_responses_never_echo_unknown_payload(self):
        result = self.prepare("stop", {"SECRET_INPUT": "SECRET_INPUT"})
        self.assertNotIn("SECRET_INPUT", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
