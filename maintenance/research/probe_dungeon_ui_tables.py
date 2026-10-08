"""Read-only discovery of localized UI table rows with original artwork references."""
import json,re,sys,struct
from pathlib import Path
from collections import Counter
BOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file());sys.path.insert(0,str(BOT.parent/'WWM-Toolkit/app'))
import update_item_names as U
import update_trial_dungeons as D
from dungeon_library import strings
def main():
    root=Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet\Package\HD\oversea')
    cat=json.loads((BOT.parent/'WWM-Toolkit/cache/table_schema_catalogue.json').read_text(encoding='utf-8'))
    print('Read-only UI table scan',flush=True);locale=D.all_english_strings(root)
    tables=[t for t in cat['tables'] if t['records']<=1000 and len(t['fields'])<200 and any(re.search('pic|image|banner|lihui',x) for x in t['fields']) and any(x in t['fields'] for x in ['name','group_name','boss_name','title','instance_name','sidepage_name','category_name']) and not any(x in t['fields'] for x in ['stuff_id','stuff_no','delivery_category','guise_suit','skill_cd'])]
    indices={};records=[]
    for t in tables:
     if t['archive'] not in indices:indices[t['archive']]=dict(U.frames(root/t['archive']))
     for key,loc,slot,control in U.shard_records(indices[t['archive']],t['frame']):records.append((t,key,loc))
    print(len(tables),'tables,',len(records),'locators',flush=True);data=U.load_frames(root,{r[2] for r in records});rows=[]
    for t,key,loc in records:
     a,f,o,z=struct.unpack('<HHHH',loc);payload=data.get((a,f),b'')[o*8:(o+z)*8];schema=t['fields']
     try:
      reader=U.Reader(payload);fields=U.read_row(reader,schema)
      if not fields or max(fields)>=len(schema) or any(payload[reader.pos:]):continue
      raw={schema[i]:v for i,v in fields.items()}
      idfield=next((x for x in ['id','no','group_id','boss_id'] if x in raw),None)
      if idfield and raw[idfield]!=key:continue
      names={k:locale.get(v,'') if isinstance(v,int) else v for k,v in raw.items() if re.search('name|title',k) and not re.search('pic|image',k)}
      names={k:v for k,v in names.items() if isinstance(v,str) and v}
      art=[{'field':k,'resource':v} for k,value in raw.items() if re.search('pic|image|banner|lihui',k) for v in strings(value) if v]
      if not names or not art:continue
      rows.append({'source':f"{t['archive']}:{t['frame']}",'key':key,'locator':loc.hex(),'names':names,'art':art,'raw':raw})
     except (ValueError,TypeError,IndexError,UnicodeError,struct.error):pass
    out=BOT/'maintenance/research/output/ui_art_rows.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps({'tables':len(tables),'records':len(records),'rows':rows},indent=2,ensure_ascii=False),encoding='utf-8');print(len(rows),'localized artwork rows saved',flush=True)
    for row in rows:
     if any(re.search(r'abyssal|serpent|blazing|jinming|rouge|segmented|landscourge|dragonscale|wailing|feng ruzhi|qianye|yewanshan',v,re.I) for v in row['names'].values()):print(row['source'],row['key'],json.dumps(row['names'],ensure_ascii=True),row['art'],flush=True)

if __name__ == "__main__":
    main()
