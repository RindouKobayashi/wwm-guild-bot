"""Read-only audit of dungeon row overrides in installed index archives."""
import json, struct, sys, argparse
from collections import Counter
from pathlib import Path
BOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0,str(BOT.parent/'WWM-Toolkit/app'))
import update_item_names as U
import update_trial_dungeons as D
from table_discovery import archives

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=BOT/'maintenance/research/output/override_candidates.json');args=p.parse_args()
 root=Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet\Package\HD\oversea')
 cat=json.loads((BOT.parent/'WWM-Toolkit/cache/table_schema_catalogue.json').read_text(encoding='utf-8'))
 specs=[('trial_displays',{'fuben_id','info_pic','normal_dungeon_index'},'id','name'),('boss_profiles',{'boss_id','boss_pic','boss_no'},'boss_id','boss_name')]
 schemas=[(family,t['fields'],key,name) for family,required,key,name in specs for t in cat['tables'] if required <= set(t['fields'])]
 schemas=list({(f,tuple(s),k,n) for f,s,k,n in schemas})
 print('Reading English localization',flush=True);locale=D.all_english_strings(root)
 records=[]
 for path in archives(root):
  for frame,raw in U.frames(path):
   for key,loc,slot,control in U.frame_records(raw):
    if isinstance(key,int) and 1<=key<=179:records.append((path.name,frame,key,loc))
  print(path.name,'indexed',len(records),flush=True)
 locs={r[3] for r in records};print('Loading',len(locs),'unique locators',flush=True);data=U.load_frames(root,locs)
 found=[]; seen=set()
 for archive,frame,key,loc in records:
  a,f,o,z=struct.unpack('<HHHH',loc);payload=data.get((a,f),b'')[o*8:(o+z)*8]
  for family,schema,key_field,name_field in schemas:
   try:
    reader=U.Reader(payload);row=U.read_row(reader,schema)
    if not row or max(row)>=len(schema) or any(payload[reader.pos:]):continue
    raw={schema[i]:v for i,v in row.items()}
    if raw.get(key_field)!=key or not isinstance(raw.get(name_field),int) or raw[name_field] not in locale:continue
    if family=='trial_displays' and not (isinstance(raw.get('fuben_id'),int) and isinstance(raw.get('info_pic'),str) and isinstance(raw.get('pic'),str)):continue
    if family=='boss_profiles' and not (isinstance(raw.get('boss_no'),int) and isinstance(raw.get('boss_pic'),str) and isinstance(raw.get('boss_icon'),str)):continue
    fingerprint=(archive,frame,key,loc.hex(),family)
    if fingerprint in seen:continue
    seen.add(fingerprint);found.append({'family':family,'key':key,'name':locale[raw[name_field]],'source':f'{archive}:{frame}','locator':loc.hex(),'raw':raw,'named_map':payload[:1]==b'\x0a'})
   except (ValueError,IndexError,UnicodeError,struct.error,TypeError):pass
 args.output.parent.mkdir(parents=True,exist_ok=True)
 args.output.write_text(json.dumps({'policy':'Candidates only; validate archive/table identity before using any override. Game files were read only.','records':len(records),'unique_locators':len(locs),'candidates':found},indent=2,ensure_ascii=False),encoding='utf-8')
 print(Counter((x['family'],x['source'].split(':')[0]) for x in found),flush=True)
 print('Saved',args.output,flush=True)
if __name__=='__main__':main()
