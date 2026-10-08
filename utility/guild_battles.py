"""Player-scoped public guild battle history, with bounded requests and caching."""
import asyncio
import json
import math
import time
from urllib.parse import urlsplit

_cache = {}
_slots = asyncio.Semaphore(2)

def collect_battle_ids(value):
    """The guild API stores battle indexes in play, not standalone fields."""
    ids=[]
    def collect(node):
        if isinstance(node,dict):
            for k,v in node.items():
                if k in ('recent_space_idx','season_recent_space_idx','spaceidx') and isinstance(v,list):
                    ids.extend(x for x in v if isinstance(x,str) and x)
                else: collect(v)
        elif isinstance(node,list):
            for v in node: collect(v)
    collect(value)
    return list(dict.fromkeys(ids))

def normalize_reports(reports, pid):
    found = {}
    malformed = 0
    for report in reports:
        try:
            stats = report.get('StatisticsJsonStr', {})
            if isinstance(stats, (str, bytes)): stats = json.loads(stats)
            if not isinstance(stats, dict): raise ValueError()
            own = stats.get(pid)
            if own is None: continue
            if not isinstance(own, dict) or not report.get('SpaceId'): raise ValueError()
            camp = own.get('camp')
            team = 'Red' if camp == 1 else 'Blue' if camp == 2 else None
            outcome = 'Unavailable'
            if team:
                enemy = 'Blue' if team == 'Red' else 'Red'
                if report.get('EnterMode') == 3:
                    win, loss = report.get('Is'+team+'Win'), report.get('Is'+enemy+'Win')
                    if win is True and loss is False: outcome = 'Win'
                    elif loss is True and win is False: outcome = 'Loss'
                else:
                    fields = [report.get(c+k) for c in (team,enemy) for k in ('NewScore','OldScore')]
                    if all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in fields):
                        delta = fields[0]-fields[1]-(fields[2]-fields[3])
                        outcome = 'Win' if delta>0 else 'Loss' if delta<0 else 'Equal score gains'
            timestamp=report.get('TimeStamp')
            if not isinstance(timestamp,(int,float)) or not math.isfinite(timestamp) or not 0<timestamp<253402300799: timestamp=None
            found[report['SpaceId']] = {'id':report['SpaceId'],'timestamp':timestamp,'stats':own,'team':team,'outcome':outcome,
                'player_pid':pid,'participants':{p:s for p,s in stats.items() if isinstance(s,dict)},
                'guilds':{'Red':report.get('RedClubId'),'Blue':report.get('BlueClubId')},
                'scores':{c:[report.get(c+'OldScore'),report.get(c+'NewScore')] for c in ('Red','Blue')},
                'opponent_id':report.get(('Blue' if team=='Red' else 'Red')+'ClubId') if team else None,
                'season':report.get('SeasonNo'),'round':report.get('CurRound')}
        except (ValueError,TypeError,AttributeError): malformed += 1
    return sorted(found.values(),key=lambda r:r['timestamp'] or 0,reverse=True), malformed

async def load_participant_names(match):
    """Resolve only the selected match, never every historical roster at once."""
    from utility import wwm
    names=match.setdefault('player_names',{})
    if match.get('_names_loaded'):return
    pairs=[(pid,s.get('hostnum')) for pid,s in match.get('participants',{}).items() if s.get('hostnum') and pid not in names][:200]
    for offset in range(0,len(pairs),40):
        groups={}
        for pid,host in pairs[offset:offset+40]:groups.setdefault(host,[]).append(pid)
        try:
            result=await wwm.get_bulk_players_info_multi_hostnum(groups,fields=['base'])
            if not result or result.get('code')!=0:continue
            for pid,p in result.get('result',{}).items():
                base=p.get('base',{})
                if base.get('nickname'):names[pid]={'name':base['nickname'],'number':base.get('number_id')}
        except Exception:continue
    match['_names_loaded']=True

async def get_player_guild_battles(pid, hostnum):
    from utility import wwm
    from settings import WWM_UID, RANK_GET_RANKLIST_URL
    key=(str(pid),hostnum)
    async with _slots:
        if key in _cache and time.monotonic()-_cache[key][0]<180: return _cache[key][1]
        profile=await wwm.get_bulk_players_info([pid],fields=['club'],hostnum=hostnum)
        if not profile or profile.get('code')!=0 or pid not in profile.get('result',{}):
            return {'status':'unavailable','matches':[]}
        club=profile['result'][pid].get('club') or {}
        if not club.get('club_id'): return {'status':'no_guild','matches':[]}
        guild=await wwm.get_custom_guild_info(club['club_id'],hostnum=club.get('hostnum') or hostnum,
            fields={'base':[],'play':[],'league_info':[],'pk_match_info':[]})
        if not guild or guild.get('code')!=0: return {'status':'unavailable','matches':[]}
        ids=collect_battle_ids(guild.get('result',{}))
        if not ids:
            return {'status':'no_records','matches':[],'scanned':0,'available':0}
        # Sort the ObjectId-style prefix only to prioritize requests; display uses TimeStamp.
        import base64
        def hint(s):
            try: return int.from_bytes(base64.b64decode(s,validate=True)[:4],'big')
            except ValueError: return 0
        chosen=sorted(ids,key=hint,reverse=True)[:100]
        reports=[];failed=0
        url='https://'+urlsplit(RANK_GET_RANKLIST_URL).netloc+'/flk/club_combat/club_combat_read_history'
        for offset in range(0,len(chosen),10):
            response=await wwm._wwm_api_post(url,{'uid':WWM_UID,'space_ids':chosen[offset:offset+10]},timeout=15,max_retries=0)
            histories=(response or {}).get('result',{}).get('histories')
            if not response or response.get('code')!=0 or not isinstance(histories,list): failed+=1;continue
            reports.extend(histories)
        matches,malformed=normalize_reports(reports,pid)
        opponents={}
        for r in reports:
            if isinstance(r,dict):
                for c in ('Red','Blue'):
                    if r.get(c+'ClubId') and r.get(c+'ClubHostNum'): opponents[r[c+'ClubId']]=r[c+'ClubHostNum']
        names={}
        pairs=list(opponents.items())
        for offset in range(0,len(pairs),20):
            batch=pairs[offset:offset+20]
            try:
                info=await wwm.get_club_brief_info_batch([x[0] for x in batch],[x[1] for x in batch])
                for g in info or []:
                    cid=g.get('club_id') or g.get('id')
                    if cid and g.get('base',{}).get('name'): names[cid]=g['base']['name']
            except Exception: pass  # Names are optional; never discard valid combat data.
        data={'status':'ok','matches':matches,'guild_name':guild.get('result',{}).get('base',{}).get('name'),
            'names':names,'scanned':len({r.get('SpaceId') for r in reports if isinstance(r,dict)}),
            'available':len(ids),'partial':bool(failed or malformed or len(ids)>len(chosen) or len(reports)<len(chosen))}
        if len(_cache)>=128: _cache.pop(next(iter(_cache)))
        _cache[key]=(time.monotonic(),data)
        return data
