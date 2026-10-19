"""Fresh unauthenticated ConnectServer probe; no SDK or character login."""
import hashlib
import base64
import json
import os
import secrets
import socket
import struct
import time
from gate_legacy_stream import LegacyFrameDecoder
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from extract_native_protocol_schema import fields
from lua54_activity_constants import inspect
from gate_handshake_codec import build_fresh_session, envelope, varint, byte_field, validate_session_key_ok
from probe_test_gate_seed import ROOT, resolve_test_peer, read_exact, parse_seed


def new_connection(hostnum):
    if type(hostnum) is not int or not 1 <= hostnum <= 0xffffffff:
        raise ValueError('Invalid public host number')
    # Native sender sets opt only when optional capabilities are enabled.
    # This probe advertises none: no reconnect, compression or indexed names.
    # IdManager.genid(): Base64 BSON ObjectID. Use a fresh timestamp and
    # random remaining bytes; do not reuse captured client/character IDs.
    client_id = base64.b64encode(struct.pack('>I', int(time.time())) + secrets.token_bytes(8))
    return envelope(2, b'\x10\x00' + byte_field(3, client_id)
                    + b'\x38' + varint(hostnum))


def avatar_reconnection(client_id, entity_id, reconnect_key):
    def binary(value):
        return value.encode('utf-8') if isinstance(value, str) else value
    client_id, entity_id, reconnect_key = map(binary, (client_id, entity_id, reconnect_key))
    if (not isinstance(client_id, bytes) or len(client_id) != 16
            or not isinstance(entity_id, bytes) or len(entity_id) != 16
            or not isinstance(reconnect_key, bytes) or not 1 <= len(reconnect_key) <= 4096):
        raise ValueError('Invalid server-issued reconnect context')
    return envelope(2, b'\x10\x01' + byte_field(3, client_id)
                    + byte_field(4, entity_id) + byte_field(5, reconnect_key))


def read_cipher_frame(sock, cipher, report=None):
    header = cipher.apply(read_exact(sock, 4))
    size = struct.unpack('<I', header)[0]
    if report is not None:
        report['reply_compressed'] = bool(size & 0x80000000)
        report['reply_wire_bytes'] = size & 0x7fffffff
    if not 2 <= size <= 1024 * 1024:
        raise ValueError('Unsupported compressed or oversized response')
    plain = cipher.apply(read_exact(sock, size))
    return struct.unpack_from('<H', plain)[0], plain[2:]


def read_legacy_frame(sock, cipher, report):
    decoder = LegacyFrameDecoder()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame = decoder.pop_frame()
        if frame is not None:
            report['legacy_zlib_decoded'] = True
            report['compressed_bytes_read'] = decoder.wire_bytes
            report['inflated_bytes_read'] = decoder.plain_bytes
            return frame
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        data = sock.recv(min(4096, decoder.max_wire - decoder.wire_bytes + 1))
        if not data:
            raise ValueError('Connection closed before legacy frame')
        decoder.feed(cipher.apply(data))
    raise TimeoutError('Legacy frame read deadline reached')


def main():
    host, port, profile = resolve_test_peer()
    hostnums = set()
    for line in (ROOT / 'public-routing/server_list_prod_a.txt').read_text(encoding='utf-8').splitlines():
        columns = line.split()
        if len(columns) == 11 and columns[7].startswith(host + ':'):
            hostnums.add(int(columns[8]))
    if len(hostnums) != 1:
        raise ValueError('Public routing host number is ambiguous')
    raw = (ROOT / 'current-net-config/F92CE50E67696622.luac').read_bytes()
    if hashlib.sha256(raw).hexdigest() != '40bb6a09bf10843371a9b1d555a0328c77c55fb4c6829b3575fad1941c328b3d':
        raise ValueError('Current public-key configuration identity mismatch')
    keys = [load_pem_public_key(c.encode()) for c in inspect(raw)['constants']
            if isinstance(c, str) and c.startswith('-----BEGIN PUBLIC KEY-----')]
    if len(keys) != 1:
        raise ValueError('Expected one verified public key')
    legacy = os.environ.get('WWM_GATE_CONNECT_LEGACY') == '1'
    suffix = ('-legacy-zlib' if legacy else
              '-base64-clientid' if os.environ.get('WWM_GATE_CONNECT_CORRECTED') == '1' else '')
    with (ROOT / ('connect-probe' + suffix + '-attempt.private.json')).open('x') as handle:
        json.dump({'status': 'attempt_consumed', 'capture_sha256': profile['capture_sha256']}, handle)
    report = {'status': 'connect_probe_started', 'connections_attempted': 1,
              'account_credentials_sent': False, 'character_login_sent': False,
              'recorded_identifiers_used': False, 'game_memory_access': False,
              'session_values_saved': False, 'connect_server_sent': False, 'stage': 'seed'}
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(envelope(0))
            head = read_exact(sock, 4)
            size = struct.unpack('<I', head)[0]
            if not 2 <= size <= 124:
                raise ValueError('Seed size outside budget')
            frame = head + read_exact(sock, size)
            parse_seed(frame)
            rows, _ = fields(frame[6:])
            seed = next(v for n, w, v in rows if n == 1)
            seed = seed - (1 << 64) if seed >= 1 << 63 else seed
            state = build_fresh_session(keys[0], seed, enable_zstd=False)
            report['stage'] = 'key_ack'
            sock.sendall(state['session_key_request'])
            validate_session_key_ok(state['receive_cipher'].apply(read_exact(sock, 6)))
            report['stage'] = 'connect_reply'
            sock.sendall(state['send_cipher'].apply(new_connection(hostnums.pop())))
            report['connect_server_sent'] = True
            method, body = (read_legacy_frame(sock, state['receive_cipher'], report) if legacy
                            else read_cipher_frame(sock, state['receive_cipher'], report))
            report['reply_method'] = method
            if method != 2:
                raise ValueError('Expected ConnectServerReply')
            rows, used = fields(body)
            replies = [v for n, w, v in rows if (n, w) == (2, 0)]
            if used != len(body) or len(replies) != 1 or replies[0] not in range(7):
                raise ValueError('Invalid ConnectServerReply')
            report['reply_code'] = replies[0]
            report['status'] = 'fresh_connect_server_accepted' if replies[0] == 1 else 'connect_server_declined'
            del state
    except (OSError, ValueError) as exc:
        report['status'] = 'connect_probe_not_confirmed'
        report['error_type'] = type(exc).__name__
        if isinstance(exc, ValueError) and str(exc) in {
                'Expected ConnectServerReply', 'Invalid ConnectServerReply',
                'Unsupported compressed or oversized response',
                'Connection closed before complete seed'}:
            report['error_reason'] = str(exc)
    (ROOT / ('connect-probe' + suffix + '.redacted.json')).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


class Run:
    def running(self):
        from mitmproxy import ctx
        try:
            main()
        finally:
            ctx.master.shutdown()


addons = [Run()]
