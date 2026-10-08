"""Normalize saved API reports and verify damage breakdown sums, offline."""
import argparse
import json
import math
from pathlib import Path

BOT=next(p for p in Path(__file__).resolve().parents if (p/'guildbot.py').is_file())

def pairs(value):
    if not isinstance(value,dict):return []
    return [r for r in value.get('__',[]) if isinstance(r,list) and len(r)==2 and type(r[0])==int and type(r[1]) in (int,float)]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('response',type=Path);args=parser.parse_args()
    response=json.loads(args.response.read_text(encoding='utf-8'))
    reports=response['result']['result']
    labels=json.loads((BOT.parent/'WWM-Toolkit/output/companion/live-labels.json').read_text(encoding='utf-8')).get('skills',{})
    normalized=[];ids=set();checks=[]
    for report in reports:
        players=[]
        for side in ['win','lose']:
            for player in report.get(side,[]):
                pid=player['pid'];stats=report.get('statistic',{}).get(pid,{})
                skills={}
                for key,value in pairs(stats.get('own_skill_counter')):skills.setdefault(key,{})['uses']=value;ids.add(key)
                for detail in stats.get('combat_stat_detail',{}).values():
                    for field in ['damage','absorb_dmg']:
                        for key,value in pairs(detail.get(field)):
                            skill=skills.setdefault(key,{});skill[field]=skill.get(field,0)+value;ids.add(key)
                totals=stats.get('combat_stat',{})
                for field in ['damage','absorb_dmg']:
                    total=totals.get(field);summed=sum(s.get(field,0) for s in skills.values())
                    if type(total) in (int,float):checks.append({'field':field,'matches':math.isclose(total,summed,rel_tol=1e-7,abs_tol=0.01),'difference':summed-total})
                skill_rows=[{'id':key,'name':labels.get(str(key),{}).get('name'),**value} for key,value in skills.items()]
                skill_rows.sort(key=lambda x:x.get('damage',0),reverse=True)
                players.append({'pid':pid,'nickname':player.get('nickname'),'hostnum':player.get('hostnum'),'side':side,'old_score':player.get('old_score'),'new_score':player.get('new_score'),'head_no':player.get('head_no'),'main_kongfu':player.get('main_kongfu'),'sub_kongfu':player.get('sub_kongfu'),'totals':totals,'skills':skill_rows})
        normalized.append({'space_id':report['space_id'],'timestamp':report.get('ts'),'duration_seconds':report.get('time_cost'),'sid':report.get('sid'),'record_id':report.get('record_id'),'players':players})
    summary={'reports':len(normalized),'participant_records':sum(len(r['players']) for r in normalized),'nonempty_replay_keys':sum(bool(r.get('record_id')) for r in normalized),'breakdown_checks':len(checks),'breakdown_checks_passed':sum(c['matches'] for c in checks),'distinct_counter_and_damage_ids':len(ids),'named_ids':sum(bool(labels.get(str(i),{}).get('name')) for i in ids),'unmapped_ids':sorted(i for i in ids if not labels.get(str(i),{}).get('name')),'scope':'Known-ID API retrieval validated. IDs originated in an explicitly approved saved capture; no public personal-history listing established.'}
    out=args.response.parent
    (out/'private-normalized-pvp-reports.json').write_text(json.dumps(normalized,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'pvp-validation-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary),flush=True)
    if any(not c['matches'] for c in checks):raise SystemExit('Some breakdowns differ; retain reported totals and flag incomplete breakdowns')

if __name__=='__main__':main()
