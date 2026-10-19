"""Read constant topic assignments from the original game Lua; never executes it."""
import hashlib
import sqlite3
import struct
import sys
from pathlib import Path
from lua_symbols import inspect


def assignments(tree):
    constants = tree['constants']
    result = {}
    for word in tree['instructions']:
        # Lua 5.4 SETTABUP with constant key and value. Ignore dynamic expressions.
        if word & 127 != 24 or not (word >> 15 & 1):
            continue
        key, value = constants[word >> 16 & 255], constants[word >> 24 & 255]
        if isinstance(key, str) and key.startswith('TOPIC_') and type(value) is int:
            if str(value) in result and result[str(value)] != key:
                raise ValueError('Ambiguous topic enum')
            result[str(value)] = key
    if not result:
        raise ValueError('No direct topic enum assignments')
    return result


def extract(toolkit, game):
    sys.path.insert(0, str(Path(toolkit) / 'app/lib'))
    from wwm import MpkInfoReader, messiah_fileid, lz4_raw_decompress
    source = 'hexm/common/consts/likeunlike_consts.lua'
    identity = messiah_fileid(('LuaText/' + source).encode())
    game = Path(game)
    selected = None
    raw = (game / 'Resources.mpkinfo').read_bytes()
    for fid, offset, size, location in struct.iter_unpack('<QIII', raw[8:8 + struct.unpack_from('<I', raw, 4)[0] * 20]):
        if fid == identity:
            number = location >> 1
            selected = (game / ('Resources.mpk' if not number else f'Resources{number}.mpk'), offset, size)
    for index in sorted((game / 'LocalData/Patch').glob('LT*.mpkinfo')):
        for row in MpkInfoReader(index).iter_records():
            if row.file_id == identity:
                selected = (index.with_suffix('.mpk'), row.archive_offset, row.physical_length)
    patch = game / 'LocalData/Patch'
    database = patch / 'Patch.mpkdb'
    wal = patch / 'Patch.mpkdb-wal'
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Uncheckpointed patch WAL')
    if database.exists():
        with sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
            for fid, size, archive, offset in connection.execute('SELECT v0,v1,v2,v6 FROM info0'):
                value = fid & ((1 << 64) - 1) if isinstance(fid, int) else messiah_fileid(fid.encode())
                if value == identity:
                    selected = (patch / f'Patch{archive}.mpk', offset, size)
    if selected is None:
        raise ValueError('Like topic Lua not found')
    path, offset, size = selected
    if not 4 < size <= 4 * 1024 * 1024:
        raise ValueError('Unbounded Lua record')
    with path.open('rb') as handle:
        handle.seek(offset)
        raw = handle.read(size)
    expected = struct.unpack_from('<I', raw)[0]
    if not 0 < expected <= 4 * 1024 * 1024:
        raise ValueError('Unbounded Lua output')
    bytecode, _ = lz4_raw_decompress(raw[4:], expected)
    tree = inspect(bytecode, instructions=True)
    if tree['source'] != '@' + source:
        raise ValueError('Lua source identity mismatch')
    return assignments(tree), {'source': source, 'sha256': hashlib.sha256(bytecode).hexdigest(),
                               'kind': 'direct Lua constant assignments; symbolic names, not localized display labels'}
