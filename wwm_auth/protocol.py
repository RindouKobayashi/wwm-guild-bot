"""Bounded native login protocol. No game files, Steam, capture reader or process access.

Derived from the verified October 2026 protocol implementation. Legacy transport
algorithms match the existing server; they are not used to protect stored secrets.
"""
import base64
import hashlib
import re
import secrets
import struct
import time
import zlib
import msgpack
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TOKEN = re.compile(rb"[0-9]{9,12}:[a-fA-F0-9]{32}")


def read_varint(data, position):
    result=0
    for shift in range(0,70,7):
        if position>=len(data):raise ValueError('Truncated varint')
        value=data[position];position+=1;result|=(value&127)<<shift
        if value<128:return result,position
    raise ValueError('Oversized varint')

def fields(data, padding=False):
    position=0;result=[]
    while position<len(data):
        if padding and data[position]==0:break
        key,position=read_varint(data,position);number=key>>3;wire=key&7
        if not number:raise ValueError('Zero field number')
        if wire==0:value,position=read_varint(data,position)
        elif wire==2:
            length,position=read_varint(data,position)
            if length>1048576 or position+length>len(data):raise ValueError('Length bounds')
            value=data[position:position+length];position+=length
        elif wire in (1,5):
            length=8 if wire==1 else 4
            value=data[position:position+length];position+=length
            if len(value)!=length:raise ValueError('Truncated fixed field')
        else:raise ValueError('Unsupported descriptor wire type')
        result.append((number,wire,value))
    return result,position

class RC4State:

    def __init__(self, x, y, permutation):
        if not 0 <= x <= 255 or not 0 <= y <= 255:
            raise ValueError('RC4 index bounds')
        if len(permutation) != 256 or sorted(permutation) != list(range(256)):
            raise ValueError('RC4 permutation bounds/uniqueness')
        self.x = x
        self.y = y
        self.s = list(permutation)

    @classmethod
    def from_key(cls, key):
        if not 1 <= len(key) <= 256:
            raise ValueError('RC4 key size bounds')
        s = list(range(256))
        j = 0
        for i in range(256):
            j = j + s[i] + key[i % len(key)] & 255
            s[i], s[j] = (s[j], s[i])
        return cls(0, 0, s)

    def apply(self, data):
        result = bytearray(len(data))
        s = self.s
        x = self.x
        y = self.y
        for pos, value in enumerate(data):
            x = x + 1 & 255
            y = y + s[x] & 255
            s[x], s[y] = (s[y], s[x])
            result[pos] = value ^ s[s[x] + s[y] & 255]
        self.x = x
        self.y = y
        return bytes(result)

def varint(value):
    if not isinstance(value, int) or not 0 <= value < (1 << 64):
        raise ValueError('Unsigned protobuf integer bounds')
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)

def byte_field(number, value):
    return varint(number * 8 + 2) + varint(len(value)) + value

def envelope(method, payload=b''):
    if not isinstance(method, int) or not 0 <= method <= 65535 or not isinstance(payload, bytes) or len(payload) > 8 * 1024 * 1024 - 2:
        raise ValueError('Envelope bounds/type')
    return struct.pack('<IH', len(payload) + 2, method) + payload

def validate_session_key_ok(frame):
    # Inbound gate method table: seed_reply=0, session_key_ok=1.
    # Frame ordinal zero in the decrypted stream is NOT message ID zero.
    if frame != envelope(1):
        raise ValueError('Expected encrypted SessionKeyOK')
    return True

