"""Recover public package metadata offline; not an API token/signing key."""
import base64
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'output/test-steam-20261018'
INDEX_HASH = '6a9620feb360c6adb7d54d82e5608f74ca189deec5ac068369bb73052bba299c'


def decode_config(data, key):
    if not data.startswith(b'HEX_') or not 1 <= len(key) <= 256 or len(data) > 65536:
        raise ValueError('Bounded HEX config required')
    raw = base64.b64decode(data[4:], validate=True)
    raw = bytes(v ^ key[i % len(key)] for i, v in enumerate(raw))
    decoder = zlib.decompressobj()
    plain = decoder.decompress(raw, 65537)
    if len(plain) > 65536 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('Package decompression bounds/integrity failure')
    obj = json.loads(plain)
    if not isinstance(obj, dict) or not isinstance(obj.get('spec_tag'), str) or not re.fullmatch('[a-z0-9_]{1,32}', obj['spec_tag']):
        raise ValueError('Package tag schema mismatch')
    return obj


def recover():
    toolkit = HERE.parents[2] / 'WWM-Toolkit'
    sys.path.insert(0, str(toolkit / 'tools'))
    from inspect_avatar_bindings import verified_samples
    captures = toolkit / 'output/companion/private/live-probe'
    cr, cb = verified_samples(captures / '20261015_194843_587098', 'bounded_image_samples_captured')
    dr, db = verified_samples(captures / '20261015_194843_123804', 'bounded_image_samples_captured')
    tx = next(s for s in cr['samples'] if s['section'] == '.text')
    rd = next(s for s in dr['samples'] if s['section'] == '.rdata')
    if tx['sha256'] != 'e7b717126e0a74b971100369a972b1fbcb18439c1092d3f99879b5319430ae23' or rd['sha256'] != 'a1824b2b2e4b9c7d26b70c20f7c1a91b20418da67be79ae62b3b04b12765a58d':
        raise ValueError('Original decoder build mismatch')
    at = 0x140148664 - int(tx['address'], 16)
    if cb['.text'][at:at + 4] != bytes.fromhex('c5f81005'):
        raise ValueError('Original constant-load instruction mismatch')
    address = 0x140148664 + 8 + struct.unpack_from('<i', cb['.text'], at + 4)[0]
    key = db['.rdata'][address - int(rd['address'], 16):address - int(rd['address'], 16) + 27]
    game = Path('C:/Program Files (x86)/Steam/steamapps/common/Where Winds Meet')
    index = (game / 'Resources.mpkinfo').read_bytes()
    if hashlib.sha256(index).hexdigest() != INDEX_HASH:
        raise ValueError('Package archive index build mismatch')
    count = struct.unpack_from('<I', index, 4)[0]
    rows = [row for row in struct.iter_unpack('<QIII', index[8:8 + count * 20]) if row[0] == 0xFFFF10C36769660C]
    if len(rows) != 1 or rows[0][2] > 65536:
        raise ValueError('Unique bounded base package config required')
    _, offset, size, location = rows[0]
    archive = game / ('Resources.mpk' if location >> 1 == 0 else f'Resources{location >> 1}.mpk')
    with archive.open('rb') as handle:
        handle.seek(offset)
        physical = handle.read(size)
    obj = decode_config(physical, key)
    report = {'status': 'native_package_tag_verified', 'spec_tag': obj['spec_tag'],
        'game_index_sha256': INDEX_HASH, 'package_record_sha256': hashlib.sha256(physical).hexdigest(),
        'decoder_scope': 'Original static base64/XOR/zlib asset config decoder; not API authentication',
        'network_requests': 0, 'process_access': False}
    (ROOT / 'package-context.redacted.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def load_verified_tag():
    report = json.loads((ROOT / 'package-context.redacted.json').read_text())
    if report.get('status') != 'native_package_tag_verified' or report.get('game_index_sha256') != INDEX_HASH or report.get('spec_tag') != 'netease':
        raise ValueError('Verified package context missing')
    return report['spec_tag']


if __name__ == '__main__':
    recover()
