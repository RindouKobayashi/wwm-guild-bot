"""Offline protocol-framing checks for saved UDP messages; never decrypts or sends."""
from collections import Counter
import json
import os
from pathlib import Path
import struct


def kcp_segments(data, offset=0):
    """Accept only a complete sequence of standard little-endian KCP segments."""
    segments=[];position=offset
    while position+24<=len(data):
        conversation,command,fragment,window,timestamp,sequence,unacked,size=struct.unpack_from('<IBBHIIII',data,position)
        if command not in (81,82,83,84) or size>len(data)-position-24:
            return None
        if command!=81 and (size or fragment):
            return None
        segments.append((command,fragment,sequence,data[position+24:position+24+size],conversation))
        position+=24+size
    return segments if segments and position==len(data) else None


def reassemble(messages):
    """Deduplicate PUSH segments and accept complete descending-fragment runs."""
    unique={};conflicts=set();duplicates=0
    for message in messages:
        segments=kcp_segments(message)
        if segments is None:
            continue
        for command,fragment,sequence,payload,conversation in segments:
            if command!=81:
                continue
            key=(conversation,sequence);value=(fragment,payload)
            if key in unique:
                duplicates+=1
                if unique[key]!=value:
                    conflicts.add(key)
            else:
                unique[key]=value
    complete=[];consumed=set();incomplete=0
    for key in sorted(unique):
        if key in consumed:
            continue
        conversation,sequence=key;fragment,_=unique[key]
        keys=[(conversation,(sequence+i)&0xffffffff) for i in range(fragment+1)]
        if any(k not in unique or k in conflicts or unique[k][0]!=fragment-i for i,k in enumerate(keys)):
            incomplete+=1;continue
        complete.append(b''.join(unique[k][1] for k in keys));consumed.update(keys)
    return complete,{'unique_push_segments':len(unique),'duplicate_segments':duplicates,
        'conflicting_segments':len(conflicts),'complete_messages':len(complete),
        'incomplete_runs':incomplete,'complete_message_bytes':sum(map(len,complete))}


def contiguous_payloads(messages):
    """Preserve sequence continuity for stream-mode KCP; never bridge a gap."""
    unique={};conflicts=set()
    for message in messages:
        for command,fragment,sequence,payload,conversation in kcp_segments(message) or ():
            if command!=81:
                continue
            key=(conversation,sequence)
            if key in unique and unique[key]!=payload:
                conflicts.add(key)
            unique[key]=payload
    for key in conflicts:
        del unique[key]
    runs=[];consumed=set()
    starts=[key for key in unique if (key[0],(key[1]-1)&0xffffffff) not in unique]
    for key in starts:
        parts=[]
        while key in unique and key not in consumed:
            consumed.add(key);parts.append(unique[key]);key=(key[0],(key[1]+1)&0xffffffff)
        runs.append(b''.join(parts))
    if len(consumed)!=len(unique):
        raise ValueError('Unresolved circular/full sequence domain')
    return runs


class Profiler:
    async def running(self):
        from mitmproxy import ctx,io
        try:
            source=Path(os.environ['WWM_AUTH_CAPTURE'])
            report=[];tokens=set()
            with source.open('rb') as handle:
                for flow in io.FlowReader(handle).stream():
                    request=getattr(flow,'request',None)
                    if request is not None:
                        token=request.headers.get('h72-ms-token')
                        if token:
                            tokens.add(token.encode('utf-8'))
            with source.open('rb') as handle:
                for flow in io.FlowReader(handle).stream():
                    if getattr(flow,'type',None)!='udp':
                        continue
                    peer=getattr(flow.server_conn,'address',None)
                    if not peer or peer[1]!=4040:
                        continue
                    directions={}
                    for direction,from_client in [('inbound',False),('outbound',True)]:
                        messages=[m.content for m in flow.messages if m.from_client==from_client]
                        offset_hits=Counter();command_counts=Counter();compressed=Counter()
                        for data in messages:
                            for name,magic in [('zstd',b'\x28\xb5\x2f\xfd'),('gzip',b'\x1f\x8b\x08')]:
                                if magic in data:
                                    compressed[name]+=1
                            for offset in range(min(33,max(0,len(data)-23))):
                                segments=kcp_segments(data,offset)
                                if segments:
                                    offset_hits[offset]+=1
                                    if offset==0:
                                        command_counts.update(str(s[0]) for s in segments)
                        complete,assembly=reassemble(messages)
                        assembly['messages_containing_known_token']=sum(any(t in m for t in tokens) for m in complete)
                        assembly['messages_containing_token_property_name']=sum(b'ms_request_token' in m for m in complete)
                        streams=contiguous_payloads(messages)
                        assembly['contiguous_sequence_runs']=len(streams)
                        assembly['runs_containing_known_token']=sum(any(t in m for t in tokens) for m in streams)
                        assembly['runs_containing_token_property_name']=sum(b'ms_request_token' in m for m in streams)
                        directions[direction]={'messages':len(messages),'bytes':sum(map(len,messages)),
                            'complete_kcp_matches_by_offset':dict(offset_hits),
                            'kcp_commands_at_offset_zero':dict(command_counts),
                            'compression_magic_candidates':dict(compressed),'reassembly':assembly}
                    report.append({'peer':str(peer),'directions':directions})
            target=Path(os.environ['WWM_PROTOCOL_REPORT'])
            result={'channels':report,'scope':'Syntactic standard KCP segment checks at offsets 0..32, plus compression-magic candidates. No session identifiers, payloads or credentials emitted. A miss does not prove encryption.'}
            target.write_text(json.dumps(result,indent=2),encoding='utf-8')
            print(json.dumps(result,indent=2))
        finally:
            ctx.master.shutdown()


addons=[Profiler()]
