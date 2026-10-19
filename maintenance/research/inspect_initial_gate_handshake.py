"""Offline, bounded SessionSeed-shape checks on the earliest saved KCP payloads.

Only candidate locations and nonsecret negotiation flags are exported. A shape
match is not a verified RPC frame; no session seed, payload, or token is emitted.
"""
import json
import os
import struct
from pathlib import Path
from profile_game_capture import kcp_segments
from extract_native_protocol_schema import fields


def seed_shape(data):
    try:
        rows,used=fields(data)
    except ValueError:
        return None
    numbers={n for n,w,v in rows}
    if used!=len(data) or len(rows) not in (2,3) or len(numbers)!=len(rows) or 1 not in numbers or not numbers<={1,2,3}:
        return None
    if any(w!=0 for n,w,v in rows):return None
    values={n:v for n,w,v in rows}
    crypto=values.get(2,0)
    valid_int32=0<=crypto<1<<31 or (1<<64)-(1<<31)<=crypto<1<<64
    if not 0<=values[1]<1<<64 or not valid_int32 or values.get(3,0) not in (0,1):
        return None
    return {'crypto_code':crypto if crypto<1<<31 else crypto-(1<<64),
            'crypto_field_present':2 in numbers,'zstd_enabled':bool(values.get(3,0)),
            'zstd_field_present':3 in numbers}


class Inspect:
    def running(self):
        from mitmproxy import io,ctx
        try:
            results=[]
            with Path(os.environ['WWM_AUTH_CAPTURE']).open('rb') as handle:
                for flow in io.FlowReader(handle).stream():
                    peer=getattr(getattr(flow,'server_conn',None),'address',None)
                    if getattr(flow,'type',None)!='udp' or not peer or peer[1]!=4040:continue
                    unique={};sequences=[]
                    for message in flow.messages:
                        if message.from_client:continue
                        for command,fragment,sequence,payload,conversation in kcp_segments(message.content) or ():
                            if command==81 and len(unique)<64:
                                sequences.append(sequence)
                                unique.setdefault((conversation,sequence),payload)
                    matches=[]
                    for ordinal,payload in enumerate(unique.values()):
                        for offset in range(min(33,len(payload))):
                            for length in range(6,min(33,len(payload)-offset+1)):
                                found=seed_shape(payload[offset:offset+length])
                                if found:matches.append({'payload_ordinal':ordinal,'payload_bytes':len(payload),
                                    'offset':offset,'bytes':length,**found})
                    results.append({'initial_unique_inbound_push_payloads_checked':len(unique),
                                    'first_observed_push_sequence':sequences[0] if sequences else None,
                                    'earliest_sample_sequence':min(sequences) if sequences else None,
                                    'session_seed_shape_candidates':matches,
                                    'first_six_byte_header':
                                      {'uint16_le':list(struct.unpack('<3H',next(iter(unique.values()))[:6])),
                                       'uint32_le_offset0':struct.unpack_from('<I',next(iter(unique.values())))[0]}
                                      if any(m['payload_ordinal']==0 and m['offset']==6 and m['offset']+m['bytes']==m['payload_bytes'] for m in matches)
                                      else None})
            out=Path(os.environ['WWM_HANDSHAKE_REPORT'])
            report={'channels':results,'scope':'First 64 observed unique inbound KCP PUSH payloads per port-4040 flow; candidate offsets 0..32, lengths 6..32. No decryption or verified frame parsing.'}
            out.write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report,indent=2))
        finally:ctx.master.shutdown()


addons=[Inspect()]
