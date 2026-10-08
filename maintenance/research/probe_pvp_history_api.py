"""Bounded read-only PvP API investigation; never starts the Discord bot."""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

BOT = next(p for p in Path(__file__).resolve().parents if (p / 'guildbot.py').is_file())
sys.path[:0] = [str(BOT), str(BOT / '.venv/Lib/site-packages')]
import aiohttp
import msgpack
from utility.wwm import get_default_headers
from settings import WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, WWM_UID

async def main():
    logging.disable(logging.CRITICAL)
    records = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def post(label, url, payload):
            try:
                async with session.post(url, data=msgpack.packb(payload), headers=await get_default_headers(), allow_redirects=False) as response:
                    raw = await response.read()
                    try:
                        data = msgpack.unpackb(raw, raw=False, strict_map_key=False)
                    except Exception:
                        data = {'undecoded_bytes': len(raw)}
                    record = {'label': label, 'http_status': response.status, 'response': data}
            except Exception as exc:
                record = {'label': label, 'error_type': type(exc).__name__}
                data = None
            records.append(record)
            print(json.dumps({k: v for k, v in record.items() if k != 'response'}), flush=True)
            return data

        space_ids = set()
        fields = ['base', 'lunjian', 'lunjian2v2_prop', 'lunjian3v3_prop', 'fight_shoulder']
        for index, number in enumerate(['1028077590', '4036668451'], 1):
            resolved = await post(f'player_{index}_resolve', WWM_API_URL, {'uid': WWM_UID, 'number_id': number, 'force_search': False})
            result = resolved.get('result', {}) if isinstance(resolved, dict) else {}
            pid = result.get('id') if isinstance(result, dict) else None
            if not pid:
                print(json.dumps({'player': index, 'resolution_code': resolved.get('code') if isinstance(resolved, dict) else None}), flush=True)
                continue
            host = result.get('hostnum', 10595)
            profile = await post(f'player_{index}_profile', WWM_CLUB_HOSTNUMS_URL, {'uid': WWM_UID, 'hostnum2pids': {host: [pid]}, 'fields': fields})
            players = profile.get('result', {}) if isinstance(profile, dict) else {}
            player = players.get(pid, {}) if isinstance(players, dict) else {}
            summary = {'player': index, 'code': profile.get('code') if isinstance(profile, dict) else None, 'returned_fields': list(player) if isinstance(player, dict) else []}
            summary['pvp_fields'] = {key: list(player[key]) for key in fields[1:] if isinstance(player.get(key), dict)}
            print(json.dumps(summary), flush=True)
            def collect(value):
                if isinstance(value, dict):
                    if isinstance(value.get('space_id'), str):
                        space_ids.add(value['space_id'])
                    for child in value.values(): collect(child)
                elif isinstance(value, list):
                    for child in value: collect(child)
            for key in fields[1:]: collect(player.get(key))
            if index == 1:
                explicit = await post('explicit_history_fields', WWM_CLUB_HOSTNUMS_URL, {'uid': WWM_UID, 'hostnum2pids': {host: [pid]}, 'fields': ['battle_history', 'lunjian.battle_history', 'lunjian2v2_prop.battle_history', 'lunjian3v3_prop.battle_history', 'fight_shoulder.battle_history']})
                explicit_players = explicit.get('result', {}) if isinstance(explicit, dict) else {}
                explicit_player = explicit_players.get(pid, {}) if isinstance(explicit_players, dict) else {}
                print(json.dumps({'explicit_history_code': explicit.get('code') if isinstance(explicit, dict) else None, 'returned_fields': list(explicit_player) if isinstance(explicit_player, dict) else []}), flush=True)
                collect(explicit_player)
        # Existing repository probe ID; age/retention are unknown.
        ids = sorted(space_ids)[:5] or ['aqKCUGb8H/JIVPn0']
        detail = await post('pvp_get_history', 'https://h72naxx2gb-ms-prod.easebar.com/flk/pvp_battle_history/get_history', {'space_ids': ids, 'uid': WWM_UID})
        print(json.dumps({'history_id_source': 'public_profile' if space_ids else 'existing_repository_probe', 'detail_code': detail.get('code') if isinstance(detail, dict) else None, 'detail_result': detail.get('result') if isinstance(detail, dict) else None}, default=str), flush=True)
    output = BOT / 'maintenance/research/output'
    output.mkdir(parents=True, exist_ok=True)
    path = output / ('pvp-history-api-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('Private research responses saved to ' + str(path), flush=True)

if __name__ == '__main__':
    asyncio.run(main())
