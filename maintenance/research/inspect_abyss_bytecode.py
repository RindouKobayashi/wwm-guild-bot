"""Read original recall prototypes to check fields omitted by decompilation."""
import json,re,sqlite3,struct,sys
from pathlib import Path
from scan_activity_bytecode import ROOT,BOT,MpkRecord,read_physical_record,lz4
from lua54_activity_constants import inspect
from wwm_asset_lua import readable_strings

def main():
    game=Path(json.loads((ROOT/'WWM-Toolkit/config/settings.json').read_text())['game_root'])
    db=Path.home()/'Documents/WWM Asset Explorer/wwm_asset_catalog.sqlite'
    conn=sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True);conn.row_factory=sqlite3.Row
    scan_all='--all-logs' in sys.argv
    rows=[dict(r) for r in conn.execute('SELECT l.source_path,r.* FROM lua_scripts l JOIN records r USING(record_index)') if scan_all or re.search('recall|retrospect|battle_log|imp_dynamic_rank',r['source_path'])];conn.close()
    output=[]
    def walk(proto):
        yield proto
        for child in proto['children']:yield from walk(child)
    errors=[]
    for number,r in enumerate(rows,1):
        try:
            rec=MpkRecord(int(r['record_index']),int(r['file_id'],16),r['archive_offset'],r['physical_length'],r['location_code'],r['archive_path'] or '')
            raw=read_physical_record(game,rec);bc=lz4.block.decompress(raw[4:],uncompressed_size=struct.unpack_from('<I',raw)[0])
            if scan_all:
                if b'battle_log' in bc:
                    output.append({'source':r['source_path'],'strings':readable_strings(bc)})
                if number%5000==0:print('Scanned',number,'/',len(rows),flush=True)
                continue
            tree=inspect(bc)
            for p in walk(tree):
                text=str(p['constants'])+str(p['locals'])
                if re.search('battle_log|battle_skills|/flk/|slice',text):
                    item={'source':r['source_path'],'line':p['first_line'],'constants':p['constants'],'locals':p['locals']};output.append(item)
                    print(json.dumps(item,ensure_ascii=True),flush=True)
        except Exception as exc:
            errors.append({'source':r['source_path'],'error':str(exc)})
            if not scan_all:print(json.dumps(errors[-1]))
    name='abyss-battle-log-scan.json' if scan_all else 'abyss-bytecode-prototypes.json'
    (BOT/'maintenance/research/output'/name).write_text(json.dumps({'scanned':len(rows),'errors':errors,'hits':output},indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'scanned':len(rows),'errors':len(errors),'sources':[r['source'] for r in output]}),flush=True)

if __name__=='__main__':main()
