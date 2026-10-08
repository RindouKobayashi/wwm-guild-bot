"""Copy original art and normalize existing public research snapshots. No requests."""
import ast,json,shutil,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
BOT=HERE.parent
sys.path.insert(0,str(BOT))
from utility.dungeon_assets import DungeonAssets
from utility.guild_battles import normalize_reports

def main():
    public=HERE/'public';(public/'art').mkdir(parents=True,exist_ok=True)
    source=BOT.parent/'WWM-Toolkit/output/game_data/abyss_trials'
    data=json.loads((BOT/'data/abyss_assets/catalogue.json').read_text(encoding='utf-8'))
    weapons={}
    for node in ast.parse((BOT/'utility/api_constants.py').read_text(encoding='utf-8')).body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='KONGFU_WEAPON_MAP' for t in node.targets):
            weapons={str(k):v['name'] for k,v in ast.literal_eval(node.value).items()}
    data['names']['martial']={**weapons,**data['names'].get('martial',{})}
    boards=[]
    for entry in data['trials']:
        entry=dict(entry);entry.update(key=f"abyss:{entry['id']}",family='abyss',title=entry['name'])
        if entry.get('image'):
            shutil.copy2(BOT/'data/abyss_assets'/entry['image'],public/'art'/f"abyss-{entry['id']}.png")
            entry['image']=f"/art/abyss-{entry['id']}.png"
        boards.append(entry)
    dungeon=DungeonAssets(BOT/'data/ranking_assets')
    for entry in dungeon.entries():
        art=dungeon.get(entry['rank_type'],entry['dungeon_id']);key=f"{entry['rank_type']}:{entry['dungeon_id']}"
        item={'key':key,'family':entry['rank_type'],'title':entry['name'],'rank_name':art['rank_name']}
        if art.get('path'):
            filename=key.replace(':','-')+'.png';shutil.copy2(art['path'],public/'art'/filename);item['image']='/art/'+filename
        boards.append(item)
    ranks={}
    for b in json.loads((source/'public_rank_samples.json').read_text(encoding='utf-8'))['boards']:
        ranks[b['rank_name']]={'rank_list':b['rows'],'rank_total_len':b['total']}
    for b in json.loads((source/'path_trial_rank_receipts.json').read_text(encoding='utf-8')):
        result=b['response']['result'];ranks[result['rank_name']]=result
    rawpath=sorted((BOT/'maintenance/research/output').glob('dungeon-pvp-followup-*/private-responses.json'))[-1]
    for r in json.loads(rawpath.read_text(encoding='utf-8')):
        result=(r.get('response') or {}).get('result') or {}
        if result.get('rank_list'):ranks[result['rank_name']]=result
    viewer=BOT/'maintenance/research/output/guild-battle-viewer/index.html'
    html=viewer.read_text(encoding='utf-8');start=html.index('const DATA=') if 'const DATA=' in html else -1
    # Decode the JSON literal, without executing the research viewer's JavaScript.
    if start<0:
        import re
        match=re.search(r'(?:const|let|var)\s+DATA\s*=\s*',html)
        if not match:raise ValueError('Viewer data not found')
        start=match.end()
    else:start+=len('const DATA=')
    guild,_=json.JSONDecoder().raw_decode(html[start:].lstrip())
    raw=[]
    for report in guild['reports']:
        raw.append({**report,'StatisticsJsonStr':report['stats']})
    matches,_=normalize_reports(raw,guild['own'])
    labels=guild['names']['players']
    for m in matches:m['player_names']=labels
    snapshot={'ranks':ranks,'guild':{'status':'ok','matches':matches,'names':guild['names']['guilds'],'scanned':len(raw)},'own':guild['own'],'players':labels,'captured':'2026-10-08','scope':'Saved public research snapshots; not live data or personal Arena history.'}
    (public/'catalogue.json').write_text(json.dumps({'boards':boards,'names':data['names']},ensure_ascii=False),encoding='utf-8')
    (public/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
    print(f"Prepared {len(boards)} boards, {len(ranks)} rank snapshots and {len(matches)} player-scoped guild matches")
if __name__=='__main__':main()
