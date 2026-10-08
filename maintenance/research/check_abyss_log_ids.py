"""Check public Abyss log IDs against two previously verified report readers."""
import asyncio,json,sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from utility import wwm
from settings import WWM_UID,RANK_GET_RANKLIST_URL

async def main():
    output=Path(__file__).resolve().parents[3]/'WWM-Toolkit/output/game_data/abyss_trials'
    samples=json.loads((output/'public_rank_samples.json').read_text(encoding='utf-8'))
    ids=list(dict.fromkeys(b['rows'][0]['ud']['battle_log_id'] for b in samples['boards'] if b['rows']))[:3]
    origin='https://'+urlsplit(RANK_GET_RANKLIST_URL).netloc;results=[]
    try:
        for path in ('/flk/pvp_battle_history/get_history','/flk/club_combat/club_combat_read_history'):
            response=await wwm._wwm_api_post(origin+path,{'uid':WWM_UID,'space_ids':ids},max_retries=0)
            result=(response or {}).get('result',{})
            item={'endpoint':path,'public_id_count':len(ids),'code':(response or {}).get('code'),'result':result}
            results.append(item);print(json.dumps(item,default=str),flush=True)
    finally:await wwm.close_session()
    (output/'battle_log_checks.json').write_text(json.dumps(results,indent=2,default=str),encoding='utf-8')

if __name__=='__main__':asyncio.run(main())
