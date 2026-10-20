"""Host-side unit entrypoint. Consumes one durable start permit, no restart loop."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import sys
import time

from . import ark_shutdown as native
from .ark_docker import (Engine, STATE, load, validate_instance, record_fields,
                         verify_files, create_args, contract_id)


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET', '')
    if not address:
        raise RuntimeError('A notify systemd unit is required.')
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode())


def lease(root, invocation):
    native.save(root / STATE / 'lease.json', {'invocation_id':invocation,
                'boot_id':native.boot_id(), 'monotonic':time.monotonic()})


def main():
    os.umask(0o077)
    root = Path(sys.argv[1]).resolve()
    config = load(root)
    engine = Engine(); engine.trusted()
    invocation = os.environ.get('INVOCATION_ID', '')
    if not re.fullmatch('[0-9a-f]{32}', invocation):
        return 78
    permit_path = root / STATE / 'start-permit.json'
    permit = native.private_json(permit_path, 8*1024*1024)
    if (permit.get('schema') != 1 or permit.get('phase') != 'issued'
            or permit.get('runtime') != config or permit.get('boot_id') != native.boot_id()
            or permit.get('configuration') != native.configuration(root)):
        return 78
    verify_files(root, permit['files'])
    image = json.loads(engine.call('image', 'inspect', config['image']))[0]
    if image['Config'].get('Labels', {}).get('dev.ark.source-contract') != contract_id(config['source_contract']):
        return 78
    pending = []
    def requested(number, frame):
        pending.append(number)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, requested)
    # Consumption precedes any create/remove/start. Interrupted starts are not replayed.
    native.save(permit_path, permit | {'phase':'consumed', 'invocation_id':invocation})
    native.save(root / STATE / 'launch.json', {'phase':'starting', 'invocation_id':invocation,
                                             'permit_nonce':permit['nonce']})
    old = engine.inspect(config['container_name'])
    if old is not None:
        validate_instance(root, config, old)
        if old['State']['Running'] or old['State']['Status'] != 'exited':
            return 78
        native.save(root / STATE / 'instances' / (old['Id'] + '.json'),
                    {'record':record_fields(old), 'labels':old['Config']['Labels']})
        engine.call('rm', old['Id'])  # Never force removal; preserved receipt/metadata remain.
    container = engine.call(*create_args(root, config, invocation)).strip()
    if not re.fullmatch('[0-9a-f]{64}', container):
        return 70
    native.save(root / STATE / 'launch.json', {'phase':'created', 'invocation_id':invocation,
                                             'container_id':container, 'permit_nonce':permit['nonce']})
    lease(root, invocation)
    engine.call('start', container)
    ready = False
    forwarded = False
    deadline = time.monotonic() + 60
    while True:
        lease(root, invocation)
        inspected = engine.inspect(config['container_name'])
        if inspected is None or inspected['Id'] != container:
            return 70
        validate_instance(root, config, inspected)
        if not inspected['State']['Running']:
            native.save(root / STATE / 'launch.json', {'phase':'exited', 'invocation_id':invocation,
                'container_id':container, 'permit_nonce':permit['nonce'], 'record':record_fields(inspected)})
            value = inspected['State']['ExitCode']
            return value if type(value) is int and 0 <= value <= 255 else 70
        if pending and not forwarded:
            forwarded = True
            # One Docker stop request. Runtime timeout/SIGKILL is NOT qualified.
            engine.call('stop', '--timeout=150', container, timeout=170)
            continue
        if not ready:
            try:
                checked = json.loads(engine.call('exec', container, '/usr/bin/python3', '-m',
                             'scripts.ark_container_supervisor', 'process', timeout=10))
                if checked.get('ok') is True:
                    native.save(root / STATE / 'launch.json', {'phase':'running', 'invocation_id':invocation,
                        'container_id':container, 'permit_nonce':permit['nonce']})
                    notify('READY=1')
                    ready = True
            except Exception:
                if time.monotonic() >= deadline:
                    # Lease is no longer renewed; container supervisor will stop the
                    # game as an unqualified failure. Do not retry a game operation.
                    return 70
        time.sleep(1)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('Docker game transport failed; operation was not replayed. Inspect private evidence.', file=sys.stderr)
        sys.exit(70)
