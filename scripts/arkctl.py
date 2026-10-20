#!/usr/bin/env python3
"""JSON CLI for the local ARK controller; never accepts shell commands or INI paths."""
import argparse
import json
import os
import sys

if __package__:
    from .ark_control import ControlError, Controller, response
else:
    from ark_control import ControlError, Controller, response


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default errors can echo user-provided values.
        raise ControlError("invalid_request")


def parser():
    common = Parser(add_help=False)
    common.add_argument("--request-id", help="Reuse the same ID only for retries of the same operation")
    common.add_argument("--confirm", action="store_true", help="Explicitly acknowledge a disruptive operation")
    result = Parser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("status", "start", "stop", "restart", "backup"):
        commands.add_parser(name, parents=[common])
    verify = commands.add_parser('backup-verify', parents=[common], help='Inspect a completed archive without extracting it')
    verify.add_argument('backup')
    settings = commands.add_parser("settings")
    options = settings.add_subparsers(dest="setting_action", required=True, parser_class=Parser)
    options.add_parser("show", parents=[common])
    options.add_parser("plan", parents=[common], help="Read a changes object from standard input")
    apply = options.add_parser("apply", parents=[common], help="Apply the previously previewed changes while stopped")
    apply.add_argument("--revision", required=True)
    restore_plan = options.add_parser('restore-plan', parents=[common], help='Preview allowlisted keys from a config snapshot')
    restore_plan.add_argument('snapshot')
    restore = options.add_parser('restore', parents=[common], help='Restore allowlisted keys while stopped; never restores secrets')
    restore.add_argument('snapshot')
    restore.add_argument('--revision', required=True)
    return result


def read_changes(stream):
    text = stream.read(8193)
    if len(text) > 8192:
        raise ControlError("invalid_request")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ControlError("invalid_request")
            result[key] = value
        return result

    try:
        result = json.loads(text, object_pairs_hook=unique_pairs)
    except (ValueError, RecursionError):
        raise ControlError("invalid_request") from None
    if not isinstance(result, dict):
        raise ControlError("invalid_request")
    return result


def main(argv=None, controller=None, stdin=None):
    os.umask(0o077)
    try:
        args = parser().parse_args(argv)
        action = args.command if args.command != "settings" else f"settings.{args.setting_action.replace('-', '.')}"
        if action == 'backup-verify':
            action = 'backup.verify'
        params = {'backup': args.backup} if action == 'backup.verify' else {}
        if action in ("settings.plan", "settings.apply"):
            params["changes"] = read_changes(sys.stdin if stdin is None else stdin)
        if action in ('settings.restore', 'settings.restore.plan'):
            params['snapshot'] = args.snapshot
        if action in ("settings.apply", 'settings.restore'):
            params["expected_revision"] = args.revision
        result = (Controller() if controller is None else controller).execute(
            action, params, request_id=args.request_id, confirmed=args.confirm,
        )
    except ControlError as error:
        result = response(None, None, code=error.code)
    except (OSError, ValueError, RecursionError):
        result = response(None, None, code="invalid_request")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
