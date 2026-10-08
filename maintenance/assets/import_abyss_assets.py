"""Copy extracted Abyss definitions and original artwork into bot-local data."""
import json,shutil
from pathlib import Path
BOT=Path(__file__).resolve().parents[2]
def main():
    source=BOT.parent/'WWM-Toolkit/output/game_data/abyss_trials'
    output=BOT/'data/abyss_assets';(output/'art').mkdir(parents=True,exist_ok=True)
    data=json.loads((source/'abyss_trials.json').read_text(encoding='utf-8'))
    boards=[]
    for r in data['trials']:
        assets=[a for a in r.get('artwork',{}).get('introduction_pic',[]) if a.get('status')=='EXPORTED' and (source/a['file']).is_file()]
        art=max(assets,key=lambda a:a.get('size',[0,0])[0]*a.get('size',[0,0])[1]) if assets else None
        entry={k:r.get(k) for k in ('id','name','boss_recall_id','rank_tag2','trial_plan_ids','overall_rank_name','no_hit_rank_name','build_rank_names')}
        if art:
            target=output/'art'/f'trial_{r["id"]}.png';shutil.copy2(source/art['file'],target)
            entry['image']='art/'+target.name;entry['image_source']=r['introduction_pic'];entry['sha256']=art['png_sha256']
        boards.append(entry)
    mappings=json.loads((source/'loadout_name_tables.json').read_text(encoding='utf-8'))
    names={kind:{id:row['name'] for id,row in rows.items() if row.get('name')} for kind,rows in mappings.items()}
    (output/'catalogue.json').write_text(json.dumps({'trials':boards,'names':names},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Imported',len(boards),'Abyss trials and',sum(bool(r.get('image')) for r in boards),'original images')
if __name__=='__main__':main()
