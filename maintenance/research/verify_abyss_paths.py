"""Read-only verification of boss-specific Path Trial ranking configurations."""
import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from utility import wwm
from settings import WWM_UID,RANK_GET_RANKLIST_URL
async def main():
    out=Path(__file__).resolve().parents[3]/'WWM-Toolkit/output/game_data/abyss_trials'
    trials=json.loads((out/'abyss_trials.json').read_text(encoding='utf-8'))['trials']
    names=json.loads((out/'loadout_name_tables.json').read_text(encoding='utf-8'))['preset']
    selected={}
    for trial in trials:
        for plan in trial.get('trial_plan_ids') or []:selected.setdefault(plan,trial)
    reports=[];receipts=[]
    try:
        for plan,trial in selected.items():
            name=f'DynamicRank___tag1_recall_liupai___{trial["rank_tag2"]}_{plan}'
            response=await wwm._wwm_api_post(RANK_GET_RANKLIST_URL+name,{'uid':WWM_UID,'rank_name':name,'hostnum':10001,'pid':'','fields':['base','head'],'start':0,'end':20,'page':1,'params':{}},max_retries=0)
            data=(response or {}).get('result') or {};rows=data.get('rank_list') or []
            config=names[str(plan)];raw=config['raw'];expected=[raw['kongfu_main'][0],raw['kongfu_sub'][0]]
            xinfa=raw.get('xinfa_tp_data') or [];inner=xinfa[xinfa.index('xinfa')+1] if 'xinfa' in xinfa else []
            skills=[(r.get('ud') or {}).get('battle_skills') or [] for r in rows]
            item={'trial_id':trial['id'],'boss':trial['name'],'path_id':plan,'path_name':config['name'],'rank_name':name,'code':(response or {}).get('code'),'total':data.get('rank_total_len'),'sampled_rows':len(rows),'expected_martial_pair':expected,'exact_martial_pair_matches':sum(s[:2]==expected for s in skills),'unordered_martial_pair_matches':sum(sorted(s[:2])==sorted(expected) for s in skills),'expected_inner_ways':inner,'exact_inner_way_matches':sum(s[3:7]==inner for s in skills),'distinct_mystic_loadouts':len({tuple(s[7:]) for s in skills}),'returned_tiaoping_ids':sorted({(r.get('ud') or {}).get('tiaoping_id') for r in rows},key=str)}
            reports.append(item);receipts.append({'path_id':plan,'response':response});print(json.dumps(item,ensure_ascii=True),flush=True)
    finally:await wwm.close_session()
    (out/'path_trial_verification.json').write_text(json.dumps({'scope':'One first-page board per nine extracted path IDs; group 10001. Not all bosses.','results':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'path_trial_rank_receipts.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':asyncio.run(main())
