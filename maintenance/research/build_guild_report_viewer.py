"""Build an offline guild battle viewer; optionally refresh public name mappings."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BOT / '.venv/Lib/site-packages'))
import msgpack

def unpack(path):
    def convert(x):
        if isinstance(x, bytes):
            return x.decode('utf-8', 'replace')
        if isinstance(x, dict): return {convert(k): convert(v) for k,v in x.items()}
        if isinstance(x, list): return [convert(v) for v in x]
        return x
    return convert(msgpack.unpackb(path.read_bytes(), raw=True, strict_map_key=False))

async def refresh(reports, cache):
    from probe_activity_details import aiohttp, get_default_headers, unpack_response
    from settings import WWM_UID, WWM_CLUB_HOSTNUMS_URL, WWM_CLUB_BRIEF_INFO_BATCH_URL
    people = sorted({(s['hostnum'], pid) for r in reports for pid,s in r['stats'].items() if s.get('hostnum')})
    guilds = sorted({(r[c+'ClubHostNum'], r[c+'ClubId']) for r in reports for c in ('Red','Blue') if r.get(c+'ClubId')})
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async def post(url, payload):
            async with session.post(url, data=msgpack.packb({'uid':WWM_UID, **payload}), headers=await get_default_headers(), allow_redirects=False) as res:
                if res.status != 200: raise RuntimeError('Metadata HTTP status '+str(res.status))
                data=unpack_response(await res.read())
                if data.get('code') != 0: raise RuntimeError('Metadata API did not return success')
                return data.get('result', {})
        for offset in range(0,len(people),40):
            groups={}
            for host,pid in people[offset:offset+40]: groups.setdefault(host,[]).append(pid)
            data=await post(WWM_CLUB_HOSTNUMS_URL, {'hostnum2pids':groups,'fields':['base']})
            for pid,p in data.items():
                b=p.get('base',{})
                if b.get('nickname'): cache['players'][pid]={'name':b['nickname'],'number':b.get('number_id')}
            print('Mapped player batch',offset//40+1,flush=True)
            await asyncio.sleep(.25)
        for offset in range(0,len(guilds),20):
            data=await post(WWM_CLUB_BRIEF_INFO_BATCH_URL, {'club_list':[{'club_id':cid,'hostnum':host} for host,cid in guilds[offset:offset+20]]})
            for g in data.get('data',[]):
                cid=g.get('club_id') or g.get('id')
                if cid and g.get('base',{}).get('name'): cache['guilds'][cid]=g['base']['name']
    return len(people),len(guilds)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--refresh-labels',action='store_true')
    args=parser.parse_args()
    source=args.source or sorted((BOT/'maintenance/research/output').glob('public-guild-lookup-*'))[-1]
    out=BOT/'maintenance/research/output/guild-battle-viewer';out.mkdir(parents=True,exist_ok=True)
    identity=unpack(source/'resolve.msgpack')['result']
    reports=[]
    for p in source.glob('reports_*.msgpack'):
        for raw in unpack(p).get('result',{}).get('histories',[]):
            r={k:v for k,v in raw.items() if k not in ('StatisticsJsonStr','BattleInfoJsonStr')}
            r['stats']=json.loads(raw.get('StatisticsJsonStr') or '{}');reports.append(r)
    reports.sort(key=lambda r:r.get('TimeStamp',0),reverse=True)
    cachepath=out/'name-mappings.json'
    cache=json.loads(cachepath.read_text(encoding='utf-8')) if cachepath.exists() else {'players':{},'guilds':{}}
    cache['players'][identity['id']]={'name':identity['base']['nickname'],'number':identity['base'].get('number_id')}
    if args.refresh_labels: asyncio.run(refresh(reports,cache))
    cachepath.write_text(json.dumps(cache,ensure_ascii=False,indent=2),encoding='utf-8')
    data={'reports':reports,'own':identity['id'],'names':cache,'source':source.name}
    template=(Path(__file__).with_name('guild_report_viewer.html')).read_text(encoding='utf-8')
    payload=json.dumps(data,ensure_ascii=True).replace('<','\\u003c')
    (out/'index.html').write_text(template.replace('__REPORT_DATA__',payload),encoding='utf-8')
    ids={pid for r in reports for pid in r['stats']}
    print(json.dumps({'reports':len(reports),'unique_players':len(ids),'named_players':len(ids & cache['players'].keys()),'named_guilds':len(cache['guilds']),'html':str(out/'index.html')}))

if __name__=='__main__': main()
