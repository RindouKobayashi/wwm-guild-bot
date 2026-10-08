"""Test known report IDs; saved PvP capture is optional, never reads live RAM."""
import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from probe_activity_details import BOT, aiohttp, msgpack, get_default_headers, unpack_response, structure
from settings import WWM_UID, RANK_GET_RANKLIST_URL

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('guild_responses',type=Path)
    parser.add_argument('--saved-pvp-history',type=Path)
    args=parser.parse_args();logging.disable(logging.CRITICAL)
    records=json.loads(args.guild_responses.read_text(encoding='utf-8'))
    reports=next(r['response']['result']['histories'] for r in records if r['label']=='guild_ids_combat_reports')
    jobs=[('guild_reports','/flk/club_combat/club_combat_read_history',[r['SpaceId'] for r in reports[:3]],'public_guild_api')]
    if args.saved_pvp_history:
        saved=json.loads(args.saved_pvp_history.read_text(encoding='utf-8'))
        ids=[r['scalar_values'][5] for r in saved['rows'] if len(r.get('scalar_values',[]))==7 and isinstance(r['scalar_values'][5],str)]
        jobs.append(('saved_pvp_ids','/flk/pvp_battle_history/get_history',list(dict.fromkeys(ids))[:10],'previously_saved_capture_not_public_player_lookup'))
    out=BOT/'maintenance/research/output'/('known-report-ids-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));out.mkdir(parents=True,exist_ok=True)
    summaries=[]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        for label,path,ids,source in jobs:
            async with session.post('https://'+urlsplit(RANK_GET_RANKLIST_URL).netloc+path,data=msgpack.packb({'uid':WWM_UID,'space_ids':ids}),headers=await get_default_headers(),allow_redirects=False) as response:
                body=await response.read();(out/(label+'.msgpack')).write_bytes(body)
                data=unpack_response(body)
                summary={'label':label,'id_source':source,'id_count':len(ids),'http_status':response.status,'code':data.get('code') if isinstance(data,dict) else None,'structure':structure(data.get('result')) if isinstance(data,dict) else structure(data)}
                summaries.append(summary);print(json.dumps(summary),flush=True)
                (out/(label+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=lambda v:{'binary_bytes':len(v)} if isinstance(v,bytes) else str(v)),encoding='utf-8')
    (out/'summary.json').write_text(json.dumps(summaries,indent=2),encoding='utf-8');print(str(out),flush=True)

if __name__=='__main__':asyncio.run(main())
