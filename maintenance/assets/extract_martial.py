"""Strict offline combat-ID -> path -> original icon extraction."""
import argparse
import hashlib
import html
import json
from pathlib import Path
import shutil
import struct
import sys

BOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BOT))
from utility.game_assets import digest


def decode(reader,schema):
    # The existing packed-row codec omits the zero-width 0x30 false tag.
    # Decode all other types using its existing scalar/container readers.
    count=reader.unpack('<H');result={}
    if count>len(schema):raise ValueError('Invalid martial field count')
    formats={4:'<B',8:'<H',12:'<i',16:'<q',20:'<f',24:'<d'}
    for _ in range(count):
        low,packed=reader.unpack('<B'),reader.unpack('<B');field=low|((packed&3)<<8);tag=packed&252
        if field>=len(schema) or field in result:raise ValueError('Duplicate/out-of-schema martial field')
        if tag in formats:value=reader.unpack(formats[tag])
        elif tag==0:value=None
        elif tag in (28,32):
            value=reader.take(reader.unpack('<H'))
            if tag==32:value=value.decode('utf-8')
        elif tag in (36,40):value=[reader.generic() for _ in range(reader.unpack('<H'))]
        elif tag in (44,48,52):value=tag==52
        else:raise ValueError(f'Unsupported martial type {tag}')
        result[field]=value
    return {schema[i]:v for i,v in result.items()}


def read_tables(root,cat,required,key,U,D,locale):
    hits=[t for t in cat['tables'] if required<=set(t['fields']) and t['records']]
    if not hits:raise ValueError('Missing martial schema: '+','.join(sorted(required)))
    # Base and one explicit patch namespace; never choose an arbitrary candidate.
    expected={'kongfu_id':{'index2','index202'},'liupai_id':{'index3','index203'}}[key]
    if any(t['archive'] not in expected for t in hits):raise ValueError('Unreviewed martial archive namespace')
    by_archive={}
    for t in hits:
        if t['archive'] in by_archive:raise ValueError('Ambiguous martial schema in '+t['archive'])
        by_archive[t['archive']]=t
    rows={};evidence=[]
    for t in sorted(hits,key=lambda t:int(t['archive'][5:])):
        frames=dict(U.frames(root/t['archive']));schema=D.decode_name_list(frames[t['frame']])
        records=U.shard_records(frames,t['frame']);data=U.load_frames(root,[l for _,l,*_ in records]);seen=set()
        for identity,locator,*_ in records:
            if identity in seen:raise ValueError('Duplicate martial ID')
            seen.add(identity);a,f,o,z=struct.unpack('<HHHH',locator);payload=data[(a,f)][o*8:(o+z)*8];reader=U.Reader(payload)
            row=decode(reader,schema)
            if row.get(key)!=identity or any(payload[reader.pos:]):raise ValueError('Martial identity/padding mismatch')
            row['name']=locale.get(row.get('name'),row.get('name'))
            row['source']={'archive':t['archive'],'frame':t['frame'],'locator':locator.hex()}
            rows[str(identity)]=row
        evidence.append(dict(archive=t['archive'],frame=t['frame'],fields=schema,records=len(seen)))
    return rows,evidence


from utility.martial_data import validate


