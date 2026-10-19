"""Restricted Account login messages and bounded reply parsing; no network API."""
import hashlib
from pathlib import Path
import sys
try:
    import msgpack
except ImportError:
    # mitmdump ships crypto but no msgpack. The bot's installed msgpack has a
    # pure-Python fallback; its Python-3.11 extension cannot load on Python3.14.
    sys.path.append(str(Path(__file__).resolve().parents[2] / '.venv/Lib/site-packages'))
    import msgpack
from extract_native_protocol_schema import fields, varint as read_varint
from gate_handshake_codec import envelope, byte_field, varint


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


def reply_structure(body):
    """Small structural diagnostic; never exports field values or hashes."""
    rows, _ = fields(body)
    result = {'fields': [(n, w) for n, w, _ in rows[:32]], 'properties': []}
    for n, w, prop in rows:
        if (n, w) != (3, 2) or len(result['properties']) >= 16:
            continue
        items, _ = fields(prop)
        item = {'fields': [(pn, pw) for pn, pw, _ in items]}
        for pn, pw, message in items:
            if (pn, pw) == (5, 2):
                values, _ = fields(message)
                item['message_fields'] = [(mn, mw) for mn, mw, _ in values]
                for mn, mw, name in values:
                    if (mn, mw) == (3, 2):
                        names, _ = fields(name)
                        item['method_fields'] = [(nn, nw, len(v) if isinstance(v, bytes) else None)
                                                 for nn, nw, v in names]
                        raw = next((v for nn, nw, v in names if (nn, nw) == (2, 0)), None)
                        if raw is not None:
                            # Method indexes are protocol identifiers, not tokens.
                            item['method_index'] = (raw >> 1) ^ -(raw & 1)
        result['properties'].append(item)
    return result
