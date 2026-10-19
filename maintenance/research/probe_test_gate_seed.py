"""One bounded seed-only TCP probe of the captured test server. No account login.

No Steam/SDK calls, tokens, character IDs, RAM access or encrypted replay.
"""
import json
from pathlib import Path
import socket
import struct
from extract_native_protocol_schema import fields
from probe_test_steam_sdk import ROOT, validate_profile


def parse_seed(frame):
    if not 6 <= len(frame) <= 128:
        raise ValueError('Seed envelope size')
    size, method = struct.unpack_from('<IH', frame)
    if size != len(frame) - 4 or method != 0:
        raise ValueError('Unexpected seed envelope')
    rows, used = fields(frame[6:])
    numbers = [n for n, w, v in rows]
    if (used != len(frame) - 6 or len(numbers) != len(set(numbers))
            or 1 not in numbers or not set(numbers) <= {1, 2, 3}
            or any(w != 0 for n, w, v in rows)):
        raise ValueError('Unexpected SessionSeed fields')
    values = dict((n, v) for n, w, v in rows)
    if values.get(2, 0) != 0 or values.get(3, 0) not in (0, 1):
        raise ValueError('Unsupported transport negotiation')
    return {'seed_present': True, 'crypto_code': 0,
            'zstd_enabled': bool(values.get(3, 0)), 'reply_bytes': len(frame)}


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError('Connection closed before complete seed')
        data.extend(chunk)
    return bytes(data)


def resolve_test_peer():
    profile = json.loads((ROOT / 'test-account-profile.private.json').read_text())
    validate_profile(profile, Path(profile['capture_path']).read_bytes())
    routing = json.loads((ROOT / 'session-routing.redacted.json').read_text())
    if routing['capture_sha256'] != profile['capture_sha256']:
        raise ValueError('Routing capture identity mismatch')
    peers = {r['peer_host'] for r in routing['transports'] if r['first_kcp_push']}
    if len(peers) != 1:
        raise ValueError('Exactly one test-session game server required')
    host = peers.pop()
    candidates = []
    for line in (ROOT / 'public-routing/server_list_prod_a.txt').read_text(encoding='utf-8').splitlines():
        for part in line.split():
            if part.startswith(host + ':'):
                candidates.append(int(part.split(':')[1]))
    if not candidates or set(candidates) != {4000}:
        raise ValueError('Captured peer not uniquely confirmed in public server list')
    return host, 4000, profile


def main():
    host, port, profile = resolve_test_peer()
    # Consume before connecting. Never retry automatically.
    with (ROOT / 'seed-probe-attempt.private.json').open('x') as handle:
        json.dump({'status': 'attempt_consumed', 'capture_sha256': profile['capture_sha256']}, handle)
    report = {'status': 'seed_probe_started', 'connections_attempted': 1,
              'account_credentials_sent': False, 'character_login_sent': False,
              'steam_api_called': False, 'game_memory_access': False,
              'seed_value_saved': False, 'transport': 'TCP'}
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(struct.pack('<IH', 2, 0))
            head = read_exact(sock, 4)
            size = struct.unpack('<I', head)[0]
            if not 2 <= size <= 124:
                raise ValueError('Seed reply length out of bounds')
            report.update(parse_seed(head + read_exact(sock, size)))
            report['status'] = 'test_gate_tcp_seed_received'
    except (OSError, ValueError) as exc:
        report['status'] = 'seed_probe_not_confirmed'
        report['error_type'] = type(exc).__name__
    (ROOT / 'seed-probe.redacted.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
