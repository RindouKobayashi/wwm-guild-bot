import struct
import unittest
import zlib
from gate_legacy_stream import LegacyFrameDecoder


def compress(data):
    c = zlib.compressobj(wbits=12)
    return c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)


class LegacyStreamTests(unittest.TestCase):
    def test_native_failure_prefix_is_zlib_not_envelope(self):
        decoder = LegacyFrameDecoder()
        decoder.feed(bytes.fromhex('4889d261'))
        self.assertEqual(decoder.plain_bytes, 1)
        self.assertIsNone(decoder.pop_frame())

    def test_byte_fragments_and_multiple_frames_preserve_stream(self):
        first = struct.pack('<IH', 4, 2) + b'\x10\x01'
        second = struct.pack('<IH', 2, 10)
        decoder = LegacyFrameDecoder()
        frames = []
        for value in compress(first + second):
            decoder.feed(bytes([value]))
            while (frame := decoder.pop_frame()) is not None:
                frames.append(frame)
        self.assertEqual(frames, [(2, b'\x10\x01'), (10, b'')])

    def test_inflation_bomb_and_bad_lengths_rejected(self):
        with self.assertRaises(ValueError):
            LegacyFrameDecoder(max_plain=64).feed(compress(b'x' * 10000))
        decoder = LegacyFrameDecoder(max_frame=64)
        decoder.feed(compress(struct.pack('<I', 1000)))
        with self.assertRaises(ValueError):
            decoder.pop_frame()

    def test_wire_budget_and_trailing_stream_rejected(self):
        with self.assertRaises(ValueError):
            LegacyFrameDecoder(max_wire=2).feed(b'123')
        with self.assertRaises(ValueError):
            LegacyFrameDecoder().feed(zlib.compress(b'') + b'extra')
