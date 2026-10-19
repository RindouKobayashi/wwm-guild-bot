"""Decode the paired outgoing stream; export envelope metadata, never RPC bodies."""
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
from decode_paired_transport import decode_envelopes
from private_transport_state import unprotect_for_current_user
from transport_rc4_state import RC4State


def load_pair():
    root = Path(__file__).resolve().parent / 'output'
    folder = root / 'token-update-20261016/paired-state-135247'
    evidence = json.loads((folder / 'streams.private.json').read_text())
    sealed = json.loads((root / 'token-descriptor-metadata/20261016_135247_542930/transport-states.user-dpapi.json').read_text())
    receive = next(s for s in evidence['streams'] if s.get('port') == 4100 and s['direction'] == 'receive')
    send = next(s for s in evidence['streams'] if s.get('port') == 4100 and s['direction'] == 'send' and s['flow_ordinal'] == receive['flow_ordinal'])
    raw = base64.b64decode(receive['network_bytes_base64'])
    skip = 4 + struct.unpack_from('<I', raw)[0]
    snapshot = sealed['directions']['receive']
    state_bytes = unprotect_for_current_user(base64.b64decode(snapshot['protected_state_base64']))
    if hashlib.sha256(state_bytes).hexdigest() != snapshot['state_sha256']:
        raise ValueError('State fingerprint mismatch')
    moment = datetime.fromisoformat(snapshot['captured_from_utc']).timestamp()
    consumed = sum(s['bytes'] for s in receive['segments'] if s['timestamp'] <= moment) - skip
    state = RC4State.from_engine_bytes(state_bytes)
    if state.x != consumed % 256:
        raise ValueError('Receive alignment mismatch')
    state.rewind(consumed)
    if state.x or state.y:
        raise ValueError('Not an initial state')
    # The native paired mode-zero constructors initialize both directions with
    # the same key. This is NOT independent alignment of the send snapshot.
    outgoing = base64.b64decode(send['network_bytes_base64'])
    offset = 0
    for expected_method in (0, 1):
        size, method = struct.unpack_from('<IH', outgoing, offset)
        if method != expected_method or not 2 <= size <= 4096:
            raise ValueError('Unexpected outgoing handshake')
        offset += size + 4
    send_snapshot = sealed['directions']['send']
    send_state = unprotect_for_current_user(base64.b64decode(send_snapshot['protected_state_base64']))
    if hashlib.sha256(send_state).hexdigest() != send_snapshot['state_sha256']:
        raise ValueError('Send state fingerprint mismatch')
    target = RC4State.from_engine_bytes(send_state)
    check = RC4State.from_engine_bytes(state.to_engine_bytes())
    aligned = []
    # Check the FULL captured state, not merely the modulo-256 index. No state
    # bytes are exported. This independently tests the shared-key assumption.
    position = target.x
    check.apply(bytes(position))
    while position <= len(outgoing) - offset:
        if check.to_engine_bytes() == send_state:
            aligned.append(position)
        position += 256
        if position <= len(outgoing) - offset:
            check.apply(bytes(256))
    if len(aligned) != 1:
        raise ValueError('Send snapshot must match exactly one stream position')
    frames = decode_envelopes(state.apply(outgoing[offset:]))
    return folder, evidence, send, offset, frames, aligned[0]


def main():
    folder, evidence, send, offset, frames, aligned_position = load_pair()
    outgoing_length = sum(s['bytes'] for s in send['segments'])
    segment_ends = []
    end = 0
    for segment in send['segments']:
        end += segment['bytes']
        segment_ends.append((end, segment['timestamp']))
    rows = []
    for ordinal, (method, body, position, compressed) in enumerate(frames):
        next_position = frames[ordinal + 1][2] if ordinal + 1 < len(frames) else outgoing_length - offset
        timestamp = next(t for end, t in segment_ends if end >= offset + next_position)
        rows.append({'ordinal': ordinal, 'method_word': method, 'body_bytes': len(body),
                     'compressed': compressed,
                     'segment_end_utc': datetime.fromtimestamp(timestamp, timezone.utc).isoformat()})
    report = {'status': 'paired_outgoing_envelopes_decoded',
              'capture_sha256': evidence['capture_sha256'], 'handshake_bytes': offset,
              'send_snapshot_full_state_verified': True,
              'send_snapshot_ciphertext_position': aligned_position,
              'frames': rows, 'method_counts': dict(Counter(str(r['method_word']) for r in rows)),
              'raw_method_body_sizes': dict(Counter(str(len(b)) for m,b,p,c in frames if m == 32768)),
              'plaintext_saved': False, 'api_requests_made': False,
              'limitations': ['Shared initial key verified against the complete captured send state and complete envelope decoding.',
                              'Segment timestamps show observed arrival at recorder, not exact server processing or causation.',
                              'High-bit method payloads require their own native parser; no RPC semantics inferred.']}
    path = folder / 'outgoing-envelopes.redacted.json'
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'frames': len(rows), 'methods': report['method_counts'], 'report': str(path)}))


if __name__ == '__main__':
    main()
