"""Local, transport-independent control API. No Discord, HTTP, sudo or shell."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

if __package__:
    from . import ark_config, ark_recovery, ark_migration, ark_shutdown
    from .query import QueryError, query_server
else:
    import ark_config
    import ark_recovery
    import ark_migration
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import ark_shutdown
    from query import QueryError, query_server

API_VERSION = 1
GAME_UNIT = "ark-genesis.service"
BACKUP_UNIT = "ark-genesis-backup.service"
READ_ACTIONS = frozenset(("status", "settings.show", "settings.plan", "settings.restore.plan", "migration.plan", "migration.recover.plan"))
MUTATING_ACTIONS = frozenset(("start", "stop", "stop.idle", "restart", "backup", "backup.verify", "settings.apply", "settings.restore", "migration.apply", "migration.recover"))
CONFIRM_ACTIONS = MUTATING_ACTIONS - {"start"}
ACTIONS = READ_ACTIONS | MUTATING_ACTIONS
MESSAGES = {
    "invalid_request": "Unsupported command, parameters or request ID.",
    "confirmation_required": "This operation requires explicit confirmation.",
    "busy": "Another control, backup, game or maintenance operation is running.",
    "service_unavailable": "The user systemd service or user bus is unavailable.",
    "operation_failed": "The service operation failed; inspect the local service journal.",
    "shutdown_failed": "Neither clean exit nor complete qualified save/exit evidence was verified. No automatic restart; inspect the preserved shutdown record and Saved data.",
    "outcome_unknown": "Completion could not be verified. Inspect status before issuing a new request.",
    "not_running": "Restart requires an active server; use start for a stopped server.",
    "server_running": "Stop the server before applying settings; it will not be stopped automatically.",
    "invalid_configuration": "Configuration is missing, unsafe or invalid; inspect it locally.",
    "configuration_recovery_required": "Configuration rollback failed. Keep the server stopped and recover from the private snapshot.",
    "stale_configuration": "Configuration changed after the preview; generate a new preview.",
    "request_conflict": "This request ID was already used for different parameters or an actor.",
    "storage_unavailable": "Private request or audit storage is unavailable; no new operation was started.",
    "internal_error": "The control operation failed; inspect local state before retrying.",
    "mods_not_ready": "Active MOD content is missing. Install the Workshop content before starting; do not start a modded world without its mods.",
    "archive_invalid": "The archive is unavailable, corrupt, incomplete or contains unsafe entries. No files were extracted.",
    "archive_limit": "Archive verification exceeded the time or size limit. Integrity is NOT established.",
    "migration_invalid": "The migration package or destination is unavailable, unsafe or incomplete. Inspect the private migration snapshot if one was created.",
    "migration_limit": "Migration verification exceeded its time, entry, byte or free-space limit.",
    "forbidden": "This server, channel, user or role is not permitted to perform the operation.",
    "confirmation_expired": "The confirmation is missing, already used or expired.",
    "confirmation_limit": "Too many pending confirmations; wait for an existing confirmation to expire.",
    "shutdown_unverified": "Automatic stopping and restart are disabled until shutdown and saved-world validation is complete.",
    "readiness_timeout": "The process started but readiness was not verified before the deadline. It was not stopped or restarted automatically.",
    "automation_blocked": "An earlier automation operation failed or was interrupted. Inspect its record before allowing another mutation.",
}


class ControlError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def response(request_id, action, *, data=None, code=None):
    result = {"api_version": API_VERSION, "request_id": request_id, "action": action, "ok": code is None}
    result["error" if code else "data"] = {"code": code, "message": MESSAGES[code]} if code else data
    return result


@dataclass(frozen=True)
class Actor:
    # Supplied by trusted local code, never by an unverified Discord message.
    source: str
    subject_id: str
    guild_id: str | None = None
    channel_id: str | None = None

    def __post_init__(self):
        if self.source not in ("local", "discord"):
            raise ControlError("invalid_request")
        for value in (self.subject_id, self.guild_id, self.channel_id):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value)):
                raise ControlError("invalid_request")
        if not self.subject_id or (self.source == "discord" and (not self.guild_id or not self.channel_id)):
            raise ControlError("invalid_request")


class SystemdBackend:
    def _run(self, arguments, timeout):
        try:
            runtime = f"/run/user/{os.getuid()}"
            environment = os.environ | {"XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}
            result = subprocess.run(["/usr/bin/systemctl", "--user", *arguments], capture_output=True,
                                    text=True, timeout=timeout, check=False, env=environment)
        except subprocess.TimeoutExpired:
            raise ControlError("outcome_unknown") from None
        except OSError:
            raise ControlError("service_unavailable") from None
        if result.returncode:
            # Never forward stderr/journal text, which might contain command lines or secrets.
            raise ControlError("operation_failed")
        return result.stdout

    def state(self, unit=GAME_UNIT):
        if unit not in (GAME_UNIT, BACKUP_UNIT):
            raise ControlError("invalid_request")
        try:
            output = self._run(["show", unit, "--property=LoadState", "--property=ActiveState",
                                "--property=SubState", "--property=Result", "--property=MainPID", "--property=ExecMainStatus", "--property=ExecMainCode", "--property=InvocationID"], 10)
        except ControlError:
            raise ControlError("service_unavailable") from None
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if values.get("LoadState") != "loaded" or values.get("ActiveState") not in (
            "active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance",
        ):
            raise ControlError("service_unavailable")
        result = {key: values.get(key, "unknown") for key in ("ActiveState", "SubState", "Result")}
        result['InvocationID'] = values.get('InvocationID', '')
        for key in ('MainPID', 'ExecMainStatus', 'ExecMainCode'):
            raw = values.get(key, '')
            result[key] = int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 20 else None
        return result

    def metrics(self):
        keys = ("MemoryCurrent", "CPUUsageNSec", "MainPID", "NRestarts", "ExecMainStatus", "ActiveEnterTimestampMonotonic")
        output = self._run(["show", GAME_UNIT, *("--property=" + key for key in keys)], 10)
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        def number(key):
            raw = values.get(key, "")
            return int(raw) if raw.isascii() and raw.isdigit() and int(raw) < 2**63 else None
        entered = number("ActiveEnterTimestampMonotonic")
        return {"service_memory": number("MemoryCurrent"), "cpu_usage_ns": number("CPUUsageNSec"),
                "pid": number("MainPID"), "restarts": number("NRestarts"), "exit_status": number("ExecMainStatus"),
                "uptime_seconds": max(0, time.monotonic() - entered / 1000000) if entered else None}

    def backup_timer(self):
        output = self._run(["show", "ark-genesis-backup.timer", "--property=LoadState", "--property=ActiveState", "--property=UnitFileState"], 10)
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        state = values.get("ActiveState")
        return {"known": values.get("LoadState") == "loaded", "state": state if state in ("active", "inactive", "failed", "activating", "deactivating") else "unavailable",
                "enabled": values.get("UnitFileState") in ("enabled", "enabled-runtime")}

    def operate(self, action):
        if action not in ("start", "stop", "restart", "backup"):
            raise ControlError("invalid_request")
        unit = BACKUP_UNIT if action == "backup" else GAME_UNIT
        verb = "start" if action == "backup" else action
        self._run([verb, unit], 1830 if action == "backup" else 240)


@contextmanager
def exclusive_lock(path):
    path = ark_config.lock_path(path)
    with path.open("a") as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("busy") from None
        yield


class Controller:
    def __init__(self, root=ark_config.ROOT, backend=None, probe=None):
        self.root = Path(root)
        if backend is None and ((self.root / 'config/docker-runtime.json').exists()
                                or (self.root / 'config/docker-runtime.json').is_symlink()):
            from .ark_docker import DockerBackend
            backend = DockerBackend(self.root)
        self.backend = backend if backend is not None else SystemdBackend()
        self.probe = probe if probe is not None else query_server

    def shutdown_guard(self):
        return (self.backend.shutdown_guard() if hasattr(self.backend, 'shutdown_guard')
                else ark_shutdown.ShutdownGuard(self.root))

    def validate(self, action, params, request_id, confirmed):
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(params, dict):
            raise ControlError("invalid_request")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            raise ControlError("invalid_request")
        expected = {"stop.idle": {"minimum_uptime_seconds"}, "settings.plan": {"changes"}, "settings.apply": {"changes", "expected_revision"},
                    "settings.restore.plan": {"snapshot"}, "settings.restore": {"snapshot", "expected_revision"},
                    "backup.verify": {"backup"}, "migration.plan": {"package_path"},
                    "migration.apply": {"package_path", "expected_revision"},
                    "migration.recover.plan": {"package_path", "snapshot"},
                    "migration.recover": {"package_path", "snapshot", "expected_revision"}}.get(action, set())
        if set(params) != expected or type(confirmed) is not bool:
            raise ControlError("invalid_request")
        if action == "stop.idle" and (type(params["minimum_uptime_seconds"]) is not int or not 300 <= params["minimum_uptime_seconds"] <= 86400):
            raise ControlError("invalid_request")
        if action in ("settings.plan", "settings.apply"):
            if not isinstance(params["changes"], dict) or not params["changes"] or not set(params["changes"]) <= ark_config.EDITABLE_SETTINGS:
                raise ControlError("invalid_request")
        if action in ("migration.plan", "migration.apply", "migration.recover.plan", "migration.recover"):
            try:
                ark_migration.validate_source(params['package_path'])
                if action in ('migration.recover.plan', 'migration.recover'):
                    ark_migration.validate_snapshot(params['snapshot'])
            except ark_migration.MigrationInvalidError:
                raise ControlError('invalid_request') from None
        if action in ("settings.apply", "settings.restore", "migration.apply", "migration.recover") and (not isinstance(params["expected_revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", params["expected_revision"])):
            raise ControlError("invalid_request")
        if action in ("settings.restore", "settings.restore.plan") and (not isinstance(params["snapshot"], str) or not re.fullmatch(ark_config.SNAPSHOT_ID, params["snapshot"])):
            raise ControlError("invalid_request")
        if action == "backup.verify" and (not isinstance(params["backup"], str) or not re.fullmatch(ark_recovery.BACKUP_ID, params["backup"])):
            raise ControlError("invalid_request")
        if action in CONFIRM_ACTIONS and not confirmed:
            raise ControlError("confirmation_required")

    @staticmethod
    def _verify_stopped(state):
        if state["ActiveState"] == "failed":
            raise ControlError("shutdown_failed")
        if state["ActiveState"] != "inactive" or state.get("Result") in (None, "", "unknown"):
            raise ControlError("outcome_unknown")
        if state["Result"] != "success":
            raise ControlError("shutdown_failed")
        # Real systemd state always includes these. Legacy injected backends may not.
        if 'MainPID' in state and (type(state['MainPID']) is not int or state['MainPID'] != 0):
            raise ControlError('outcome_unknown')
        if 'ExecMainStatus' in state:
            if type(state['ExecMainStatus']) is not int:
                raise ControlError('outcome_unknown')
            if state['ExecMainStatus'] != 0:
                raise ControlError('shutdown_failed')

    def verify_operational_stopped(self, state):
        """Clean exit OR separately qualified warning; never calls the latter clean."""
        if hasattr(self.backend, 'shutdown_guard'):
            try:
                return self.shutdown_guard().verify_stopped(state)
            except (OSError, ValueError, KeyError, TypeError):
                raise ControlError('shutdown_failed') from None
        try:
            self._verify_stopped(state)
            return {'clean_exit': True}
        except ControlError:
            try:
                return ark_shutdown.ShutdownGuard(self.root).verify_stopped(state)
            except (OSError, ValueError, KeyError, TypeError):
                raise ControlError('shutdown_failed') from None

    def verify_startable_stopped(self, state):
        if hasattr(self.backend, 'shutdown_guard'):
            try:
                return self.shutdown_guard().verify_startable(state)
            except (OSError, ValueError, KeyError, TypeError):
                raise ControlError('shutdown_failed') from None
        # A fresh user-manager after a reboot may show factory inactive/success/0.
        # That must not erase a previous observed crash or interrupted game.
        try:
            self._verify_stopped(state)
            if ark_shutdown.policy(self.root):
                guard = ark_shutdown.ShutdownGuard(self.root)
                live_path = guard.directory / 'live.json'
                if live_path.exists() or live_path.is_symlink():
                    live = ark_shutdown.private_json(live_path)
                    run_id = live.get('run_id')
                    if not isinstance(run_id, str) or re.fullmatch('[0-9a-f]{32}', run_id) is None:
                        raise ark_shutdown.ShutdownError('Invalid previous observer instance.')
                    observed = ark_shutdown.private_json(guard.directory / 'runtime' / (run_id + '.json'))
                    if (observed.get('phase') != 'exited' or observed.get('run_id') != run_id
                            or observed.get('invocation_id') != live.get('invocation_id')):
                        raise ark_shutdown.ShutdownError('Previous game exit remains unknown.')
                    if (observed.get('exit_signal') is None and type(observed.get('exit_code')) is int
                            and observed['exit_code'] == 0 and not observed.get('observation_fault')):
                        return {'clean_exit': True}
                    # Validate a historical receipt in ITS boot/invocation, then
                    # independently hash all current Saved. Never rewrite it as
                    # a current clean service exit or ignore an unknown history.
                    class HistoricalGuard(ark_shutdown.ShutdownGuard):
                        def status_receipt(inner, historical_state):
                            value = ark_shutdown.private_json(inner.directory / 'last-stop.json')
                            if (type(value.get('schema')) is not int or value['schema'] != 1
                                    or value.get('clean_exit') is not False or value.get('warning') != 'expected_native_abort'
                                    or value.get('boot_id') != live.get('boot_id') or value.get('observation') != observed
                                    or value.get('service_record') != historical_state):
                                raise ark_shutdown.ShutdownError('Historical exit does not match.')
                            return value
                    receipt = ark_shutdown.private_json(guard.directory / 'last-stop.json')
                    return HistoricalGuard(self.root).verify_stopped(receipt['service_record'])
            return {'clean_exit': True}
        except (OSError, ValueError, KeyError, TypeError):
            raise ControlError('shutdown_failed') from None
        except ControlError:
            pass
        try:
            return self.verify_operational_stopped(state)
        except ControlError:
            try:
                return ark_shutdown.ShutdownGuard(self.root).verify_initial_review(state)
            except (OSError, ValueError, KeyError, TypeError):
                raise ControlError('shutdown_failed') from None

    def reviewed_blocking_operation(self):
        if hasattr(self.backend, 'shutdown_guard'):
            return False  # A legacy review never clears a Docker failure latch.
        try:
            ark_shutdown.ShutdownGuard(self.root).verify_initial_review(self.backend.state(), check_operation=True)
            return True
        except (OSError, ValueError, KeyError, TypeError, ControlError):
            return False

    def _recovery_service_record(self):
        """Explicit recovery can preserve a stopped SIGABRT record, not clear it.

        Normal migration still requires a clean stop. An unavailable PID or an
        unfamiliar failure is not evidence that replacement is safe.
        """
        state = self.backend.state()
        if state['ActiveState'] == 'active':
            raise ControlError('server_running')
        if state['ActiveState'] not in ('inactive', 'failed'):
            raise ControlError('outcome_unknown')
        if not hasattr(self.backend, 'metrics'):
            raise ControlError('outcome_unknown')
        metrics = self.backend.metrics()
        if not isinstance(metrics, dict):
            raise ControlError('outcome_unknown')
        pid, status = metrics.get('pid'), metrics.get('exit_status')
        if type(pid) is not int or pid != 0 or type(status) is not int:
            raise ControlError('outcome_unknown')
        if state['ActiveState'] == 'failed':
            if state.get('Result') != 'core-dump' or status != 6:
                raise ControlError('shutdown_failed')
        else:
            self._verify_stopped(state)
            if status != 0:
                raise ControlError('shutdown_failed')
        return state | {'MainPID': pid, 'ExecMainStatus': status}

    @staticmethod
    def _boot_id():
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def _request_grace(self, minimum):
        path = self.root / 'logs/control/start-grace.json'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return False
        try:
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                    return True
                value = json.loads(stream.read(4097))
            stamp = value['monotonic']
            if not isinstance(value.get('boot_id'), str) or re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['boot_id']) is None:
                return True
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
                return True
            return value['boot_id'] == self._boot_id() and time.monotonic() - stamp < minimum
        except (OSError, ValueError, KeyError, TypeError):
            return True  # Missing/invalid evidence cannot authorize stopping.

    def _idle_reason(self, minimum_uptime, expected_pid=None):
        """Fail closed, called twice inside the control AND backup locks."""
        state = self.backend.state()
        if state['ActiveState'] != 'active' or state.get('Result') != 'success':
            return 'not_running', None
        metrics = self.backend.metrics() if hasattr(self.backend, 'metrics') else {}
        pid, uptime = metrics.get('pid'), metrics.get('uptime_seconds')
        if type(pid) is not int or pid <= 0 or (expected_pid is not None and pid != expected_pid):
            return 'process_unknown_or_changed', None
        if type(uptime) not in (int, float) or not math.isfinite(uptime) or uptime < minimum_uptime:
            return 'startup_grace_or_unknown_uptime', pid
        if self._request_grace(minimum_uptime):
            return 'request_grace_or_unknown_record', pid
        try:
            ark_config.launch_url(self.root)  # Also reject lost initialization/auth/MODs before an unattended save/stop.
            settings = ark_config.load_settings(self.root)
            game = self.probe('127.0.0.1', settings['query_port'], timeout=2)
        except (OSError, QueryError):
            return 'query_unavailable', pid
        if not isinstance(game, dict) or game.get('map') != settings['map'] or type(game.get('players')) is not int:
            return 'player_count_unknown', pid
        return (None if game['players'] == 0 else 'players_present'), pid

    def _dispatch(self, action, params):
        if action == "settings.show":
            return {"settings": ark_config.load_editable_settings(self.root), "editable": sorted(ark_config.EDITABLE_SETTINGS), "rules": ark_config.RULES}
        if action == "settings.plan":
            return ark_config.plan_settings(self.root, params["changes"])
        if action == "settings.restore.plan":
            return ark_recovery.plan_restore(self.root, params["snapshot"])[0]
        if action == 'migration.plan':
            return ark_migration.plan_migration(self.root, params['package_path'])
        if action == 'migration.recover.plan':
            record = self._recovery_service_record()
            return ark_migration.plan_recovery(self.root, params['package_path'], params['snapshot'], record)
        if action == "status":
            state = self.backend.state()
            info, latency, metrics, timer = None, None, None, None
            if state["ActiveState"] == "active":
                try:
                    started = time.monotonic()
                    info = self.probe("127.0.0.1", ark_config.load_settings(self.root)["query_port"], timeout=2)
                    latency = round((time.monotonic() - started) * 1000, 1)
                except (OSError, QueryError):
                    pass
            try:
                metrics = self.backend.metrics() if hasattr(self.backend, "metrics") else None
                timer = self.backend.backup_timer() if hasattr(self.backend, "backup_timer") else None
            except ControlError:
                pass  # Optional monitoring must not hide otherwise readable service state.
            return {"service": state, "query_responding": info is not None, "game": info,
                    "query_latency_ms": latency, "metrics": metrics, "backup_timer": timer}
        if action == 'backup.verify':
            return ark_recovery.verify_backup(self.root, params['backup'])
        idle_recheck = None
        if action == 'stop.idle':
            reason, pid = self._idle_reason(params['minimum_uptime_seconds'])
            if reason is None:
                time.sleep(5)
                reason, _ = self._idle_reason(params['minimum_uptime_seconds'], expected_pid=pid)
            if reason is not None:
                return {'changed': False, 'reason': reason}
            idle_recheck = lambda: self._idle_reason(params['minimum_uptime_seconds'], expected_pid=pid)[0]
            action = 'stop'
        state = self.backend.state()
        active = state["ActiveState"]
        if active not in ("active", "inactive", "failed"):
            raise ControlError("busy")
        if action == 'migration.recover':
            record = self._recovery_service_record()
            def verify_recovery_stopped():
                if self._recovery_service_record() != record:
                    raise ark_config.StaleConfigError('The stopped-service record changed after preview.')
            return ark_migration.apply_migration(self.root, params['package_path'], params['expected_revision'],
                verify_recovery_stopped, recovery_snapshot=params['snapshot'], service_record=record)
        if action == 'migration.apply':
            if active == 'active':
                raise ControlError('server_running')
            self._verify_stopped(state)
            return ark_migration.apply_migration(self.root, params['package_path'], params['expected_revision'],
                lambda: self._verify_stopped(self.backend.state()))
        if action in ("settings.apply", "settings.restore"):
            if active == "active":
                raise ControlError("server_running")
            if action == "settings.restore":
                return ark_recovery.restore_settings(self.root, params["snapshot"], params["expected_revision"])
            return ark_config.patch_settings(self.root, params["changes"], params["expected_revision"])
        if action in ('start', 'restart'):
            # Also protect an already-running server after a player's fresh start request.
            ark_config.atomic_write(self.root / 'logs/control/start-grace.json', json.dumps({
                'boot_id': self._boot_id(), 'monotonic': time.monotonic(),
            }).encode())
        if action == 'start' and active == 'active':
            return {'changed': False, 'service': state}
        if action == 'stop' and active in ('inactive', 'failed'):
            stopped = self.verify_operational_stopped(state)
            return {'changed': False, 'service': state, 'shutdown': stopped,
                    'warnings': ['expected_native_abort'] if not stopped['clean_exit'] else []}
        if action == "restart" and active != "active":
            raise ControlError("not_running")
        if action in ("start", "restart"):
            ark_config.launch_url(self.root)  # Authentication preflight; never returned or logged.
        start_warning = None
        if action == "start":
            start_warning = self.verify_startable_stopped(state)
            # Do not collide with a manually started server holding its lifetime lock.
            with ark_config.server_lock(self.root):
                pass
        # systemctl restart can hide an abnormal stop behind a successful start.
        # Keep the game stopped if shutdown failed; do not erase that evidence.
        stopped_data = None
        if action in ('stop', 'restart'):
            try:
                qualified = ark_shutdown.policy(self.root)
                if qualified:
                    stopped_data = self.shutdown_guard().stop(self.backend, idle_recheck)
                    if action == 'stop' or not stopped_data.get('changed'):
                        return stopped_data
                elif action == 'restart':
                    self.backend.operate('stop')
                    try:
                        stopped_state = self.backend.state()
                    except ControlError:
                        raise ControlError('outcome_unknown') from None
                    self._verify_stopped(stopped_state)
            except (ark_shutdown.ShutdownError, OSError, ValueError):
                raise ControlError('shutdown_failed') from None
        self.backend.operate("start" if action == "restart" else action)
        try:
            after = self.backend.state()
        except ControlError:
            raise ControlError("outcome_unknown") from None
        if action == "stop":
            self._verify_stopped(after)
        if action in ("start", "restart") and after["ActiveState"] != "active":
            raise ControlError("operation_failed")
        data = {"changed": True, "service": after, "readiness_verified": False}
        if stopped_data:
            data.update(shutdown=stopped_data['shutdown'], warnings=stopped_data.get('warnings', []))
        elif start_warning and not start_warning['clean_exit']:
            data.update(shutdown=start_warning, warnings=[start_warning['warning']])
        return data

    def _audit(self, directory, request_id, action, actor, event, code=None):
        record = {"time": datetime.now(timezone.utc).isoformat(), "request_id": request_id,
                  "action": action, "actor": asdict(actor), "event": event, "code": code}
        path = directory / "audit.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        ark_config.sync_directory(directory)

    def _mutate(self, action, params, request_id, actor):
        with exclusive_lock(self.root / ".control.lock"):
            directory = self.root / "logs/control"
            requests = directory / "requests"
            requests.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            requests.chmod(0o700)
            for parent in (self.root, self.root / "logs", directory, requests):
                ark_config.sync_directory(parent)
            fingerprint = hashlib.sha256(json.dumps({"action": action, "params": params, "actor": asdict(actor)}, sort_keys=True).encode()).hexdigest()
            path = requests / f"{request_id}.json"
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous["fingerprint"] != fingerprint:
                    raise ControlError("request_conflict")
                if "result" not in previous:
                    raise ControlError("outcome_unknown")
                return previous["result"]
            try:
                # Persist intent BEFORE an operation. A crash never causes silent re-execution.
                ark_config.atomic_write(path, json.dumps({"fingerprint": fingerprint, "state": "pending"}).encode())
                self._audit(directory, request_id, action, actor, "accepted")
            except OSError:
                raise ControlError("storage_unavailable") from None
            try:
                if action == "backup":
                    # Detect an existing backup, then release before the child acquires it.
                    # The script's own lock still protects a timer race after this check.
                    with exclusive_lock(self.root / ".backup.lock"):
                        pass
                    data = self._dispatch(action, params)
                else:
                    # Also exclude the existing scheduled/manual backup's stop/restart cycle.
                    with exclusive_lock(self.root / ".backup.lock"):
                        data = self._dispatch(action, params)
                result = response(request_id, action, data=data)
            except ControlError as error:
                result = response(request_id, action, code=error.code)
            except ark_migration.MigrationInvalidError:
                result = response(request_id, action, code='migration_invalid')
            except ark_migration.MigrationLimitError:
                result = response(request_id, action, code='migration_limit')
            except ark_config.ConfigRecoveryError:
                result = response(request_id, action, code="configuration_recovery_required")
            except ark_config.StaleConfigError:
                result = response(request_id, action, code="stale_configuration")
            except ark_config.ConfigBusyError:
                result = response(request_id, action, code="busy")
            except ark_config.ModFilesMissingError:
                result = response(request_id, action, code="mods_not_ready")
            except ark_recovery.ArchiveInvalidError:
                result = response(request_id, action, code="archive_invalid")
            except ark_recovery.ArchiveLimitError:
                result = response(request_id, action, code="archive_limit")
            except (ark_config.ConfigError, OSError, ValueError):
                result = response(request_id, action, code="invalid_configuration")
            except Exception:
                # A failure after durable intent can have happened after side effects.
                raise ControlError("outcome_unknown") from None
            try:
                self._audit(directory, request_id, action, actor, "completed", None if result["ok"] else result["error"]["code"])
                ark_config.atomic_write(path, json.dumps({"fingerprint": fingerprint, "state": "completed", "result": result}).encode())
            except OSError:
                # The command may already have run: retain intent and do not suggest a safe retry.
                raise ControlError("outcome_unknown") from None
            return result

    def execute(self, action, params=None, *, request_id=None, confirmed=False, actor=None):
        request_id = uuid.uuid4().hex if request_id is None else request_id
        params = {} if params is None else params
        safe_id = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) else None
        safe_action = action if isinstance(action, str) and action in ACTIONS else None
        try:
            self.validate(action, params, request_id, confirmed)
            actor = Actor("local", str(os.getuid())) if actor is None else actor
            if not isinstance(actor, Actor):
                raise ControlError("invalid_request")
            if action in READ_ACTIONS:
                return response(request_id, action, data=self._dispatch(action, params))
            return self._mutate(action, params, request_id, actor)
        except ControlError as error:
            return response(safe_id, safe_action, code=error.code)
        except ark_migration.MigrationInvalidError:
            return response(safe_id, safe_action, code='migration_invalid')
        except ark_migration.MigrationLimitError:
            return response(safe_id, safe_action, code='migration_limit')
        except ark_config.ConfigRecoveryError:
            return response(safe_id, safe_action, code='configuration_recovery_required')
        except ark_config.StaleConfigError:
            return response(safe_id, safe_action, code="stale_configuration")
        except ark_config.ConfigBusyError:
            return response(safe_id, safe_action, code="busy")
        except (ark_config.ConfigError, OSError, ValueError):
            return response(safe_id, safe_action, code="invalid_configuration")
        except Exception:
            # A pending mutation record remains on unexpected failures; never echo exceptions.
            return response(safe_id, safe_action, code="internal_error")
