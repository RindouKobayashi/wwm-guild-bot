"""Parse the native 0x8000 wrapper without exporting identifiers or arguments.

Saved serializer 142118250 writes an inner uint32 length, uint16 method, then
protobuf data. The outer gate envelope has already been removed by the caller.
"""
import struct
import hashlib
import msgpack
from extract_native_protocol_schema import fields


def rpc_index(name):
    """Observed default RPC-name index; unrelated to HTTP token signing."""
    return int.from_bytes(hashlib.md5((name + '0').encode()).digest()[-4:], 'big') & 0x7fffffff


def inner_message(body):
    if not 6 <= len(body) <= 8 * 1024 * 1024:
        raise ValueError('Compact message bounds')
    length, method = struct.unpack_from('<IH', body)
    if length != len(body) - 4 or length < 2:
        raise ValueError('Compact inner length mismatch')
    rows, used = fields(body[6:])
    if used != len(body) - 6:
        raise ValueError('Compact protobuf trailing bytes')
    return method, rows


def entity_method_index(rows):
    method_fields = [v for n, w, v in rows if (n, w) == (3, 2)]
    if len(method_fields) != 1:
        raise ValueError('Expected one AreaMessage method')
    values, used = fields(method_fields[0])
    if used != len(method_fields[0]):
        raise ValueError('Method trailing bytes')
    # The captured calls use Md5OrIndex.index (sint32); do not accidentally
    # interpret ZigZag wire values as direct method indexes.
    indexes = [v for n, w, v in values if (n, w) == (2, 0)]
    if len(indexes) != 1 or any((n, w) == (1, 2) for n, w, v in values):
        raise ValueError('Expected an unambiguous indexed method')
    raw = indexes[0]
    if raw > 0xffffffff:
        raise ValueError('Method index exceeds sint32')
    return (raw >> 1) ^ -(raw & 1)


def batch_method_names(payload):
    """Export only method names allowed by current imp_batch_send_rpc.lua."""
    allowed = {'rpc_st_node_attention_ins_interact', 'radiation_arbiters_report',
               'rpc_interact_event_send_to_server', 'rpc_interact_set_pos',
               'rpc_set_comp_ex_attr', 'rpc_set_graph_variable',
               'homeland_pull_data', 'homeland_pull_other_data'}
    if len(payload) > 1024 * 1024:
        raise ValueError('Batch payload bounds')
    args = msgpack.unpackb(payload, raw=False, strict_map_key=False,
                           max_array_len=4096, max_map_len=4096,
                           max_str_len=1048576, max_bin_len=1048576)
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], list):
        raise ValueError('Unexpected batch argument structure')
    names = []
    for call in args[0]:
        if not isinstance(call, list) or not call or not isinstance(call[0], str) or call[0] not in allowed:
            raise ValueError('Unexpected non-allowlisted batch method')
        names.append(call[0])
    return names
