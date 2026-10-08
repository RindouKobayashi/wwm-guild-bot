"""Refresh original dungeon activity-card bindings from installed game files (read only)."""
import json,sys,struct
from pathlib import Path
BOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0,str(BOT.parent/'WWM-Toolkit/app'))
import update_item_names as U
import update_trial_dungeons as D
from table_discovery import catalogue, archives
from dungeon_library import export_art

def main():
 game=Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet')
 root=game/'Package/HD/oversea'
 cat=catalogue(root,BOT.parent/'WWM-Toolkit/cache/table_schema_catalogue.json')
 tables=[t for t in cat['tables'] if {'game','game_list_image','rank_id','mate_info_id','team_game_id'}<=set(t['fields'])]
 locale=D.all_english_strings(root);records=[];indices={}
 for t in tables:
  if t['archive'] not in indices:indices[t['archive']]=dict(U.frames(root/t['archive']))
  records.extend((t,k,l) for k,l,_,_ in U.shard_records(indices[t['archive']],t['frame']))
 # Patch indices can contain rows without repeating the base schema.
 schemas=list({tuple(t['fields']) for t in tables})
 known={r[2] for r in records}
 for path in archives(root):
  if path.name not in indices:indices[path.name]=dict(U.frames(path))
  for frame,payload in indices[path.name].items():
   for key,loc,_,_ in U.frame_records(payload):
    if loc in known or not isinstance(key,int) or not (1<=key<=1000 or 32000<=key<=33000):continue
    known.add(loc)
    for schema in schemas:records.append(({'archive':path.name,'frame':frame,'fields':schema},key,loc))
 print('Checking activity-card rows and schema-less patch records:',len(records),flush=True)
 data=U.load_frames(root,{r[2] for r in records});rows=[]
 for t,key,loc in records:
  a,f,o,z=struct.unpack('<HHHH',loc);payload=data.get((a,f),b'')[o*8:(o+z)*8]
  try:
   reader=U.Reader(payload);fields=U.read_row(reader,t['fields'])
   if not fields or max(fields)>=len(t['fields']) or any(payload[reader.pos:]):continue
   raw={t['fields'][i]:v for i,v in fields.items()}
   if not isinstance(raw.get('rank_id'),str) or not raw['rank_id'].startswith(('rank_team_dungeon_','rank_team10_dungeon_')):continue
   if key not in (raw.get('mate_info_id'),raw.get('team_game_id'),raw.get('guild_team_id')):continue
   resource=raw.get('game_list_image')
   if not isinstance(resource,str) or not resource.startswith('wulinlu_tongyou_pic_xj_'):continue
   rows.append({'source':f"{t['archive']}:{t['frame']}",'key':key,'locator':loc.hex(),'name':locale.get(raw.get('game')),'raw':raw})
  except (ValueError,TypeError,IndexError,UnicodeError,struct.error):continue
 if not rows:raise RuntimeError('No validated dungeon activity-card bindings found; previous bundle retained')
 out=BOT/'maintenance/research/output/dungeon_cards';out.mkdir(parents=True,exist_ok=True)
 reviews=json.loads((BOT/'maintenance/assets/dungeon_card_reviews.json').read_text(encoding='utf-8'))['reviews']
 groups={'cards':{'art':[{'resource':r['raw']['game_list_image']} for r in rows] + [{'resource':r['resource']} for r in reviews]}}
 manifest=export_art(game,out,groups)
 (out/'art_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
 (out/'bindings.json').write_text(json.dumps({'policy':'Original activity cards, explicit rank_id and rank_id_replace; no boss gallery fallback','rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
 print('Refreshed',len(rows),'dungeon activity-card records',flush=True)
if __name__=='__main__':main()
