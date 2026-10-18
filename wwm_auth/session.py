"""Bounded native connection using an explicit exported account context."""
import socket
import struct
import time
import zlib
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from .protocol import (LegacyFrameDecoder, envelope, fields, build_fresh_session,
                       validate_session_key_ok, new_connection, avatar_reconnection)


def read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise ValueError('Game connection closed')
        result.extend(part)
    return bytes(result)


class Session:
    def __init__(self, context):
        self.context = context
        self.sock = None
        self.state = None
        self.redirect_target = None
        self.deadline = time.monotonic() + 45
        self.frames = 0
        self.reset_stream()

    def reset_stream(self):
        self.decoder = LegacyFrameDecoder(max_wire=8 * 1024 * 1024,
            max_plain=32 * 1024 * 1024, max_frame=8 * 1024 * 1024)
        self.compress = zlib.compressobj(wbits=12)

    def __enter__(self):
        try:
            redirect = self.redirect_target
            host, port = redirect[:2] if redirect else (self.context['peer_host'], self.context['peer_port'])
            public = load_pem_public_key(self.context['public_key_pem'].encode('ascii'))
            self.sock = socket.create_connection((host, port), timeout=5)
            self.sock.settimeout(5)
            self.sock.sendall(envelope(0))
            header = read_exact(self.sock, 4)
            size = struct.unpack('<I', header)[0]
            if not 2 <= size <= 124:
                raise ValueError('Invalid seed size')
            frame = read_exact(self.sock, size)
            if frame[:2] != b'\x00\x00':
                raise ValueError('Invalid seed method')
            rows, _ = fields(frame[2:])
            if (len({n for n, _, _ in rows}) != len(rows) or
                    not {n for n, _, _ in rows} <= {1, 2, 3} or
                    any(w != 0 for _, w, _ in rows)):
                raise ValueError('Invalid seed fields')
            values = {n: v for n, _, v in rows}
            if 1 not in values or values.get(2, 0) != 0 or values.get(3, 0) not in (0, 1):
                raise ValueError('Unsupported transport')
            seed = values[1]
            seed = seed - (1 << 64) if seed >= 1 << 63 else seed
            self.state = build_fresh_session(public, seed, enable_zstd=False)
            self.sock.sendall(self.state['session_key_request'])
            validate_session_key_ok(self.state['receive_cipher'].apply(read_exact(self.sock, 6)))
            request = avatar_reconnection(*redirect[2:]) if redirect else new_connection(self.context['logical_hostnum'])
            self.sock.sendall(self.state['send_cipher'].apply(request))
            method, body = self.receive()
            rows, _ = fields(body)
            if method != 2 or [v for n, w, v in rows if (n, w) == (2, 0)] != [2 if redirect else 1]:
                raise ValueError('Game connection rejected')
            self.redirect_target = None
            return self
        except BaseException:
            self.close()
            raise

    def receive(self):
        while time.monotonic() < self.deadline:
            if self.frames >= 512:
                raise ValueError('Session frame budget reached')
            frame = self.decoder.pop_frame()
            if frame is not None:
                self.frames += 1
                return frame
            self.sock.settimeout(max(0.1, min(5, self.deadline - time.monotonic())))
            try:
                data = self.sock.recv(min(16384, self.decoder.max_wire - self.decoder.wire_bytes + 1))
            except socket.timeout:
                continue
            if not data:
                raise ValueError('Game connection closed')
            self.decoder.feed(self.state['receive_cipher'].apply(data))
        raise TimeoutError('Saved login session time limit reached')

    def send(self, frame):
        if self.state is None or time.monotonic() >= self.deadline:
            raise ValueError('No active bounded session')
        data = self.compress.compress(frame) + self.compress.flush(zlib.Z_SYNC_FLUSH)
        self.sock.sendall(self.state['send_cipher'].apply(data))

    def reconnect_avatar(self, args):
        if not isinstance(args, list) or len(args) != 5:
            raise ValueError('Invalid relay shape')
        client, host, port, entity, key = args
        expected = self.context['character_id'].encode('ascii')
        if (entity.encode('ascii') if isinstance(entity, str) else entity) != expected:
            raise ValueError('Relay character mismatch')
        if host not in self.context['allowed_peers'] or type(port) is not int or port not in (4000, 4100):
            raise ValueError('Relay outside verified game peers')
        avatar_reconnection(client, entity, key)
        self.close()
        self.reset_stream()
        self.redirect_target = (host, port, client, entity, key)
        self.__enter__()

    def close(self):
        if self.sock is not None:
            self.sock.close()
        self.sock = self.state = self.redirect_target = None
        self.decoder.buffer.clear()

    def __exit__(self, *args):
        self.close()
