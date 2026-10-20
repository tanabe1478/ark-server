#!/usr/bin/env python3
"""Read-only A2S_INFO probe, also importable by the local control layer."""
import json
import socket
import sys

if __package__:
    from .ark_config import load_settings
else:
    from ark_config import load_settings


class QueryError(ValueError):
    """Unsupported or malformed A2S reply (never includes raw network data)."""


def parse_info(data):
    if data[:5] != b'\xff\xff\xff\xffI':
        raise QueryError('Unsupported A2S_INFO reply.')
    pos = 6  # Header, type and protocol byte.
    fields = []
    try:
        for _ in range(4):
            end = data.index(0, pos)
            fields.append(data[pos:end].decode('utf-8', errors='replace'))
            pos = end + 1
        pos += 2  # App ID.
        players, maximum, _ = data[pos:pos + 3]
        pos += 5  # Counts, server type and operating system.
        password, vac = data[pos:pos + 2]
    except (ValueError, IndexError):
        raise QueryError('Malformed A2S_INFO reply.') from None
    return dict(server=fields[0], map=fields[1], game=fields[3], players=players,
                max_players=maximum, password_required=bool(password), VAC=bool(vac))


def query_server(host='127.0.0.1', port=None, timeout=2):
    port = load_settings()['query_port'] if port is None else port
    query = b'\xff\xff\xff\xffTSource Engine Query\x00'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.send(query)
        data = sock.recv(65535)
        if data[:5] == b'\xff\xff\xff\xffA':
            if len(data) < 9:
                raise QueryError('Malformed A2S challenge.')
            sock.send(query + data[5:9])
            data = sock.recv(65535)
    return parse_info(data)


def main():
    try:
        host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
        port = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(json.dumps(query_server(host, port, timeout=5), ensure_ascii=False, indent=2))
    except (OSError, ValueError):
        print('Server query unavailable; check startup state, settings and UDP connectivity.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
