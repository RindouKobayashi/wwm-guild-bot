"""Validate ranked-run joins using public board data; not full player history."""
import asyncio
import base64
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from probe_activity_details import BOT, structure, aiohttp, msgpack, get_default_headers, unpack_response
from settings import RANK_GET_RANKLIST_URL, WWM_CLUB_HOSTNUMS_URL, WWM_FULL_GUILD_URL, WWM_UID

async def main():
    logging.disable(logging.CRITICAL)
    records, index, board_summaries = [], defaultdict(list), []
    origin = 'https://' + urlsplit(RANK_GET_RANKLIST_URL).netloc
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        async def post(label, url, payload):
            if len(records) >= 40: raise RuntimeError('Request budget exceeded')
            data = None
            item = {'label':label,'path':urlsplit(url).path}
            try:
                async with session.post(url, data=msgpack.packb({'uid':WWM_UID,**payload}), headers=await get_default_headers(), allow_redirects=False) as response:
                    body = await response.read()
                    item.update(http_status=response.status,body_bytes=len(body))
                    try: data = unpack_response(body)
                    except Exception: item['non_msgpack']=True
                    item['response']=data
            except (aiohttp.ClientError,asyncio.TimeoutError,OSError) as exc: item['error_type']=type(exc).__name__
            records.append(item)
            print(json.dumps({k:v for k,v in item.items() if k!='response'}),flush=True)
            await asyncio.sleep(0.25)
            return data
        all_rows=[]
        for name in ['rank_team10_dungeon_22','rank_team10_dungeon_3']:
            data=await post(name,RANK_GET_RANKLIST_URL+name,{'rank_name':name,'hostnum':10001,'pid':'','fields':[],'start':0,'end':500})
            result=data.get('result',{}) if isinstance(data,dict) else {}
            rows=result.get('rank_list',[])
            if not isinstance(rows,list): rows=[]
            # The service caps each response at 25, even when end=500.
            total=min(int(result.get('rank_total_len',0)),500)
            while rows and len(rows)<total:
                start=len(rows)
                page=await post(name+f'_offset_{start}',RANK_GET_RANKLIST_URL+name,{'rank_name':name,'hostnum':10001,'pid':'','fields':[],'start':start,'end':min(start+25,total)})
                more=page.get('result',{}).get('rank_list',[]) if isinstance(page,dict) else []
                if not more or {r.get('pid') for r in more}.issubset({r.get('pid') for r in rows}): break
                rows.extend(more)
            decoded=[]
            for rank,row in enumerate(rows,1):
                ud=row.get('ud',{}); members=ud.get('members',[])
                entry={'board':name,'rank':rank,'score':row.get('score'),'timestamp':ud.get('ts'),'spaceids':ud.get('spaceids',[]),'team_key':row.get('pid'),'members':members}
                all_rows.append((row,entry))
                for pid in members: index[pid].append(entry)
                for sid in ud.get('spaceids',[]):
                    try:
                        raw=base64.b64decode(sid,validate=True)
                        if len(raw)==12 and isinstance(ud.get('ts'),(int,float)):decoded.append(ud['ts']-int.from_bytes(raw[:4],'big'))
                    except (ValueError,TypeError): pass
            summary={'board':name,'returned_rows':len(rows),'reported_total':result.get('rank_total_len'),'rows_with_spaceids':sum(bool(r.get('ud',{}).get('spaceids')) for r in rows),'spaceid_counts':sorted(set(len(r.get('ud',{}).get('spaceids',[])) for r in rows)),'team_key_matches_member_set':sum(set(r.get('pid','').split('_'))==set(r.get('ud',{}).get('members',[])) for r in rows),'possible_id_timestamp_delta_seconds':{'count':len(decoded),'min':min(decoded) if decoded else None,'max':max(decoded) if decoded else None}}
            board_summaries.append(summary);print(json.dumps(summary),flush=True)
        roster_summary={}
        if all_rows:
            row,entry=all_rows[0];ud=row['ud'];groups=defaultdict(list)
            for pid in ud['members']:
                host=ud.get(pid,{}).get('hostnum')
                if isinstance(host,int):groups[host].append(pid)
            profile=await post('ranked_roster_profiles',WWM_CLUB_HOSTNUMS_URL,{'hostnum2pids':dict(groups),'fields':['base','head','equipments','club','lunjian','dungeon']})
            players=profile.get('result',{}) if isinstance(profile,dict) else {}
            roster_summary={'requested_members':sum(map(len,groups.values())),'returned_members':sum(pid in players for pids in groups.values() for pid in pids),'available_property_groups':sorted(set(k for p in players.values() if isinstance(p,dict) for k in p))}
            leader=players.get(ud.get('leader_id'),{});club=leader.get('club',{})
            club_id=club.get('club_id') if isinstance(club,dict) else None
            if club_id:
                guild=await post('ranked_leader_guild_activity',WWM_FULL_GUILD_URL,{'club_id':club_id,'hostnum':club.get('hostnum',ud.get('hostnum')),'field_info':{'base':[],'play':[],'league_info':[],'pk_match_info':[]}})
                print(json.dumps({'guild_response_shape':structure(guild)}),flush=True)
                # The single-tag wrapper supports these exact fields; type 1 has no weekly suffix.
                play=await post('guild_play_history_single',origin+'/flk/club_service/get_play_history_data',{'club_id':club_id,'play_tag':'1','with_sub':False})
                print(json.dumps({'guild_play_response_shape':structure(play)}),flush=True)
                def collect_ids(v):
                    found=[]
                    if isinstance(v,dict):
                        for k,x in v.items():
                            if k in ('recent_space_idx','season_recent_space_idx') and isinstance(x,list):found.extend(s for s in x if isinstance(s,str))
                            else:found.extend(collect_ids(x))
                    elif isinstance(v,list):
                        for x in v:found.extend(collect_ids(x))
                    return found
                guild_ids=list(dict.fromkeys(collect_ids(guild)))[:3]
                if guild_ids:
                    reports=await post('guild_ids_combat_reports',origin+'/flk/club_combat/club_combat_read_history',{'space_ids':guild_ids})
                    print(json.dumps({'guild_report_response_shape':structure(reports)}),flush=True)
            print(json.dumps(roster_summary),flush=True)
        summary={'boards':board_summaries,'unique_players_in_sample':len(index),'players_with_multiple_ranked_entries':sum(len(v)>1 for v in index.values()),'roster':roster_summary,'scope':'Current board entries only; not all runs or all player history. ID timestamp interpretation is inferred.'}
    out=BOT/'maintenance/research/output'/('ranked-run-joins-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));out.mkdir(parents=True,exist_ok=True)
    (out/'private-responses.json').write_text(json.dumps(records,ensure_ascii=False,indent=2,default=lambda v:{'binary_bytes':len(v)} if isinstance(v,bytes) else str(v)),encoding='utf-8')
    (out/'private-player-run-index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary),flush=True);print(str(out),flush=True)

if __name__=='__main__':asyncio.run(main())
