"""Resolve a primary table name from the installed MTable MANIFEST, offline."""
import struct


def binding(raw,name,*,page_links=False):
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
    rows,pages,location,extension,_,_,first_page,_=struct.unpack('<8I',record)
    archive,frame=struct.unpack('<HH',struct.pack('<I',location))
    if archive==65535 or not rows or not pages:raise ValueError('Named table does not bind an indexed schema')
    result = dict(table=name,archive='index'+str(archive),frame=frame,records=rows,pages=pages,
                  manifest_slot=slots[0],record_hex=record.hex())
    if page_links:
        start = pos + primary_count * 32 + first_page * 8
        if first_page + pages > record_count or start + pages * 8 > len(raw):
            raise ValueError('Named table page links exceed the manifest')
        supplemental = []
        for page in range(pages):
            a, f, other_a, other_f = struct.unpack_from('<4H', raw, start + page * 8)
            if (a, f) != (archive, frame + page + 1):
                raise ValueError('Named table page link does not match its primary schema')
            if (other_a, other_f) != (65535, 65535):
                if 65535 in (other_a, other_f):
                    raise ValueError('Invalid supplemental table page link')
                supplemental.append(dict(archive='index'+str(other_a),frame=other_f))
        if len({(item['archive'],item['frame']) for item in supplemental}) != len(supplemental):
            raise ValueError('Duplicate supplemental table page link')
        if extension != 0xFFFFFFFF:
            ext_archive, ext_frame = extension & 0xFFFF, extension >> 16
            if 65535 in (ext_archive, ext_frame):
                raise ValueError('Invalid supplemental table schema link')
            result['supplemental_schema'] = dict(archive='index'+str(ext_archive),frame=ext_frame)
        result['supplemental_pages'] = supplemental
    return result


def installed_binding(root,name,*,page_links=False):
    import hashlib
    import update_item_names as U
    path=root/'MANIFEST';frames=list(U.frames(path))
    if len(frames)!=1:raise ValueError('Unreviewed MTable manifest compression')
    result=binding(frames[0][1],name,page_links=page_links)
    result['manifest_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    return result