def pack(data,out):
    from PIL import Image,ImageOps
    pack=out/'upload-pack';(pack/'icons').mkdir(parents=True,exist_ok=True);items=[]
    for key,path in data['paths'].items():
        image=Image.open(out/path['file']).convert('RGBA');image=ImageOps.contain(image,(128,128));canvas=Image.new('RGBA',(128,128));canvas.alpha_composite(image,((128-image.width)//2,(128-image.height)//2))
        filename='icons/'+path['icon_key']+'.png';canvas.save(pack/filename,optimize=True)
        if (pack/filename).stat().st_size>256*1024:raise ValueError('Emoji exceeds Discord limit')
        checksum=digest(pack/filename)
        # Runtime uses the upload image digest, original art digest remains separate.
        path['original_sha256']=path['icon_sha256'];path['icon_sha256']=checksum
        members=[r['name'] for r in data['kungfu'].values() if str(r['path_id'])==key]
        items.append(dict(key=path['icon_key'],name='wwm_'+path['icon_key']+'_'+checksum[:8],file=filename,sha256=checksum,path_name=path['name'],kungfu=members))
    for row in data['kungfu'].values():row['icon_sha256']=data['paths'][str(row['path_id'])]['icon_sha256']
    (pack/'upload-manifest.json').write_text(json.dumps(dict(format=1,icons=items),ensure_ascii=False,indent=2),encoding='utf-8')
    cards=''.join('<article><img width="128" height="128" src="'+r['file']+'"><h2>'+html.escape(r['path_name'])+'</h2><code>'+r['name']+'</code><p>'+html.escape(', '.join(r['kungfu']))+'</p></article>' for r in items)
    (pack/'contact-sheet.html').write_text('<!doctype html><meta charset="utf-8"><title>WWM path icons</title><style>body{background:#15151d;color:#eee;font:16px system-ui;padding:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}article{background:#252530;padding:20px;border-radius:12px}code{overflow-wrap:anywhere}</style><h1>Kungfu path icon upload pack</h1><p>One icon per game path. Review before uploading.</p><main>'+cards+'</main>',encoding='utf-8')
    (pack/'UPLOAD-GUIDE.md').write_text('# Discord path icons\n\nOpen contact-sheet.html. Each displayed PNG in icons/ is one application emoji, shared by its listed kungfu.\n\nRun maintenance/assets/upload_martial_emojis.py --environment test --pack "<upload-pack-folder>" for a read-only preview. Add --upload for interactive confirmation. Production requires its application ID. No server emoji upload is needed.\n\nManual fallback: Developer Portal → your application → Emojis. Upload the listed PNG using its exact manifest name, then run the helper with --import-existing to verify remote artwork and save assigned IDs. Never use the test application registry for production.\n',encoding='utf-8')


def extract(toolkit,game,out):
    sys.path.insert(0,str(toolkit/'app'));import update_trial_dungeons as D
    from table_discovery import catalogue
    from dungeon_library import export_art
    root=game/'Package/HD/oversea';cat=catalogue(root,toolkit/'cache/table_schema_catalogue.json');locale=D.all_english_strings(root)
    kungfu,ks=read_tables(root,cat,{'kongfu_id','liupai_id','kongfu_icon','name'},'kongfu_id',D.U,D,locale)
    paths,ps=read_tables(root,cat,{'liupai_small_icon','liupai_id','name'},'liupai_id',D.U,D,locale)
    selected={k:r for k,r in kungfu.items() if r.get('liupai_id') is not None and isinstance(r.get('name'),str) and r['name']}
    unresolved=[k for k,r in kungfu.items() if r.get('liupai_id') is not None and (not isinstance(r.get('name'),str) or not r['name'])]
    if unresolved:raise ValueError('Unresolved localized kungfu IDs: '+str(unresolved))
    used={str(r['liupai_id']) for r in selected.values()}
    for k in used:
        if k not in paths or not isinstance(paths[k].get('name'),str) or not paths[k].get('liupai_small_icon'):raise ValueError('Missing martial path '+k)
    out.mkdir(parents=True,exist_ok=True)
    # Overseas client artwork, regular UI resolution; do not mix 4k variants.
    resources={k:paths[k]['liupai_small_icon']+'_oversea_0_0_ui' for k in used}
    manifest=export_art(game,out,{'martial':{'art':[{'resource':resources[k]} for k in sorted(used)]}})
    (out/'art_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    result=dict(format=1,kungfu={},paths={},sources={'kungfu':ks,'paths':ps,'index_sha256':cat['index_sha256']},excluded_no_path=[k for k in kungfu if k not in selected])
    for key in sorted(used):
        resource=resources[key];candidates=[manifest['assets'][u] for u in manifest['references'].get(resource,[]) if manifest['assets'][u].get('status')=='EXPORTED' and manifest['assets'][u]['n']==resource]
        if len(candidates)!=1:raise ValueError('Missing/ambiguous original icon '+resource+': '+str(len(candidates)))
        art=candidates[0];target='art/path_'+key+'.png';shutil.copy2(out/art['file'],out/target)
        result['paths'][key]=dict(name=paths[key]['name'],icon_key='path_'+key,file=target,resource=resource,icon_sha256=digest(out/target),source=paths[key]['source'])
    for key,row in selected.items():
        path=result['paths'][str(row['liupai_id'])]
        result['kungfu'][key]=dict(name=row['name'],path_id=row['liupai_id'],icon_key=path['icon_key'],icon_sha256=path['icon_sha256'],source=row['source'])
    result['sources']['artwork']=dict(registry_sha256=manifest['registry_sha256'],physical_lookup=manifest.get('physical_lookup'),selection='overseas regular UI variant; 4k excluded')
    result['sources']['game_version']=None
    result['sources']['version_note']='Installed content identified by index/registry fingerprints; no authoritative semantic version supplied.'
    pack(result,out);validate(result)
    (out/'catalogue.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def copy_export(source,target):
    data=json.loads((source/'catalogue.json').read_text(encoding='utf-8'));validate(data)
    target.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source/'catalogue.json',target/'catalogue.json')
    (target/'upload-pack/icons').mkdir(parents=True,exist_ok=True)
    for name in ('upload-manifest.json','contact-sheet.html','UPLOAD-GUIDE.md'):
        shutil.copy2(source/'upload-pack'/name,target/'upload-pack'/name)
    for row in data['paths'].values():
        name=row['icon_key']+'.png'
        shutil.copy2(source/'upload-pack/icons'/name,target/'upload-pack/icons'/name)
    (target/'art').mkdir(exist_ok=True)
    for row in data['paths'].values():
        if digest(source/row['file'])!=row['original_sha256']:raise ValueError('Original artwork changed')
        shutil.copy2(source/row['file'],target/row['file'])
    applications=json.loads((BOT/'assets/discord/martial-applications.json').read_text(encoding='utf-8'))
    approval=dict(format=1,enabled=True,applications=applications,
                  catalogue_sha256=digest(target/'catalogue.json'),
                  upload_manifest_sha256=digest(target/'upload-pack/upload-manifest.json'))
    (target/'startup-approval.json').write_text(json.dumps(approval,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--toolkit',type=Path,default=BOT.parent/'WWM-Toolkit');parser.add_argument('--game-root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=extract(args.toolkit,args.game_root,args.output);print('Verified',len(result['kungfu']),'kungfu and',len(result['paths']),'path icons')
