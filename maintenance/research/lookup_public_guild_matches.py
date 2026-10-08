"""Player number -> public guild -> recent battle IDs -> participant reports.

Research command, not a Discord command. Never reads RAM. Coverage is limited
to the character's current guild and the selected returned battle IDs.
"""
import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from probe_activity_details import BOT, aiohttp, msgpack, get_default_headers, unpack_response
from settings import WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, WWM_FULL_GUILD_URL, WWM_UID, RANK_GET_RANKLIST_URL

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('player_number',nargs='?',help='A known player number; prompts if omitted')
    parser.add_argument('--limit',type=int,default=3,choices=range(1,41))
    args=parser.parse_args();logging.disable(logging.CRITICAL)
    if args.player_number is None:args.player_number=input('Player number: ').strip()
    if not args.player_number.isdigit():parser.error('Expected a numeric player number')
    out=BOT/'maintenance/research/output'/('public-guild-lookup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));out.mkdir(parents=True,exist_ok=True)
    receipts=[]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def post(label,url,payload):
            async with session.post(url,data=msgpack.packb({'uid':WWM_UID,**payload}),headers=await get_default_headers(),allow_redirects=False) as response:
                body=await response.read();(out/(label+'.msgpack')).write_bytes(body)
                receipt={'step':label,'http_status':response.status,'bytes':len(body)};receipts.append(receipt)
                print(json.dumps(receipt),flush=True)
                if response.status!=200:return {}
                data=unpack_response(body)
                return data if isinstance(data,dict) and data.get('code')==0 else {}
        identity=(await post('resolve',WWM_API_URL,{'number_id':args.player_number,'force_search':False})).get('result',{})
        pid=identity.get('id');host=identity.get('hostnum')
        summary={'scope':'Current public guild battle records only; not personal arena history or all guild memberships.','reports_returned':0,'matches_containing_requested_player':0}
        if pid and host:
            profiles=(await post('profile',WWM_CLUB_HOSTNUMS_URL,{'hostnum2pids':{host:[pid]},'fields':['club']})).get('result',{})
            club=profiles.get(pid,{}).get('club',{})
            if club.get('club_id'):
                guild=(await post('guild',WWM_FULL_GUILD_URL,{'club_id':club['club_id'],'hostnum':club.get('hostnum',host),'field_info':{'base':[],'play':[],'league_info':[],'pk_match_info':[]}})).get('result',{})
                ids=[]
                def collect(value):
                    if isinstance(value,dict):
                        for key,child in value.items():
                            if key in ('recent_space_idx','season_recent_space_idx') and isinstance(child,list):ids.extend(x for x in child if isinstance(x,str))
                            else:collect(child)
                    elif isinstance(value,list):
                        for child in value:collect(child)
                collect(guild)
                ids=list(dict.fromkeys(ids))
                # ObjectId-like prefix is a sorting hint, not a verified match time.
                import base64
                def hint(s):
                    try:return int.from_bytes(base64.b64decode(s,validate=True)[:4],'big')
                    except ValueError:return 0
                selected=sorted(ids,key=hint,reverse=True)[:args.limit]
                summary['available_guild_ids']=len(ids);summary['requested_report_ids']=len(selected)
                if selected:
                    reports=[]
                    for offset in range(0,len(selected),10):
                        response=await post(f'reports_{offset}','https://'+urlsplit(RANK_GET_RANKLIST_URL).netloc+'/flk/club_combat/club_combat_read_history',{'space_ids':selected[offset:offset+10]})
                        reports.extend(response.get('result',{}).get('histories') or [])
                        await asyncio.sleep(0.25)
                    normalized=[]
                    for report in reports:
                        stats=json.loads(report.get('StatisticsJsonStr','{}'))
                        own=stats.get(pid)
                        normalized.append({'space_id':report.get('SpaceId'),'timestamp':report.get('TimeStamp'),'requested_player_present':pid in stats,'requested_player_statistics':own,'participant_count':len(stats),'red_win':report.get('IsRedWin'),'blue_win':report.get('IsBlueWin')})
                    summary['reports_returned']=len(normalized);summary['matches_containing_requested_player']=sum(x['requested_player_present'] for x in normalized)
                    (out/'private-player-reports.json').write_text(json.dumps(normalized,ensure_ascii=False,indent=2),encoding='utf-8')
    summary['receipts']=receipts
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');print(json.dumps(summary),flush=True);print(str(out),flush=True)

if __name__=='__main__':asyncio.run(main())
