import struct
import base64
import unittest
from probe_test_gate_connect import new_connection, read_cipher_frame
from extract_native_protocol_schema import fields
from transport_rc4_state import RC4State


class ConnectCodecTests(unittest.TestCase):
    def test_new_connection_never_carries_reconnect_or_recorded_client_id(self):
        first, second = new_connection(12401), new_connection(12401)
        rows, used = fields(first[6:])
        values = {n: v for n, w, v in rows}
        self.assertEqual(used, len(first) - 6)
        self.assertEqual(values[2], 0)
        self.assertEqual(values[7], 12401)
        self.assertEqual(len(values[3]), 16)
        self.assertEqual(len(base64.b64decode(values[3], validate=True)), 12)
        self.assertFalse({1, 4, 5, 6} & set(values))
        self.assertNotEqual(first, second)

    def test_split_socket_reads_keep_cipher_position(self):
        raw = struct.pack('<IH', 4, 2) + b'\x10\x01'
        encrypted = RC4State.from_key(b'synthetic').apply(raw)
        class Socket:
            def __init__(self):
                self.parts = [encrypted[i:i+1] for i in range(len(encrypted))]
            def recv(self, size):
                return self.parts.pop(0) if self.parts else b''
        self.assertEqual(read_cipher_frame(Socket(), RC4State.from_key(b'synthetic')), (2, b'\x10\x01'))

    def test_invalid_host_number_rejected(self):
        for invalid in (True, 0, -1, 1 << 32, '12401'):
            with self.assertRaises(ValueError):
                new_connection(invalid)
