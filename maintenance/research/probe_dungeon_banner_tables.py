"""Read game UI artwork tables without requiring localized name columns."""
import json,sys,struct,re
from pathlib import Path
BOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0,str(BOT.parent/'WWM-Toolkit/app'))
import update_item_names as U
import update_trial_dungeons as D
from dungeon_library import strings

def main():
 root=Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet\Package\HD\oversea')
 cat=json.loads((BOT.parent/'WWM-Toolkit/cache/table_schema_catalogue.json').read_text(encoding='utf-8'))
 tables=[t for t in cat['tables'] if any(re.search('pic|image|banner|lihui',f) for f in t['fields'])]
 locale=D.all_english_strings(root);indices={};records=[]
 for t in tables:
  if t['archive'] not in indices:indices[t['archive']]=dict(U.frames(root/t['archive']))
  records.extend((t,k,l) for k,l,_,_ in U.shard_records(indices[t['archive']],t['frame']))
 print(len(tables),'schemas;',len(records),'records',flush=True)
 data=U.load_frames(root,{r[2] for r in records});hits=[]
 for t,key,loc in records:
  a,f,o,z=struct.unpack('<HHHH',loc);payload=data.get((a,f),b'')[o*8:(o+z)*8]
  if not any(s in payload for s in (b'wulinlu_tongyou',b'duorenzhenshou',b'zhenshou_pic')):continue
  try:
   reader=U.Reader(payload);fields=U.read_row(reader,t['fields'])
   if not fields or max(fields)>=len(t['fields']) or any(payload[reader.pos:]):continue
   raw={t['fields'][i]:v for i,v in fields.items()}
   names={k:locale[v] for k,v in raw.items() if isinstance(v,int) and v in locale}
   art=[{'field':k,'resource':s} for k,v in raw.items() for s in strings(v) if re.search('wulinlu_tongyou|duorenzhenshou|zhenshou_pic',s)]
   if art:hits.append({'source':f"{t['archive']}:{t['frame']}",'key':key,'locator':loc.hex(),'names':names,'art':art,'raw':raw})
  except (ValueError,TypeError,IndexError,UnicodeError,struct.error):pass
 out=BOT/'maintenance/research/output/dungeon_banner_rows.json';out.parent.mkdir(parents=True,exist_ok=True)
 out.write_text(json.dumps({'schemas':len(tables),'records':len(records),'rows':hits},ensure_ascii=False,indent=2),encoding='utf-8')
 from collections import Counter
 print('Sources:',Counter(x['source'] for x in hits),flush=True)
 for row in hits:
  if row['source']!='index1:13160':print(json.dumps(row,ensure_ascii=True),flush=True)
if __name__=='__main__':main()
