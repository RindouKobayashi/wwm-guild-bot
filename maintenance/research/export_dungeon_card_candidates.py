"""Export original dungeon-card candidates for visual review, without assigning boards."""
import json,sys,html
from pathlib import Path
BOT=next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0,str(BOT.parent/'WWM-Toolkit/app'))
from dungeon_library import export_art

def main():
 candidates=json.loads((BOT/'maintenance/research/output/registry_candidates.json').read_text(encoding='utf-8'))
 names=sorted({r['n'].removesuffix('_4k').removesuffix('_0_0_ui') for r in candidates['textures'] if r['n'].startswith('wulinlu_tongyou_pic_xj_')})
 groups={'candidates':{'art':[{'resource':n} for n in names]}}
 out=BOT/'maintenance/research/output/dungeon_card_candidates';out.mkdir(parents=True,exist_ok=True)
 manifest=export_art(Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet'),out,groups)
 (out/'art_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
 cards=[]
 from PIL import Image,ImageDraw
 sheet=Image.new('RGB',(1000,((len(names)+2)//3)*180),'#20252c');draw=ImageDraw.Draw(sheet)
 for i,n in enumerate(names):
  assets=[manifest['assets'][u] for u in manifest['references'][n] if manifest['assets'][u]['status']=='EXPORTED']
  if not assets:continue
  a=max(assets,key=lambda a:a['size'][0]*a['size'][1]);im=Image.open(out/a['file']).convert('RGBA');im.thumbnail((320,145))
  x=(i%3)*333;y=(i//3)*180;sheet.paste(im,(x,y),im);draw.text((x,y+148),n.replace('wulinlu_tongyou_pic_',''),fill='white')
  cards.append('<figure><img src="'+a['file']+'"><figcaption>'+html.escape(n)+'</figcaption></figure>')
 sheet.save(out/'contact_sheet.jpg')
 (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Unassigned original dungeon cards</title><style>body{background:#20252c;color:white;font:16px system-ui}figure{display:inline-block;width:400px}img{width:100%}</style><h1>Original dungeon card candidates — mappings unverified</h1>'+''.join(cards),encoding='utf-8')
 print(out,flush=True)
if __name__=='__main__':main()
