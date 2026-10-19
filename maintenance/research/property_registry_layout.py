"""Bounded parsers for saved-build descriptor metadata; no process access."""
import struct
import re


def pointer(value):
    if not 0x10000 <= value < 0x7fffffff0000 or value & 7:
        raise ValueError('Invalid aligned metadata pointer')
    return value


def registry_header(raw):
    if len(raw) != 64:
        raise ValueError('Expected 64-byte registry header')
    head, count = struct.unpack_from('<QQ', raw, 8)
    if count > 20000:
        raise ValueError('Registry entry cap exceeded')
    pointer(head)
    return head, count


def registry_nodes(read, address):
    """Walk a bounded MSVC list-backed registry, rejecting mutation/corruption."""
    first = read(address, 64); head, count = registry_header(first)
    sentinel = read(head, 32)
    following, previous = struct.unpack_from('<QQ', sentinel)
    last = head; seen = set()
    while following != head:
        pointer(following)
        if following in seen or len(seen) >= count:
            raise ValueError('Registry cycle/count mismatch')
        raw = read(following, 32)
        nxt, prev, key, value = struct.unpack('<QQQQ', raw)
        if prev != last:
            raise ValueError('Registry reciprocal link mismatch')
        seen.add(following); last = following; following = nxt
        yield key, value
    if len(seen) != count or previous != last or read(address,64) != first or read(head,32) != sentinel:
        raise ValueError('Registry changed or failed closure validation')


def property_vector(raw):
    if len(raw) != 0x58:
        raise ValueError('Expected descriptor child-map metadata')
    begin, end, capacity = struct.unpack_from('<QQQ', raw, 0x40)
    if begin == end == capacity == 0:
        return 0, 0
    pointer(begin)
    if not begin <= end <= capacity or (end - begin) % 16 or end - begin > 8192 * 16:
        raise ValueError('Invalid descriptor property vector')
    return begin, (end - begin) // 16


def name_metadata(raw):
    if len(raw) != 56 or raw[0x14] != 1:
        raise ValueError('Not the expected string-key metadata')
    length, capacity = struct.unpack_from('<QQ', raw, 0x28)
    if length > 128 or capacity < length or capacity > 4096:
        raise ValueError('Property-name size bounds failed')
    if capacity < 16:
        if length > 15:
            raise ValueError('Invalid inline key')
        return length, None, raw[0x18:0x18 + length]
    return length, pointer(struct.unpack_from('<Q', raw, 0x18)[0]), None


def target_record(head, tail, expected_key):
    if len(head) != 16 or len(tail) != 16:
        raise ValueError('Incomplete property-record metadata')
    index, flags, type_flags = struct.unpack_from('<III', head)
    key, kind = struct.unpack_from('<QI', tail)
    if key != expected_key or index > 8192 or flags > 65535 or kind != 3:
        raise ValueError('Target record identity/type bounds failed')
    return {'index': index, 'sync_flags': flags, 'type_flags': type_flags,
            'default_kind': kind}


def registered_class_names(read, address, wanted):
    """Resolve only known class identities in the native class-name registry."""
    # This is a distinct custom hash table, NOT the descriptor registry's
    # MSVC list. +8 bucket count, +10 size, +28 bucket array; the extra array
    # slot at bucket_count stores the global singly-linked list head.
    first=read(address,48)
    if len(first)!=48:raise ValueError('Incomplete class registry header')
    buckets,count=struct.unpack_from('<QQ',first,8)
    array=struct.unpack_from('<Q',first,0x28)[0]
    if not 16<=buckets<=131072 or buckets&(buckets-1) or count>20000:
        raise ValueError('Class registry bucket/count bounds')
    pointer(array)
    head_slot=array+buckets*8
    head_raw=read(head_slot,8);following=struct.unpack('<Q',head_raw)[0]
    seen=set();found=[]
    while following:
        pointer(following)
        if following in seen or len(seen)>=count:
            raise ValueError('Class registry cycle/count mismatch')
        raw=read(following,56)
        nxt,bucket_index=struct.unpack_from('<QQ',raw)
        if bucket_index>=buckets:raise ValueError('Class node bucket index bounds')
        identity=struct.unpack_from('<Q',raw,0x30)[0]
        if identity in wanted:
            length,capacity=struct.unpack_from('<QQ',raw,0x20)
            if not 1<=length<=128 or capacity<length or capacity>4096:
                raise ValueError('Class-name bounds failed')
            if capacity<16:
                if length>15:raise ValueError('Invalid inline class name')
                value=raw[0x10:0x10+length]
            else:
                value=read(pointer(struct.unpack_from('<Q',raw,0x10)[0]),length)
            name=value.decode('ascii')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]{0,127}',name):
                raise ValueError('Unexpected registered class name')
            found.append({'entity_class_identity':hex(identity),'entity_class_name':name})
        seen.add(following);following=nxt
    if len(seen)!=count or read(address,48)!=first or read(head_slot,8)!=head_raw:
        raise ValueError('Class registry changed or failed closure validation')
    return found


def image_method_target(read, callback, indirect, image_base, image_size, text_base, text_size):
    target=callback
    if indirect:
        if not image_base<=callback<callback+16<=image_base+image_size:
            raise ValueError('Method descriptor outside image; heap following refused')
        raw=read(callback,16)
        if len(raw)!=16:raise ValueError('Incomplete method descriptor')
        target=struct.unpack_from('<Q',raw,8)[0]
    if not text_base<=target<text_base+text_size:
        raise ValueError('Method target outside original text')
    return target
