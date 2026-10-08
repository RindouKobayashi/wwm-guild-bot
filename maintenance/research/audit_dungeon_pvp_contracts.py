"""Focused original-bytecode audit of dungeon records and PvP history contracts."""
import json,re,sqlite3,struct
from pathlib import Path
from scan_activity_bytecode import ROOT,BOT,MpkRecord,read_physical_record,lz4
from lua54_activity_constants import inspect

def walk(p):
    yield p
    for child in p['children']:yield from walk(child)

def main():
    game=Path(json.loads((ROOT/'WWM-Toolkit/config/settings.json').read_text(encoding='utf-8'))['game_root'])
    db=Path.home()/'Documents/WWM Asset Explorer/wwm_asset_catalog.sqlite'
    conn=sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True);conn.row_factory=sqlite3.Row
    rows=[dict(r) for r in conn.execute('SELECT l.source_path,r.* FROM lua_scripts l JOIN records r USING(record_index)') if re.search(r'(property_define/(avatar|other_avatar|space_data)/(dungeon|lunjian|fight_shoulder|competition_lunjian)|imp_dungeon\.lua|dungeon_consts\.lua|fuben.*(stat|rank|jiesuan)|lunjian.*history|uwsgi_manager_members/imp_(pvp_battle|rank|watch|common_history)|imp_watch\.lua)',r['source_path'])];conn.close()
    hits=[];errors=[]
    for r in rows:
        try:
            rec=MpkRecord(int(r['record_index']),int(r['file_id'],16),r['archive_offset'],r['physical_length'],r['location_code'],r['archive_path'] or '')
            raw=read_physical_record(game,rec);bc=lz4.block.decompress(raw[4:],uncompressed_size=struct.unpack_from('<I',raw)[0])
            actual=re.search(rb'@([ -~]{1,1024}?\.lua)',bc)
            if not actual or actual.group(1).decode()!=r['source_path']:raise ValueError('Source path mismatch')
            for p in walk(inspect(bc)):
                text=str(p['constants'])+str(p['locals'])
                if re.search(r'multi_guard_rank_team_data|best_record|battle_history|combat_stat|space.?id|record_id|history|rank_record|share|/flk/|/rank_',text,re.I):
                    hits.append({'source':r['source_path'],'line':p['first_line'],'constants':p['constants'],'locals':p['locals']})
        except Exception as e:errors.append({'source':r['source_path'],'error':str(e)})
    out=BOT/'maintenance/research/output/dungeon-pvp-deep-contracts.json'
    out.write_text(json.dumps({'scanned':len(rows),'errors':errors,'hits':hits},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'scanned':len(rows),'errors':len(errors),'prototypes':len(hits),'output':str(out)}))
if __name__=='__main__':main()
