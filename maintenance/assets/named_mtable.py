"""Resolve a primary table name from the installed MTable MANIFEST, offline."""
import struct


def binding(raw,name):
    if not 24<=len(raw)<=128*1024*1024:raise ValueError('Invalid MTable manifest size')
    version,_,primary_count,record_count,alias_count,extra_count=struct.unpack_from('<6I',raw)
    if version!=0x10001 or any(n>500000 for n in (primary_count,record_count,alias_count,extra_count)):
        raise ValueError('Unreviewed MTable manifest format')
    pos=24;primary=[]
    # Three observed, separately sorted name pools. Only primary names bind
    # records here; alias/extra names are traversed to locate the record array.
    for pool,count in enumerate((primary_count,alias_count,extra_count)):
        for _ in range(count):
            if pos+2>len(raw):raise ValueError('Truncated manifest name pool')
            length=struct.unpack_from('<H',raw,pos)[0]
            if not 1<=length<=512 or pos+2+length>len(raw):raise ValueError('Invalid manifest name length')
            value=raw[pos+2:pos+2+length].decode('utf-8')
            if not value.isprintable():raise ValueError('Invalid manifest name')
            if pool==0:primary.append(value)
            pos=(pos+2+length+7)&~7
    slots=[i for i,value in enumerate(primary) if value==name]
    if len(slots)!=1 or slots[0]>=record_count or pos+record_count*32>len(raw):raise ValueError('Missing/ambiguous named table binding')
    record=raw[pos+slots[0]*32:pos+(slots[0]+1)*32]
    rows,pages,location,*_=struct.unpack('<8I',record)
    archive,frame=struct.unpack('<HH',struct.pack('<I',location))
    if archive==65535 or not rows or not pages:raise ValueError('Named table does not bind an indexed schema')
    return dict(table=name,archive='index'+str(archive),frame=frame,records=rows,pages=pages,
                manifest_slot=slots[0],record_hex=record.hex())


def installed_binding(root,name):
    import hashlib
    import update_item_names as U
    path=root/'MANIFEST';frames=list(U.frames(path))
    if len(frames)!=1:raise ValueError('Unreviewed MTable manifest compression')
    result=binding(frames[0][1],name)
    result['manifest_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    return result
