"""Exercise production player->guild->reports pipeline without starting Discord."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from utility import wwm
from utility.guild_battles import get_player_guild_battles

async def main():
    identifier=sys.argv[1]
    pid,host,_=await wwm.resolve_player_identifier(identifier)
    if not pid: raise RuntimeError('Player resolution failed')
    result=await get_player_guild_battles(pid,host)
    print(json.dumps({'resolved':True,'status':result['status'],'available_ids':result.get('available'),
        'reports_checked':result.get('scanned'),'player_matches':len(result['matches']),'partial':result.get('partial')},ensure_ascii=True))
    await wwm.close_session()

if __name__=='__main__': asyncio.run(main())
