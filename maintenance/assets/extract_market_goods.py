"""Extract named hoard-profiteer products -> stuff_items -> original UI icons."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys

BOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BOT))
from utility.game_assets import digest
from utility.market_goods import validate


def primary(root,binding,U,D,wanted=None):
    frames=dict(U.frames(root/binding['archive']));schema=D.decode_name_list(frames[binding['frame']])
    if not schema:raise ValueError('Missing named market table schema')
    # Use the primary shard count from MANIFEST. Later secondary indexes can
    # also look like numeric row maps and must not enter the product namespace.
    records=[]
    for frame in range(binding['frame']+1,binding['frame']+1+binding['pages']):
        records.extend(U.frame_records(frames[frame]))
    if not records or len(records)>binding['records'] or (wanted is None and len(records)!=binding['records']):raise ValueError('Named market primary-index coverage mismatch')
    binding['decoded_primary_records']=len(records)
    if wanted is not None:binding['selected_item_count']=len(wanted)
    selected=[r for r in records if wanted is None or r[0] in wanted]
    chunks=U.load_frames(root,[loc for _,loc,*_ in selected]);rows={}
    for key,locator,*_ in selected:
        a,f,o,z=struct.unpack('<HHHH',locator);raw=chunks[(a,f)][o*8:(o+z)*8];reader=U.Reader(raw)
        row={schema[i]:v for i,v in U.read_row(reader,schema).items()}
        if key in rows or any(raw[reader.pos:]):raise ValueError('Duplicate or unbounded market row')
        rows[key]=row
    if wanted is not None and set(rows)!=set(wanted):raise ValueError('Missing market item definition')
    return rows,schema


def extract(toolkit,game,out):
    sys.path.insert(0,str(toolkit/'app'));import update_trial_dungeons as D
    from named_mtable import installed_binding
    from dungeon_library import export_art
    from original_lua import read_original
    from lua_symbols import inspect
    root=game/'Package/HD/oversea'
    products_binding=installed_binding(root,'hoard_profiteer_products');items_binding=installed_binding(root,'stuff_items')
    products,product_fields=primary(root,products_binding,D.U,D)
    if not {'good_item_id','id','base_price'}<=set(product_fields):raise ValueError('Market product schema changed')
    for key,row in products.items():
        if row.get('good_item_id')!=key:raise ValueError('Market good_item_id primary key mismatch')
    items,item_fields=primary(root,items_binding,D.U,D,set(products))
    if not {'stuff_id','name','icon_no'}<=set(item_fields):raise ValueError('Market item schema changed')
    locale=D.all_english_strings(root);rows={}
    for key,item in items.items():
        if item.get('stuff_id',key)!=key:raise ValueError('Market item primary-key join mismatch')
        value=item.get('name');name=locale.get(value) if type(value) is int else value
        resource=item.get('icon_no')
        if not isinstance(name,str) or not name.strip() or not isinstance(resource,str) or not resource:raise ValueError('Unresolved market name/icon')
        rows[str(key)]=dict(item_id=key,name=name,name_hash=value,icon_resource=resource+'_0_0_ui',product_row_id=products[key]['id'])
    evidence=[]
    for source,required in (
        ('hexm/common/misc/hoard_profiteer_misc.lua',{'main_good','stuff_no','hoard_profiteer_products'}),
        ('hexm/client/ui/windows/hoard_profiteer/bulletin_detail_controller.lua',{'get_item_name','get_item_sys_d','icon_no'})):
        raw,receipt=read_original(toolkit,game,source);tree=inspect(raw)
        def walk(t):
            yield t
            for child in t['children']:yield from walk(child)
        constants={c for t in walk(tree) for c in t['constants'] if isinstance(c,str)}
        if tree['source']!='@'+source or not required<=constants:raise ValueError('Market source references changed; review namespace join')
        evidence.append(dict(receipt,required_constants=sorted(required)))
    out.mkdir(parents=True,exist_ok=True)
    art=export_art(game,out,{'market':{'art':[{'resource':r['icon_resource']} for r in rows.values()]}})
    (out/'source-art-manifest.json').write_text(json.dumps(art,indent=2),encoding='utf-8')
    for key,row in rows.items():
        hits=[art['assets'][u] for u in art['references'].get(row['icon_resource'],[]) if art['assets'][u].get('status')=='EXPORTED' and art['assets'][u]['n']==row['icon_resource']]
        if len(hits)!=1:raise ValueError('Missing/ambiguous regular market icon '+row['icon_resource'])
        path=out/'art'/f'{key}.png';shutil.copy2(out/hits[0]['file'],path)
        row.update(image='art/'+path.name,sha256=digest(path))
    result=dict(format=1,goods=rows,sources=dict(products=products_binding,items=items_binding,
        product_fields=product_fields,item_fields=item_fields,lua=evidence,
        registry_sha256=art['registry_sha256'],join='API main_good -> product key good_item_id -> stuff_items primary key -> name/icon_no; product id is a different value'))
    validate(result);(out/'catalogue.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    render_preview(result,out)
    return result


def render_preview(data,out):
    import html
    cards=''.join('<article><img width=160 src="'+r['image']+'"><h2>'+html.escape(r['name'])+'</h2><p>Item '+str(r['item_id'])+' · '+html.escape(r['icon_resource'])+'</p></article>' for _,r in sorted(data['goods'].items(),key=lambda p:int(p[0])))
    (out/'contact-sheet.html').write_text('<!doctype html><meta charset=utf-8><title>Market goods review</title><style>body{background:#15151d;color:white;font:16px system-ui;padding:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px}article{padding:20px;background:#252530;border-radius:12px}</style><h1>Market goods — original item images</h1><main>'+cards+'</main>',encoding='utf-8')


def copy_export(source,target):
    data=json.loads((source/'catalogue.json').read_text(encoding='utf-8'));validate(data)
    (target/'art').mkdir(parents=True,exist_ok=True);shutil.copy2(source/'catalogue.json',target/'catalogue.json')
    shutil.copy2(source/'contact-sheet.html',target/'contact-sheet.html')
    for row in data['goods'].values():
        if digest(source/row['image'])!=row['sha256']:raise ValueError('Market artwork changed during export')
        shutil.copy2(source/row['image'],target/row['image'])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--toolkit',type=Path,default=BOT.parent/'WWM-Toolkit');p.add_argument('--game-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    data=extract(args.toolkit,args.game_root,args.output);print('Verified',len(data['goods']),'market products, names and original icons')
