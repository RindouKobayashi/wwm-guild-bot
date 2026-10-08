"""Bounded read-only activity API research. Does not import/start Discord cogs.

Only repository-known players, three named boards and IDs returned by those
requests are used. Raw responses remain in the ignored research output folder.
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

BOT = next(p for p in Path(__file__).resolve().parents if (p / 'guildbot.py').is_file())
sys.path[:0] = [str(BOT), str(BOT / '.venv/Lib/site-packages')]
import aiohttp
import msgpack
from utility.wwm import get_default_headers, _convert_bytes_to_str
from settings import WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, WWM_UID, RANK_GET_RANKLIST_URL

def structure(value, depth=0):
    if isinstance(value, bytes): return {'bytes': len(value)}
    if depth > 5: return type(value).__name__
    if isinstance(value, dict):
        return {(str(k) if str(k).replace('_', '').isalnum() and str(k).islower() and len(str(k)) < 60 else '<identifier>'): structure(v, depth+1) for k,v in value.items()}
    if isinstance(value, list): return {'count': len(value), 'first': structure(value[0], depth+1) if value else None}
    return type(value).__name__

def unpack_response(body):
    try: return msgpack.unpackb(body, raw=False, strict_map_key=False)
    except UnicodeDecodeError:
        return _convert_bytes_to_str(msgpack.unpackb(body, raw=True, strict_map_key=False))

async def main():
    logging.disable(logging.CRITICAL)
    records = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async def post(label, url, payload):
            if len(records) >= 16: raise RuntimeError('Request budget exceeded')
            record = {'label': label, 'path': urlsplit(url).path}
            data = None
            try:
                async with session.post(url, data=msgpack.packb({'uid': WWM_UID, **payload}), headers=await get_default_headers(), allow_redirects=False) as response:
                    body = await response.read()
                    record.update(http_status=response.status, body_bytes=len(body))
                    try: data = unpack_response(body)
                    except Exception: record['non_msgpack'] = True
                    record['response'] = data
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                record['error_type'] = type(exc).__name__
            records.append(record)
            print(json.dumps({k:v for k,v in record.items() if k != 'response'}), flush=True)
            if isinstance(data, dict):
                print(json.dumps({'label': label, 'code': data.get('code'), 'structure': structure(data.get('result', data))}), flush=True)
            await asyncio.sleep(0.25)
            return data

        origin = 'https://' + urlsplit(RANK_GET_RANKLIST_URL).netloc
        boards = {}
        for name in ['rank_team10_dungeon_22', 'rank_team_dungeon_22_st', 'rank_team10_dungeon_3']:
            response = await post(name, RANK_GET_RANKLIST_URL + name, {'fields':['base','head'], 'pid':'', 'hostnum':10001, 'fuzzy_info':{}, 'rank_name':name, 'page':0, 'params':{}, 'start':0, 'end':3})
            rows = response.get('result', {}).get('rank_list', []) if isinstance(response, dict) else []
            if isinstance(rows, list): boards[name] = rows
        first_rows = next((r for r in boards.values() if r), [])
        ids = first_rows[0].get('ud', {}).get('spaceids', [])[:2] if first_rows else []
        if ids:
            await post('dungeon_ids_club_report', origin + '/flk/club_combat/club_combat_read_history', {'space_ids':ids})
        # The actual ranking row's key can retrieve that same entry through my_data.
        if first_rows:
            first = first_rows[0]
            name = next(n for n,r in boards.items() if r is first_rows)
            await post('rank_entry_by_team_key', RANK_GET_RANKLIST_URL + name, {'fields':['base','head'], 'pid':first['pid'], 'hostnum':10001, 'fuzzy_info':{}, 'rank_name':name, 'page':0, 'params':{}, 'start':0, 'end':1})
        resolved = await post('known_player_resolution', WWM_API_URL, {'number_id':'1028077590','force_search':False})
        identity = resolved.get('result', {}) if isinstance(resolved, dict) else {}
        if identity.get('id'):
            pid, host = identity['id'], identity.get('hostnum',10595)
            profile = await post('public_activity_properties', WWM_CLUB_HOSTNUMS_URL, {'hostnum2pids':{host:[pid]}, 'fields':['dungeon','club','compe_lunjian','lunjian','lunjian2v2_prop','lunjian3v3_prop','fight_shoulder']})
            player = profile.get('result', {}).get(pid, {}) if isinstance(profile, dict) else {}
            club = player.get('club', {})
            club_id = club.get('club_id') if isinstance(club, dict) else None
            if club_id:
                await post('club_play_history_batch', origin + '/flk/club_service/get_play_history_data_batch', {'club_id':club_id,'play_tags':['7','13','17']})
                await post('club_history', origin + '/flk/club_service/get_club_history', {'club_id':club_id,'hostnum':host,'start_ts':int(datetime.now(timezone.utc).timestamp())})
    out = BOT / 'maintenance/research/output' / ('activity-api-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    out.mkdir(parents=True, exist_ok=True)
    (out/'private-responses.json').write_text(json.dumps(records, ensure_ascii=False, indent=2, default=lambda v:{'bytes':len(v)} if isinstance(v,bytes) else str(v)), encoding='utf-8')
    public = [{k:v for k,v in r.items() if k != 'response'} | {'code':r.get('response',{}).get('code') if isinstance(r.get('response'),dict) else None, 'structure':structure(r.get('response'))} for r in records]
    (out/'response-structures.json').write_text(json.dumps(public, indent=2), encoding='utf-8')
    print(f'Saved {len(records)} request results to {out}', flush=True)

if __name__ == '__main__': asyncio.run(main())
