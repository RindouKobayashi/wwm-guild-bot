"""Inspect original packaged Lua constants, bypassing incomplete decompilation.

Reads game archives only; writes evidence into this project's research output.
"""
import argparse
import json
import re
import struct
import sqlite3
import sys
from pathlib import Path

BOT = next(p for p in Path(__file__).resolve().parents if (p / 'guildbot.py').is_file())
ROOT = BOT.parent
EXPLORER = ROOT / 'WWM_Table_Browser_v1.0.0 - Copy/WWM_Asset_Explorer_v3.1.0'
sys.path[:0] = [str(EXPLORER), str(BOT / '.venv/Lib/site-packages')]
from wwm_asset_core import MpkRecord, read_physical_record
from wwm_asset_lua import decode_wrapped_lua, wrapped_lua_header, readable_strings
import lz4.block

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true', help='Search every catalogued Lua chunk')
    args = parser.parse_args()
    game = Path(json.loads((ROOT / 'WWM-Toolkit/config/settings.json').read_text(encoding='utf-8'))['game_root'])
    catalog = Path.home() / 'Documents/WWM Asset Explorer/wwm_asset_catalog.sqlite'
    conn = sqlite3.connect(catalog.as_uri() + '?mode=ro&immutable=1', uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT l.source_path,r.* FROM lua_scripts l JOIN records r USING(record_index)')]
    conn.close()
    selected = [r for r in rows if args.all or re.search(r'dungeon|fuben|lunjian|pvp|rank/|imp_rank|uwsgi|imp_watch|recorder|combat_stat|spectator', r['source_path'], re.I)]
    hits, errors, mismatches = [], [], []
    targets = re.compile(r'space.?ids?|record_id|unique_key|history|statistic|/flk/|/rank_|dungeon_rank|time_cost', re.I)
    if True:
        for n, row in enumerate(selected, 1):
            try:
                rid = int(row['record_index'])
                record = MpkRecord(rid, int(row['file_id'], 16), row['archive_offset'], row['physical_length'], row['location_code'], row['archive_path'] or '')
                raw = read_physical_record(game, record)
                bytecode = lz4.block.decompress(raw[4:], uncompressed_size=struct.unpack_from('<I', raw)[0])
                source_match = re.search(rb'@([ -~]{1,1024}?\.lua)', bytecode)
                actual = source_match.group(1).decode('ascii') if source_match else ''
                if actual != row['source_path']:
                    mismatches.append({'expected': row['source_path'], 'actual': actual, 'record': rid})
                    continue
                strings = [s for s in readable_strings(bytecode) if targets.search(s)]
                if strings and (not args.all or any(key in bytecode for key in [b'spaceids', b'fetch_record', b'play_history_data', b'get_dungeon_rank_record_data', b'dungeon_combat_stat', b'battle_history', b'record_id']) or any('/' in s and re.search('history|statistic|recorder|dungeon', s) and '.lua' not in s for s in strings)):
                    hits.append({'source': actual, 'record': rid, 'strings': strings, 'literal_spaceids': b'spaceids' in bytecode})
            except Exception as exc:
                errors.append({'source': row['source_path'], 'error': type(exc).__name__ + ': ' + str(exc)[:180]})
            if n % (5000 if args.all else 200) == 0:
                print(f'Inspected {n}/{len(selected)} original Lua chunks', flush=True)
    report = {'selected': len(selected), 'matched_paths': len(selected)-len(errors)-len(mismatches), 'errors': errors, 'path_mismatches': mismatches, 'hits': hits}
    out = BOT / ('maintenance/research/output/activity-bytecode-all.json' if args.all else 'maintenance/research/output/activity-bytecode-constants.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'selected': len(selected), 'errors': len(errors), 'path_mismatches': len(mismatches), 'spaceids_sources': [h['source'] for h in hits if h['literal_spaceids']], 'output': str(out)}), flush=True)

if __name__ == '__main__':
    main()
