"""Fetch public Abyss samples and the Lua-defined no-hit completion counter."""
import asyncio,json,sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from utility import wwm
from settings import WWM_UID,RANK_GET_RANKLIST_URL

async def main():
    out=Path(__file__).resolve().parents[3]/'WWM-Toolkit/output/game_data/abyss_trials'
    result={'boards':[],'counters':[]}
    origin='https://'+urlsplit(RANK_GET_RANKLIST_URL).netloc
    try:
        for boss in (4,34,72):
            for category,tag in [('overall','tag1_recall'),('no_hit','tag1_recall_wushang')]:
                name=f'DynamicRank___{tag}___recall_tag2_{boss}'
                payload={'uid':WWM_UID,'rank_name':name,'hostnum':10001,'pid':'','fields':['base'],'start':0,'end':20,'page':1,'params':{}}
                response=await wwm._wwm_api_post(RANK_GET_RANKLIST_URL+name,payload,max_retries=0)
                rows=(response or {}).get('result',{}).get('rank_list',[])
                result['boards'].append({'boss_id':boss,'category':category,'rank_name':name,'code':(response or {}).get('code'),'total':(response or {}).get('result',{}).get('rank_total_len'),'rows':rows})
                print(json.dumps({'boss':boss,'category':category,'rows':len(rows),'sample_ud':rows[0].get('ud') if rows else None},ensure_ascii=True),flush=True)
                if boss==4 and category=='overall' and rows:
                    own=await wwm._wwm_api_post(RANK_GET_RANKLIST_URL+name,{**payload,'pid':rows[0]['pid']},max_retries=0)
                    result['player_rank_lookup']={k:(own or {}).get('result',{}).get(k) for k in ('my_rank','my_data')}
                await asyncio.sleep(.25)
            counter=await wwm._wwm_api_post(origin+'/flk/kv_service/get_value_no_pack',{'uid':WWM_UID,'tag':'recall_wushang','key':f'10001_{boss}','default_value':0},max_retries=0)
            result['counters'].append({'boss_id':boss,'response':counter})
            print(json.dumps({'boss':boss,'counter':counter},default=str),flush=True)
    finally:await wwm.close_session()
    (out/'public_rank_samples.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=lambda v:{'bytes':len(v)} if isinstance(v,bytes) else str(v)),encoding='utf-8')

if __name__=='__main__':asyncio.run(main())
