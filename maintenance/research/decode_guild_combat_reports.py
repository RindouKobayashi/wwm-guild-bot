"""Decode saved API reports and build a participant index; no network/RAM."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

BOT=next(p for p in Path(__file__).resolve().parents if (p/'guildbot.py').is_file())

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('responses',type=Path)
    args=parser.parse_args()
    responses=json.loads(args.responses.read_text(encoding='utf-8'))
    raw=next(r['response']['result']['histories'] for r in responses if r['label']=='guild_ids_combat_reports')
    reports=[];player_index=defaultdict(list);summaries=[]
    for record in raw:
        stats=json.loads(record['StatisticsJsonStr'])
        if not isinstance(stats,dict):raise ValueError('Expected player-keyed statistics')
        normalized={'space_id':record['SpaceId'],'timestamp':record['TimeStamp'],'season':record['SeasonNo'],'round':record['CurRound'],'red_club':record['RedClubId'],'blue_club':record['BlueClubId'],'red_win':record['IsRedWin'],'blue_win':record['IsBlueWin'],'rating_changes':{'red':[record['RedOldScore'],record['RedNewScore']],'blue':[record['BlueOldScore'],record['BlueNewScore']]},'players':stats}
        reports.append(normalized)
        fields=Counter(); camps=Counter(); totals=defaultdict(float)
        for pid,data in stats.items():
            if not isinstance(data,dict):continue
            fields.update(data.keys());camps[str(data.get('camp'))]+=1
            for key in ['total_damage','damage_taken','healing_done','kills','deaths','assists','damage_wall','damage_goose']:
                value=data.get(key)
                if type(value) in (int,float):totals[key]+=value
            player_index[pid].append({'space_id':record['SpaceId'],'timestamp':record['TimeStamp'],'statistics':data})
        summaries.append({'timestamp':record['TimeStamp'],'participant_records':len(stats),'camp_counts':dict(camps),'field_presence_counts':dict(fields),'sum_of_returned_metrics':dict(totals),'red_win':record['IsRedWin'],'blue_win':record['IsBlueWin']})
    out=args.responses.parent
    (out/'private-normalized-guild-reports.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'private-guild-player-match-index.json').write_text(json.dumps(player_index,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={'report_count':len(reports),'unique_participants':len(player_index),'participants_in_multiple_reports':sum(len(v)>1 for v in player_index.values()),'reports':summaries,'coverage':'Only returned guild reports. Missing metrics are not proof of zero. No skill/affix breakdown established.'}
    (out/'guild-report-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
