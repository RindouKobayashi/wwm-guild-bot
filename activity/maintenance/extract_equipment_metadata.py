"""Read installed game tables into portable equipment metadata; never edits game files.
Run before build_player_mappings.py. Requires the sibling WWM-Toolkit checkout.
"""
import argparse, ast, csv, json, struct, sys
from pathlib import Path
BOT=Path(__file__).resolve().parents[2]
TOOLKIT=BOT.parent/'WWM-Toolkit'
sys.path.insert(0,str(TOOLKIT/'app'))
import update_equipment_names as codec

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--game-root',type=Path,default=Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet\Package\HD\oversea'))
    parser.add_argument('--details',type=Path)
    args=parser.parse_args()
    details=args.details or max(TOOLKIT.glob('output/game_data/runs/*/equipment/all_equipment_item_details.csv'),key=lambda p:p.stat().st_mtime)
    catalogue=json.loads((TOOLKIT/'cache/table_schema_catalogue.json').read_text(encoding='utf-8'))['tables']
    schemas={t['archive']:t['fields'] for t in catalogue if 'equip_level' in t['fields'] and 'legacy_id' in t['fields']}
    with details.open(encoding='utf-8-sig',newline='') as f:items=list(csv.DictReader(f))
    def locator(text):return ast.literal_eval(text) if text.startswith("b'") or text.startswith('b"') else bytes.fromhex(text)
    locs=[locator(r['locator_hex']) for r in items]
    frames=codec.load_data_frames(args.game_root,locs)
    metadata={};failures=[]
    for r,loc in zip(items,locs):
        try:
            raw,_=codec.decode_locator(loc,frames);fields=schemas.get(r['index_archive'],schemas['index1'])
            row={fields[k]:v for k,v in raw.items() if isinstance(k,int) and 0<=k<len(fields)}
            if row.get('id')!=int(r['item_id']) or row.get('name')!=int(r['localization_key']):raise ValueError('Equipment primary key/name mismatch')
            metadata[r['item_id']]={'tier':row.get('equip_level'),'quality_code':row.get('star'),'legacy_id':row.get('legacy_id')}
        except (ValueError,KeyError,IndexError) as e:failures.append({'id':r['item_id'],'error':str(e)})
    tables=[t for t in catalogue if (t['archive']=='index1' and ('legacy_affix_upper_limit' in t['fields'] or 'slot_name' in t['fields'])) or 'set_icon' in t['fields']]
    decoded=[]
    for archive in dict.fromkeys(t['archive'] for t in tables):
        targets={f:t for t in tables if t['archive']==archive for f in t['shard_frames']};locs=[]
        for frame,raw in codec.frames(args.game_root/archive):
            if frame in targets:
                for key,loc in codec.parse_record_frame(raw) or []:
                    if (args.game_root/f'data{struct.unpack("<H",loc[:2])[0]}').exists():locs.append((targets[frame],key,loc))
            if frame>=max(targets):break
        frames=codec.load_data_frames(args.game_root,[x[2] for x in locs])
        for table,key,loc in locs:
            try:raw,_=codec.decode_locator(loc,frames)
            except Exception:continue
            fields=table['fields'];row={fields[k]:v for k,v in raw.items() if isinstance(k,int) and 0<=k<len(fields)}
            if row.get('id',row.get('level',key))!=key:continue
            decoded.append(row)
    wanted={r[k] for r in decoded for k in ['name','slot_name'] if isinstance(r.get(k),int)};strings={}
    for name in codec.LOCALIZATION_FILES:strings.update(codec.lookup_file(args.game_root/'locale'/name,wanted)[0])
    sets={};slots={};legacy={}
    for row in decoded:
        if 'value_add_2' in row and isinstance(row.get('name'),int) and row['name'] in strings:sets[str(row['id'])]=strings[row['name']]
        if 'slot_id' in row and isinstance(row.get('slot_name'),int) and row['slot_name'] in strings:slots[str(row['slot_id'])]=strings[row['slot_name']]
        if 'legacy_affix_upper_limit' in row:legacy[str(row['level'])]=row['legacy_affix_upper_limit']
    result={'sources':[str(details.relative_to(TOOLKIT)), 'cache/table_schema_catalogue.json','installed equipment, set, slot and legacy tables; English locale'],
            'equipment_metadata':metadata,'equipment_sets':sets,'equipment_slots':slots,'legacy_limits':legacy,'unresolved_metadata':failures}
    target=BOT/'data/equipment_metadata.json';target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Exported {len(metadata)} equipment records, {len(sets)} sets, {len(slots)} slot names, {len(legacy)} relay limits; {len(failures)} unresolved.')
if __name__=='__main__':main()
