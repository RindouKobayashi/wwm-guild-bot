"""Find every occurrence of newer original dungeon card names in installed data archives."""
import json,re,struct,sys
from pathlib import Path
BOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0,str(BOT.parent/'WWM-Toolkit/app'))
import update_item_names as U
import update_trial_dungeons as D
from table_discovery import archives

def main():
 root=Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet\Package\HD\oversea')
 pattern=re.compile(rb'(?:wulinlu_tongyou_pic_xj|banner_pic_tongyou_xj)_(?:25|26|27|28|29|30)')
 hits={};occurrences=[];nframes=0;nbytes=0
 for p in sorted(root.glob('data*')):
  archive=int(p.name[4:]);count=0
  for frame,payload in U.frames(p):
   nframes+=1;nbytes+=len(payload);matches=list(pattern.finditer(payload))
   if not matches:continue
   hits[(archive,frame)]=payload;count+=len(matches)
   occurrences.extend({'archive':p.name,'frame':frame,'offset':m.start(),'resource':m.group().decode()} for m in matches)
  print(p.name,count,'new-card occurrences',flush=True)
 print('Resolving index locators for',len(occurrences),'occurrences',flush=True)
 cat=json.loads((BOT.parent/'WWM-Toolkit/cache/table_schema_catalogue.json').read_text(encoding='utf-8'))
 schemas={tuple(t['fields']) for t in cat['tables'] if 'game_list_image' in t['fields'] or 'guild_image' in t['fields']}
 locale=D.all_english_strings(root);rows=[];seen=set()
 for p in archives(root):
  for frame,payload in U.frames(p):
   for key,loc,_,_ in U.frame_records(payload):
    a,f,o,z=struct.unpack('<HHHH',loc)
    if (a,f) not in hits:continue
    row=hits[(a,f)][o*8:(o+z)*8]
    if not pattern.search(row) or (p.name,loc) in seen:continue
    seen.add((p.name,loc));decoded=[]
    for schema in schemas:
     try:
      reader=U.Reader(row);fields=U.read_row(reader,schema)
      if not fields or max(fields)>=len(schema) or any(row[reader.pos:]):continue
      raw={schema[i]:v for i,v in fields.items()}
      names={k:locale[v] for k,v in raw.items() if isinstance(v,int) and v in locale}
      decoded.append({'schema':list(schema),'names':names,'raw':raw})
     except (ValueError,TypeError,IndexError,UnicodeError,struct.error):pass
    rows.append({'source':f'{p.name}:{frame}','key':key,'locator':loc.hex(),'resources':[m.group().decode() for m in pattern.finditer(row)],'decoded':decoded,'payload_hex':row.hex()})
 out=BOT/'maintenance/research/output/new_card_occurrences.json'
 out.write_text(json.dumps({'frames_scanned':nframes,'decompressed_bytes':nbytes,'occurrences':occurrences,'indexed_rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
 print('Saved',len(rows),'rows;',nframes,'frames;',nbytes,'decompressed bytes',flush=True)
 for row in rows:print(json.dumps({k:v for k,v in row.items() if k not in ('payload_hex','decoded')})+' decoded='+str(len(row['decoded'])),flush=True)
if __name__=='__main__':main()
