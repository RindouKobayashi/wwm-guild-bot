"""Bounded legacy zlib stream framing after transport decryption; no sockets."""
import struct
import zlib


class LegacyFrameDecoder:
    def __init__(self, max_wire=1024 * 1024, max_plain=8 * 1024 * 1024,
                 max_frame=1024 * 1024):
        self.inflate = zlib.decompressobj()
        self.buffer = bytearray()
        self.wire_bytes = self.plain_bytes = 0
        self.max_wire, self.max_plain, self.max_frame = max_wire, max_plain, max_frame

    def feed(self, data):
        self.wire_bytes += len(data)
        if self.wire_bytes > self.max_wire:
            raise ValueError('Legacy compressed input budget exceeded')
        plain = self.inflate.decompress(data, self.max_plain - self.plain_bytes + 1)
        self.plain_bytes += len(plain)
        if self.plain_bytes > self.max_plain or self.inflate.unconsumed_tail:
            raise ValueError('Legacy inflated output budget exceeded')
        if self.inflate.unused_data:
            raise ValueError('Unexpected trailing legacy compressed stream')
        self.buffer.extend(plain)

    def pop_frame(self):
        if len(self.buffer) < 4:
            return None
        size = struct.unpack_from('<I', self.buffer)[0]
        if not 2 <= size <= self.max_frame:
            raise ValueError('Legacy envelope size outside budget')
        if len(self.buffer) < size + 4:
            return None
        method = struct.unpack_from('<H', self.buffer, 4)[0]
        body = bytes(self.buffer[6:size + 4])
        del self.buffer[:size + 4]
        return method, body
