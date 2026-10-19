"""Offline paired receive decoding; saves redacted findings, never plaintext."""
import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import struct
import zstandard
from private_transport_state import unprotect_for_current_user
from transport_rc4_state import RC4State
from inspect_initial_gate_handshake import seed_shape

TOKEN=re.compile(rb'(?<![0-9])[0-9]{9,12}:[a-fA-F0-9]{32}(?![a-fA-F0-9])')


def decode_envelopes(data):
    position=0;total=0;frames=[]
    while position<len(data):
        if len(data)-position<4:raise ValueError('Partial envelope header')
        word=struct.unpack_from('<I',data,position)[0]
        compressed=bool(word&0x80000000);length=word&0x7fffffff
        if not 2<=length<=0xffffff or position+4+length>len(data):
            raise ValueError('Envelope length bounds/partial body')
        body=data[position+4:position+4+length]
        if compressed:
            if len(body)<4:raise ValueError('Compressed size field absent')
            expected=struct.unpack_from('<I',body)[0]
            if not 2<=expected<=8*1024*1024:raise ValueError('Decoded size bounds')
            body=zstandard.ZstdDecompressor().decompress(body[4:],max_output_size=expected+1,
                                                       allow_extra_data=False)
            if len(body)!=expected:raise ValueError('Decoded size mismatch')
        total+=len(body)
        if total>64*1024*1024 or len(frames)>=65536:raise ValueError('Decoded stream budget')
        method=struct.unpack_from('<H',body)[0]
        frames.append((method,body[2:],position,compressed))
        position+=4+length
    return frames


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('streams',type=Path);parser.add_argument('sealed',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    evidence=json.loads(args.streams.read_text());sealed=json.loads(args.sealed.read_text())
    candidates=[s for s in evidence['streams'] if s.get('transport')=='tcp' and s['direction']=='receive']
    results=[];expected_http={v['sha256'] for v in evidence['http_token_fingerprints']}
    for stream in candidates:
        raw=base64.b64decode(stream['network_bytes_base64'])
        if len(raw)<6:continue
        first_length,method=struct.unpack_from('<IH',raw)
        if not 2<=first_length<=64 or method!=0:continue
        handshake_end=4+first_length
        shape=seed_shape(raw[6:handshake_end])
        if not shape or shape['crypto_code']!=0:continue
        snapshot=sealed['directions']['receive']
        state_bytes=unprotect_for_current_user(base64.b64decode(snapshot['protected_state_base64']))
        if hashlib.sha256(state_bytes).hexdigest()!=snapshot['state_sha256']:
            raise ValueError('Protected state fingerprint mismatch')
        sample_time=datetime.fromisoformat(snapshot['captured_from_utc']).timestamp()
        started=datetime.fromisoformat(sealed['process_started_utc']).timestamp()
        if not started-1<=stream['segments'][0]['timestamp']<=sample_time<=stream['segments'][-1]['timestamp']:
            raise ValueError('Recording does not span this process/snapshot')
        consumed=sum(s['bytes'] for s in stream['segments'] if s['timestamp']<=sample_time)-handshake_end
        state=RC4State.from_engine_bytes(state_bytes)
        if not 0<=consumed<=len(raw)-handshake_end or consumed%256!=state.x:
            raise ValueError('Timestamp-derived receive alignment does not match state index')
        state.rewind(consumed)
        if state.x!=0 or state.y!=0:raise ValueError('Rewound receive state not initial')
        frames=decode_envelopes(state.apply(raw[handshake_end:]))
        hits=[]
        for ordinal,(method,body,offset,compressed) in enumerate(frames):
            for token in set(TOKEN.findall(body)):
                fp=hashlib.sha256(token).hexdigest()
                hits.append({'frame_ordinal':ordinal,'method_word':method,'body_bytes':len(body),
                             'token_sha256':fp,'matches_captured_http_token':fp in expected_http})
        results.append({'flow_ordinal':stream['flow_ordinal'],'transport':'tcp','direction':'receive',
            'plain_handshake_bytes':handshake_end,'ciphertext_bytes':len(raw)-handshake_end,
            'state_alignment_bytes':consumed,'initial_state_indices_verified':True,
            'decoded_frames':len(frames),'compressed_frames':sum(f[3] for f in frames),
            'decoded_body_bytes':sum(len(f[1]) for f in frames),
            'method_counts':dict(Counter(str(f[0]) for f in frames)),
            'token_fingerprint_matches':hits})
    if not results:raise ValueError('No matching receive handshake found')
    report={'status':'paired_receive_decoded','pid':sealed['pid'],
        'capture_sha256':evidence['capture_sha256'],'protected_state_source':str(args.sealed),
        'streams':results,'plaintext_saved':False,'api_requests_made':False,
        'limits':['Receive transport decoding only; token generation algorithm is not identified.',
                  'Timestamp-based alignment was checked against initial state and complete envelope decoding.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'streams':len(results),
        'frames':sum(s['decoded_frames'] for s in results),
        'matching_http_token_occurrences':sum(h['matches_captured_http_token'] for s in results for h in s['token_fingerprint_matches']),
        'report':str(args.output)}))


if __name__=='__main__':main()
