"""Carve bounded protobuf file descriptors from the saved immutable engine.

No process access, code execution, credentials, or application packet decoding.
Descriptors stop at padding; only recognized descriptor fields are accepted.
"""
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
TOOLKIT=HERE.parents[2]/'WWM-Toolkit'
sys.path.insert(0,str(TOOLKIT/'tools'))
from inspect_avatar_bindings import verified_samples


def varint(data, position):
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
        key,position=varint(data,position);number=key>>3;wire=key&7
        if not number:raise ValueError('Zero field number')
        if wire==0:value,position=varint(data,position)
        elif wire==2:
            length,position=varint(data,position)
            if length>1048576 or position+length>len(data):raise ValueError('Length bounds')
            value=data[position:position+length];position+=length
        elif wire in (1,5):
            length=8 if wire==1 else 4
            value=data[position:position+length];position+=length
            if len(value)!=length:raise ValueError('Truncated fixed field')
        else:raise ValueError('Unsupported descriptor wire type')
        result.append((number,wire,value))
    return result,position


def text(rows,number,default=''):
    return next((v.decode('utf-8') for n,w,v in rows if n==number and w==2),default)


def integer(rows,number):
    return next((v for n,w,v in rows if n==number and w==0),None)


def message(data):
    rows,_=fields(data)
    values=[];nested=[];enums=[]
    for n,w,v in rows:
        if n==2 and w==2:
            f,_=fields(v)
            values.append({'name':text(f,1),'number':integer(f,3),'label':integer(f,4),
                           'type':integer(f,5),'type_name':text(f,6)})
        elif n==3 and w==2:nested.append(message(v))
        elif n==4 and w==2:enums.append(enum(v))
    return {'name':text(rows,1),'fields':values,'nested':nested,'enums':enums}


def enum(data):
    rows,_=fields(data);values=[]
    for n,w,v in rows:
        if n==2 and w==2:
            f,_=fields(v);values.append({'name':text(f,1),'number':integer(f,2)})
    return {'name':text(rows,1),'values':values}


def descriptor(blob,offset):
    rows,length=fields(blob[offset:offset+65536],padding=True)
    if any(n not in range(1,13) for n,w,v in rows):raise ValueError('Unexpected file field')
    return {'name':text(rows,1),'package':text(rows,2),'bytes':length,
            'boundary':'Parsed until first zero padding; registration length not yet independently verified',
            'dependencies':[v.decode() for n,w,v in rows if n==3 and w==2],
            'messages':[message(v) for n,w,v in rows if n==4 and w==2],
            'enums':[enum(v) for n,w,v in rows if n==5 and w==2],
            'services':[service(v) for n,w,v in rows if n==6 and w==2]}


def service(data):
    rows,_=fields(data);methods=[]
    for n,w,v in rows:
        if n==2 and w==2:
            f,_=fields(v)
            methods.append({'name':text(f,1),'input':text(f,2),'output':text(f,3)})
    return {'name':text(rows,1),'methods':methods}


def main():
    capture=TOOLKIT/'output/companion/private/live-probe/20261015_194843_123804'
    receipt,blobs=verified_samples(capture,'bounded_image_samples_captured')
    section=next(s for s in receipt['samples'] if s['section']=='.rdata')
    blob=blobs['.rdata'];base=int(section['address'],16);results=[]
    for name in ('client_gate.proto','gate_game.proto','common.proto','server_common.proto'):
        needle=b'\x0a'+bytes([len(name)])+name.encode();offset=blob.find(needle)
        if offset<0:raise ValueError('Missing descriptor '+name)
        result=descriptor(blob,offset)
        if result['name']!=name:raise ValueError('Descriptor identity differs')
        output=HERE/'output/token-update-20261016/native/descriptors'
        output.mkdir(parents=True,exist_ok=True)
        (output/(name+'.bin')).write_bytes(blob[offset:offset+result['bytes']])
        result['address']=hex(base+offset);results.append(result)
    report={'rdata_sha256':section['sha256'],'descriptors':results,
            'scope':'Static schema only; no live token/property value or network message decoded.'}
    out=HERE/'output/token-update-20261016/native/native_protocol_schema.json'
    out.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'files':[(r['name'],r['bytes'],len(r['messages'])) for r in results],'output':str(out)}))


if __name__=='__main__':main()
