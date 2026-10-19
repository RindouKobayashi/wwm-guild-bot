"""Bounded fresh connection used only by the token bootstrap. No auto-start."""
import hashlib
import json
import socket
import struct
import time
import zlib
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from extract_native_protocol_schema import fields
from lua54_activity_constants import inspect
from gate_handshake_codec import envelope, build_fresh_session, validate_session_key_ok
from gate_legacy_stream import LegacyFrameDecoder
from probe_test_gate_seed import ROOT, resolve_test_peer, read_exact, parse_seed
from probe_test_gate_connect import new_connection, avatar_reconnection


class TestGateSession:
    def __init__(self, logical_hostnum=None):
        if logical_hostnum is not None and (type(logical_hostnum) is not int or not 1 <= logical_hostnum <= 0xffffffff):
            raise ValueError('Invalid logical host number')
        self.logical_hostnum = logical_hostnum
        self.redirect_target = None
        self.sock = None
        self.state = None
        self.decoder = LegacyFrameDecoder(max_wire=8 * 1024 * 1024,
            max_plain=32 * 1024 * 1024, max_frame=8 * 1024 * 1024)
        self.compress = zlib.compressobj(wbits=12)
        self.deadline = time.monotonic() + 45
        self.frames = 0

    def __enter__(self):
        try:
            host, port, _ = resolve_test_peer()
            redirect = self.redirect_target
            if redirect is not None:
                host, port = redirect[:2]
            self.peer_host = host
            nums = {int(c[8]) for line in (ROOT / 'public-routing/server_list_prod_a.txt').read_text(encoding='utf-8').splitlines()
                    if len(c := line.split()) == 11 and c[7].startswith(host + ':')}
            if len(nums) != 1:
                raise ValueError('Ambiguous test host number')
            self.peer_hostnum = nums.pop()
            self.hostnum = self.peer_hostnum if self.logical_hostnum is None else self.logical_hostnum
            if self.logical_hostnum is not None:
                public_nums = {int(c[8]) for line in (ROOT / 'public-routing/server_list_prod_a.txt').read_text(encoding='utf-8').splitlines()
                               if len(c := line.split()) == 11}
                if self.logical_hostnum not in public_nums:
                    raise ValueError('Logical server absent from verified public list')
            raw = (ROOT / 'current-net-config/F92CE50E67696622.luac').read_bytes()
            if hashlib.sha256(raw).hexdigest() != '40bb6a09bf10843371a9b1d555a0328c77c55fb4c6829b3575fad1941c328b3d':
                raise ValueError('Public-key source identity mismatch')
            keys = [load_pem_public_key(c.encode()) for c in inspect(raw)['constants']
                    if isinstance(c, str) and c.startswith('-----BEGIN PUBLIC KEY-----')]
            if len(keys) != 1:
                raise ValueError('Ambiguous public key')
            self.sock = socket.create_connection((host, port), timeout=5)
            self.sock.settimeout(5)
            self.sock.sendall(envelope(0))
            head = read_exact(self.sock, 4)
            size = struct.unpack('<I', head)[0]
            if not 2 <= size <= 124:
                raise ValueError('Seed size outside budget')
            frame = head + read_exact(self.sock, size)
            parse_seed(frame)
            rows, _ = fields(frame[6:])
            seed = next(v for n, w, v in rows if n == 1)
            seed = seed - (1 << 64) if seed >= 1 << 63 else seed
            self.state = build_fresh_session(keys[0], seed, enable_zstd=False)
            self.sock.sendall(self.state['session_key_request'])
            validate_session_key_ok(self.state['receive_cipher'].apply(read_exact(self.sock, 6)))
            request = new_connection(self.hostnum) if redirect is None else avatar_reconnection(*redirect[2:])
            self.sock.sendall(self.state['send_cipher'].apply(request))
            method, body = self.receive()
            rows, used = fields(body)
            expected_reply = 1 if redirect is None else 2
            if method != 2 or used != len(body) or [v for n, w, v in rows if (n, w) == (2, 0)] != [expected_reply]:
                raise ValueError('Fresh connection not accepted')
            self.redirect_target = None
            return self
        except BaseException:
            self.close()
            raise

    def receive(self):
        while time.monotonic() < self.deadline:
            if self.frames >= 512:
                raise ValueError('Initial-session frame budget reached')
            frame = self.decoder.pop_frame()
            if frame is not None:
                self.frames += 1
                return frame
            self.sock.settimeout(max(0.1, min(5, self.deadline - time.monotonic())))
            try:
                data = self.sock.recv(min(16384, self.decoder.max_wire - self.decoder.wire_bytes + 1))
            except socket.timeout:
                # An idle interval is not the overall bounded login deadline.
                continue
            if not data:
                raise ValueError('Connection closed during token bootstrap')
            self.decoder.feed(self.state['receive_cipher'].apply(data))
        raise TimeoutError('Token bootstrap connection deadline reached')

    def send(self, frame):
        if not self.state or time.monotonic() >= self.deadline:
            raise ValueError('No active bounded session')
        data = self.compress.compress(frame) + self.compress.flush(zlib.Z_SYNC_FLUSH)
        self.sock.sendall(self.state['send_cipher'].apply(data))

    def reconnect_avatar(self, args, expected_entity):
        if not isinstance(args, list) or len(args) != 5:
            raise ValueError('Unexpected native avatar relay shape')
        client_id, host, port, entity_id, key = args
        entity_bytes = entity_id.encode('ascii') if isinstance(entity_id, str) else entity_id
        if entity_bytes != expected_entity.encode('ascii'):
            raise ValueError('Relay character differs from verified test character')
        public = {c[7].split(':')[0] for line in (ROOT / 'public-routing/server_list_prod_a.txt').read_text(encoding='utf-8').splitlines()
                  if len(c := line.split()) == 11}
        if not isinstance(host, str) or host not in public or type(port) is not int or port not in (4000, 4100):
            raise ValueError('Relay endpoint outside verified public game peers')
        avatar_reconnection(client_id, entity_id, key)  # Validate before closing the old connection.
        self.close()
        self.decoder = LegacyFrameDecoder(max_wire=8 * 1024 * 1024, max_plain=32 * 1024 * 1024, max_frame=8 * 1024 * 1024)
        self.compress = zlib.compressobj(wbits=12)
        self.redirect_target = (host, port, client_id, entity_id, key)
        self.__enter__()

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        self.state = None
        self.redirect_target = None
        self.decoder.buffer.clear()

    def __exit__(self, *args):
        self.close()