def build_fresh_session(public_key, seed, crypto=0, enable_zstd=False):
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 2048:
        raise ValueError('Expected inspected RSA-2048 public key')
    if not isinstance(seed, int) or not -(1 << 63) <= seed < (1 << 63):
        raise ValueError('SessionSeed.seed int64 bounds')
    if crypto != 0:
        raise ValueError('Only inspected mode-zero transport supported')
    if not isinstance(enable_zstd, bool):
        raise ValueError('Expected Boolean negotiation flag')
    key = hashlib.sha1(secrets.token_bytes(32)).hexdigest().encode('ascii')
    header = secrets.token_bytes(2 + secrets.randbelow(8))
    tail = secrets.token_bytes(2 + secrets.randbelow(8))
    payload = byte_field(1, header) + byte_field(2, key)
    payload += varint(3 * 8) + varint(seed & ((1 << 64) - 1))
    payload += byte_field(4, tail)
    if enable_zstd:
        payload += varint(6 * 8) + b'\x01'
    encrypted = public_key.encrypt(payload, padding.OAEP(
        mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    # The seed request and wrapped-key envelope are plaintext. Separate cipher
    # instances start at the beginning of the fresh key for each direction.
    return {'session_key': key, 'seed_request': envelope(0),
            'session_key_request': envelope(1, byte_field(1, encrypted)),
            'send_cipher': RC4State.from_key(key), 'receive_cipher': RC4State.from_key(key)}

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

def rpc_index(name):
    return int.from_bytes(hashlib.md5((name + '0').encode()).digest()[-4:], 'big') & 0x7fffffff

def created_owner(body):
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Creation message budget exceeded')
    # Avatar creation can contain megabytes of properties. Skip those fields
    # instead of decoding/copying them just to establish the token owner.
    rows = []
    pos = 0
    while pos < len(body):
        tag, pos = read_varint(body, pos)
        n, w = tag >> 3, tag & 7
        if not n:
            raise ValueError('Invalid creation field')
        if w == 0:
            v, pos = read_varint(body, pos)
        elif w == 2:
            size, pos = read_varint(body, pos)
            if pos + size > len(body):
                raise ValueError('Truncated creation field')
            if n in (1, 2) and size > 64:
                raise ValueError('Creation identity string budget')
            v = body[pos:pos + size] if n in (1, 2, 3) else None
            pos += size
        elif w in (1, 5):
            pos += 8 if w == 1 else 4
            if pos > len(body):
                raise ValueError('Truncated creation fixed field')
            v = None
        else:
            raise ValueError('Unsupported creation wire type')
        if n in (1, 2, 3):
            rows.append((n, w, v))
    values = {}
    for n, w, v in rows:
        if n in (1, 2, 3):
            if n in values:
                raise ValueError('Duplicate creation identity field')
            values[n] = (w, v)
    if values.get(1) not in [(2, b'Account'), (2, b'PlayerAvatar')]:
        return None
    ident, guid = values.get(2), values.get(3)
    if not ident or ident[0] != 2 or len(ident[1]) != 16 or not guid or guid[0] != 0:
        raise ValueError('Invalid fresh entity identity')
    return {'class': values[1][1], 'id': ident[1], 'guid': guid[1]}

def account_rpc(owner, method, args):
    if method not in ('sdk_login', 'player_login', 'confirm_relay_other') or owner['class'] != b'Account':
        raise ValueError('Only Account authentication/selection calls allowed')
    if method == 'confirm_relay_other' and (args != [1] or type(args[0]) is not int):
        raise ValueError('Only the native normal-login handover confirmation is allowed')
    if len(owner['id']) != 16 or type(owner['guid']) is not int or owner['guid'] < 0:
        raise ValueError('Fresh Account identity required')
    payload = msgpack.packb(args, use_bin_type=True)
    if len(payload) > 256 * 1024:
        raise ValueError('Authentication payload exceeds budget')
    index = b'\x10' + varint(rpc_index(method) << 1)
    area = byte_field(1, owner['id']) + b'\x10' + varint(owner['guid'])
    # Native call_server uses the current entity as caller and an empty sender.
    # These are fresh connection identities, never IDs copied from a capture.
    area += byte_field(3, index) + byte_field(5, owner['id'])
    area += byte_field(11, b'') + byte_field(12, payload)
    return envelope(32768, envelope(9, area))

def incoming_calls(method, body):
    rows, used = fields(body)
    if used != len(body):
        raise ValueError('Reply protobuf trailing bytes')
    messages = []
    if method in (5, 11):
        messages.append(rows)
    elif method == 10:
        for n, w, prop in rows:
            if (n, w) == (3, 2):
                items, _ = fields(prop)
                for pn, pw, message in items:
                    if (pn, pw) == (5, 2):
                        values, _ = fields(message)
                        messages.append(values)
    for message in messages:
        name = next((v for n, w, v in message if (n, w) == (3, 2)), None)
        packed = next((v for n, w, v in message if (n, w) == (12, 2)), None)
        if name is None:
            continue
        indexes, _ = fields(name)
        raw = next((v for n, w, v in indexes if (n, w) == (2, 0)), None)
        if raw is None:
            continue
        index = (raw >> 1) ^ -(raw & 1)
        allowed = ('show_player', 'set_login_info', 'login_fail', 'logout_by_server', 'logout_by_other',
                   'on_relay_to_avatar', 'on_relay_to_account', 'need_queue_up', 'try_to_relay_other')
        label = next((n for n in allowed if rpc_index(n) == index), None)
        if label is None:
            continue
        if packed is None:
            if label in ('try_to_relay_other', 'logout_by_server', 'logout_by_other'):
                yield label, []
            continue
        args = msgpack.unpackb(packed, raw=True, strict_map_key=False,
            max_array_len=4096, max_map_len=4096, max_str_len=1048576, max_bin_len=1048576)
        yield label, decode_rpc_text(args)

def decode_rpc_text(value, depth=0):
    if depth > 16:
        raise ValueError('RPC argument nesting budget')
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8')
        except UnicodeError:
            return value  # Reconnect strings can carry opaque binary bytes.
    if isinstance(value, list):
        return [decode_rpc_text(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {decode_rpc_text(k, depth + 1): decode_rpc_text(v, depth + 1) for k, v in value.items()}
    return value

def token_properties(body):
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Property frame budget exceeded')
    rows, used = fields(body)
    if used != len(body):
        raise ValueError('Property frame trailing bytes')
    guid = next((v for n, w, v in rows if (n, w) == (1, 0)), None)
    visited = 0

    def property_values(prop, depth):
        nonlocal visited
        visited += 1
        if depth > 8 or visited > 20000:
            raise ValueError('Nested property budget exceeded')
        items, _ = fields(prop)
        for pn, pw, value in items:
            if (pn, pw) == (3, 2):
                values, _ = fields(value)
                for vn, vw, text in values:
                    if (vn, vw) == (4, 2) and TOKEN.fullmatch(text):
                        yield guid, text
            elif (pn, pw) == (4, 2):
                # PropItem.dict -> PropDict.item. Initial snapshots can group
                # property values here instead of using flat update items.
                dictionary, _ = fields(value)
                for dn, dw, child in dictionary:
                    if (dn, dw) == (2, 2):
                        yield from property_values(child, depth + 1)

    for n, w, prop in rows:
        if (n, w) == (3, 2):
            yield from property_values(prop, 0)

def creation_token_values(body):
    """Typed AoiProp values in an owned entity's initial snapshot only."""
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Creation snapshot budget')
    pos = 0
    while pos < len(body):
        tag, pos = read_varint(body, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            _, pos = read_varint(body, pos)
        elif wire == 2:
            size, pos = read_varint(body, pos)
            if pos + size > len(body):
                raise ValueError('Truncated creation snapshot')
            if number == 6:
                for _, token in token_properties(body[pos:pos + size]):
                    yield token
            pos += size
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
        else:
            raise ValueError('Invalid creation snapshot wire type')
        if not number or pos > len(body):
            raise ValueError('Creation snapshot bounds')

def only_existing_avatar(args):
    if not isinstance(args, list) or not args or not isinstance(args[0], dict):
        raise ValueError('Character-list shape not recognized')
    avatars = args[0].get('avatars')
    if not isinstance(avatars, list):
        raise ValueError('No returned character list')
    active = [a for a in avatars if isinstance(a, dict) and not a.get('remove_ts')]
    if len(active) != 1 or not isinstance(active[0].get('id'), str) or len(active[0]['id']) != 16:
        raise ValueError('Exactly one active returned test character required')
    return active[0]['id']
