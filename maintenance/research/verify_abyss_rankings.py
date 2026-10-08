"""Bounded read-only checks of Lua-defined Abyss dynamic ranking names."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from utility import wwm
from settings import WWM_UID,RANK_GET_RANKLIST_URL

async def main():
    toolkit=Path(__file__).resolve().parents[3]/'WWM-Toolkit'
    output=toolkit/'output/game_data/abyss_trials'
    catalogue=json.loads((output/'abyss_trials.json').read_text(encoding='utf-8'))
    results=[]
    try:
        for trial in [r for r in catalogue['trials'] if r['id'] in (3,33,71)]:
            for category,tag in [('overall','tag1_recall'),('no_hit','tag1_recall_wushang'),('build','tag1_recall_liupai')]:
                tag2=trial['rank_tag2']
                if category=='build':
                    presets=trial.get('trial_plan_ids') or []
                    if not presets:continue
                    tag2+='_'+str(presets[0])
                name=f'DynamicRank___{tag}___{tag2}'
                response=await wwm._wwm_api_post(RANK_GET_RANKLIST_URL+name,{'uid':WWM_UID,'rank_name':name,'hostnum':10001,'pid':'','fields':['base','head'],'start':0,'end':20,'page':1,'params':{}},max_retries=0)
                result=(response or {}).get('result',{})
                rows=result.get('rank_list',[]) if isinstance(result,dict) else []
                item={'trial_id':trial['id'],'name':trial['name'],'category':category,'rank_name':name,'code':(response or {}).get('code'),'returned_rows':len(rows) if isinstance(rows,list) else 0,'reported_total':result.get('rank_total_len') if isinstance(result,dict) else None,
                    'result_keys':sorted(result) if isinstance(result,dict) else [],'sample_row_keys':sorted(rows[0]) if rows and isinstance(rows[0],dict) else [],'sample_score':rows[0].get('score') if rows and isinstance(rows[0],dict) else None,
                    'sample_ud_keys':sorted(rows[0]['ud']) if rows and isinstance(rows[0],dict) and isinstance(rows[0].get('ud'),dict) else []}
                item['status']='verified_populated' if item['code']==0 and item['returned_rows'] else 'accepted_empty' if item['code']==0 else 'failed'
                results.append(item);print(json.dumps(item,ensure_ascii=True),flush=True)
                await asyncio.sleep(.25)
    finally:await wwm.close_session()
    from datetime import datetime,timezone
    (output/'api_verification.json').write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'First page only, three selected bosses; score units unverified','results':results},indent=2,ensure_ascii=False),encoding='utf-8')

if __name__=='__main__':asyncio.run(main())
