"""Bounded Source-RCON client for ASA, which answers no A2S queries.

Only the controller uses it, on the container-internal port, with the admin
password read from the private INI. Replies and passwords are never logged.
"""
import re
import socket
import struct

from . import ark_config as c, ark_game as game
from .query import QueryError

LIMIT = 64 * 1024
AUTH, COMMAND, AUTH_RESPONSE = 3, 2, 2


def admin_password(root=c.ROOT):
    parsed = c.parse_ini(c.decode_ini((root / c.INI_PATH).read_bytes())[0])
    value = c._ini_value(parsed, 'ServerSettings', 'ServerAdminPassword', '')
    if not value:
        raise QueryError('RCON credentials are unavailable.')
    return value


def _packet(request, kind, body):
    data = struct.pack('<ii', request, kind) + body.encode('utf-8') + b'\0\0'
    return struct.pack('<i', len(data)) + data


def _exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise QueryError('RCON connection closed.')
        data += chunk
    return data


def _receive(sock):
    size = struct.unpack('<i', _exact(sock, 4))[0]
    if not 10 <= size <= LIMIT:
        raise QueryError('Unsupported RCON reply.')
    data = _exact(sock, size)
    request, kind = struct.unpack('<ii', data[:8])
    return request, kind, data[8:-2].decode('utf-8', errors='replace')


def command(text, password, host='127.0.0.1', port=game.RCON_PORT, timeout=10):
    if not text.isprintable() or len(text) > 256:
        raise QueryError('Unsupported RCON command.')
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(_packet(1, AUTH, password))
        while True:
            request, kind, _ = _receive(sock)
            if kind == AUTH_RESPONSE:
                break
        if request == -1:
            raise QueryError('RCON authentication failed.')
        sock.sendall(_packet(2, COMMAND, text))
        request, _, body = _receive(sock)
        if request != 2:
            raise QueryError('Unexpected RCON reply.')
        return body.strip()


def parse_players(text):
    """ListPlayers lines are '<n>. <name>, <id>'; anything else is unknown, never zero."""
    if text.strip() == 'No Players Connected':
        return []
    names = []
    for line in filter(None, (line.strip() for line in text.splitlines())):
        match = re.fullmatch(r'\d+\.\s*(.*?),\s*\S+', line)
        if match is None:
            raise QueryError('Unrecognized ListPlayers reply.')
        names.append(match.group(1))
    if not names:
        raise QueryError('Empty ListPlayers reply.')
    return names


def players(root=c.ROOT, timeout=5):
    return parse_players(command('ListPlayers', admin_password(root), timeout=timeout))


def probe(host='127.0.0.1', port=None, timeout=2, root=c.ROOT):
    """Same shape as query.query_server; map/capacity are what we launched."""
    settings = c.load_settings(root)
    names = players(root, timeout=timeout)
    return {'server': settings['session_name'], 'map': settings['map'], 'game': 'ARK: Survival Ascended',
            'players': len(names), 'max_players': settings['max_players'], 'password_required': True, 'VAC': False}


def roster(host='127.0.0.1', port=None, timeout=2, root=c.ROOT):
    # ASA reports no connection time, so no entry ever qualifies as a stale slot.
    return [(name, 0.0) for name in players(root, timeout=timeout)]
