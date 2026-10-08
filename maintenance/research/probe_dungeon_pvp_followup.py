"""Bounded read-only follow-up: current HR/ST schemas and public history properties."""
import asyncio,json,logging
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from probe_activity_details import BOT,aiohttp,msgpack,get_default_headers,unpack_response,structure,WWM_UID,WWM_API_URL,WWM_CLUB_HOSTNUMS_URL,RANK_GET_RANKLIST_URL
async def main():
    logging.disable(logging.CRITICAL);records=[]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async def post(label,url,payload):
            if len(records)>=10:raise RuntimeError('Request cap')
            record={'label':label,'path':urlsplit(url).path};data=None
            try:
                async with session.post(url,data=msgpack.packb({'uid':WWM_UID,**payload}),headers=await get_default_headers(),allow_redirects=False) as response:
                    raw=await response.read();record.update(http_status=response.status,bytes=len(raw))
                    try:data=unpack_response(raw)
                    except Exception:record['undecoded']=True
            except (aiohttp.ClientError,asyncio.TimeoutError,OSError) as exc:record['error']=type(exc).__name__
            record['response']=data;records.append(record)
            print(json.dumps({k:v for k,v in record.items() if k!='response'}|{'code':data.get('code') if isinstance(data,dict) else None}),flush=True)
            return data or {}
        manifest=json.loads((BOT/'data/ranking_assets/ranking-artwork-2026-10-08-revision-02/manifest.json').read_text(encoding='utf-8'))['boards']
        st_ids=[]
        for key in ['hr:22','hr:3','st:11','st:1']:
            if key not in manifest:continue
            name=manifest[key]['rank_name']
            board=await post(key,RANK_GET_RANKLIST_URL+name,{'fields':['base','head'],'pid':'','hostnum':10001,'fuzzy_info':{},'rank_name':name,'page':0,'params':{},'start':0,'end':3})
            if key == 'st:11':
                rows=(board.get('result') or {}).get('rank_list') or []
                if rows:st_ids=(rows[0].get('ud') or {}).get('spaceids',[])[:2]
        if st_ids:
            origin='https://'+urlsplit(RANK_GET_RANKLIST_URL).netloc
            await post('st_ids_pvp_report',origin+'/flk/pvp_battle_history/get_history',{'space_ids':st_ids})
            await post('st_ids_guild_report',origin+'/flk/club_combat/club_combat_read_history',{'space_ids':st_ids})
        identity=(await post('resolve_known_player',WWM_API_URL,{'number_id':'1028077590','force_search':False})).get('result') or {}
        if identity.get('id'):
            await post('public_owner_property_check',WWM_CLUB_HOSTNUMS_URL,{'hostnum2pids':{identity['hostnum']:[identity['id']]},'fields':['base','dungeon','lunjian','lunjian2v2_prop','lunjian3v3_prop','fight_shoulder','compe_lunjian']})
    out=BOT/'maintenance/research/output'/('dungeon-pvp-followup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));out.mkdir(parents=True,exist_ok=True)
    (out/'private-responses.json').write_text(json.dumps(records,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    summaries=[]
    for r in records:
        response=r.get('response') or {};result=response.get('result') or {};rows=result.get('rank_list') or []
        summaries.append({k:v for k,v in r.items() if k!='response'}|{'code':response.get('code'),'structure':structure(result),'total':result.get('rank_total_len'),'ud_keys':sorted({k for row in rows for k in (row.get('ud') or {})}),'sample_count':len(rows)})
    (out/'summary.json').write_text(json.dumps(summaries,indent=2),encoding='utf-8');print(str(out))
if __name__=='__main__':asyncio.run(main())
