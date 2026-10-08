"""Read Lua 5.4 prototype constants/debug names without decompiling control flow."""
import json
import struct

class Reader:
    def __init__(self, data): self.data, self.pos = data, 0
    def take(self, count):
        result = self.data[self.pos:self.pos+count]
        if len(result) != count: raise ValueError('Truncated chunk')
        self.pos += count
        return result
    def integer(self):
        value = 0
        for _ in range(10):
            b = self.take(1)[0]
            value = (value << 7) | (b & 127)
            if b & 128: return value
        raise ValueError('Invalid variable integer')
    def string(self):
        size = self.integer()
        return self.take(size-1).decode('utf-8', 'replace') if size else None
    def proto(self):
        source = self.string()
        first, last = self.integer(), self.integer()
        params, vararg, stack = self.take(3)
        count = self.integer()
        self.take(count * 4)
        constants = []
        for _ in range(self.integer()):
            tag = self.take(1)[0]
            if tag == 0: value = None
            elif tag == 1: value = False
            elif tag == 17: value = True
            elif tag == 3: value = struct.unpack('<q', self.take(8))[0]
            elif tag == 19: value = struct.unpack('<d', self.take(8))[0]
            elif tag in (4,20): value = self.string()
            else: raise ValueError(f'Unsupported constant tag {tag}')
            constants.append(value)
        self.take(self.integer()*3)
        children = [self.proto() for _ in range(self.integer())]
        self.take(self.integer())
        for _ in range(self.integer()): self.integer(); self.integer()
        locals_ = []
        for _ in range(self.integer()):
            locals_.append(self.string()); self.integer(); self.integer()
        upvalues = [self.string() for _ in range(self.integer())]
        return {'source':source,'first_line':first,'last_line':last,'parameters':params,'instruction_count':count,'constants':constants,'locals':locals_,'upvalue_names':upvalues,'children':children}

def inspect(data):
    r = Reader(data)
    if r.take(12) != b'\x1bLua\x54\x00\x19\x93\r\n\x1a\n': raise ValueError('Not standard Lua 5.4')
    if r.take(3) != bytes([4,8,8]): raise ValueError('Unsupported sizes')
    if struct.unpack('<q',r.take(8))[0] != 0x5678: raise ValueError('Wrong integer encoding')
    if struct.unpack('<d',r.take(8))[0] != 370.5: raise ValueError('Wrong number encoding')
    r.take(1)
    result = r.proto()
    if r.pos != len(data): raise ValueError(f'Unparsed trailing bytes: {len(data)-r.pos}')
    return result
