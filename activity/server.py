"""Standalone Activity web server. Never loads a bot or sends Discord messages."""
import argparse,asyncio,json,os,secrets,sqlite3,sys,time
from contextlib import closing
from pathlib import Path
from aiohttp import web,ClientSession,ClientTimeout
HERE=Path(__file__).resolve().parent
BOT=HERE.parent
sys.path.insert(0,str(BOT))

async def resolve_rank_names(result):
    """Use each member's recorded host; never guess a server from the team ID."""
    from utility.wwm import get_bulk_players_info_multi_hostnum
    names={};targets={}
    for row in result.get('rank_list',[]):
        ud=row.get('ud') or {};info=row.get('player_info') or {}
        profiles={row.get('pid'):info} if 'base' in info else info
        for pid,profile in profiles.items():
            if isinstance(profile,dict) and profile.get('base',{}).get('nickname'):
                names[pid]={'name':profile['base']['nickname']}
        members=ud.get('members') or [row.get('pid')]
        for member in members:
            pid=member if isinstance(member,str) else member.get('pid')
            meta=ud.get(pid,{})
            host=meta.get('hostnum') or (row.get('hostnum') if not ud.get('members') else None)
            if pid and host and pid not in names:targets[pid]=host
    pairs=list(targets.items())[:400]
    for offset in range(0,len(pairs),40):
        groups={}
        for pid,host in pairs[offset:offset+40]:groups.setdefault(host,[]).append(pid)
        try:
            response=await get_bulk_players_info_multi_hostnum(groups,fields=['base'])
            if response and response.get('code')==0:
                for pid,profile in response.get('result',{}).items():
                    base=profile.get('base') or {}
                    if base.get('nickname'):names[pid]={'name':base['nickname'],'number':base.get('number_id')}
        except Exception:
            pass # A missing/deleted profile must not hide the recorded run.
    return {**result,'player_names':names}

def binding(user_id):
    db=BOT/'data/guild_verification.db'
    if not db.exists():return None
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as conn:
        row=conn.execute('SELECT character_uid FROM verified_members WHERE user_id=?',(str(user_id),)).fetchone()
    return row[0] if row else None

def create_app(live=False):
    catalogue=json.loads((HERE/'public/catalogue.json').read_text(encoding='utf-8'))
    boards={b['key']:b for b in catalogue['boards']};sessions={};cache={};slots=asyncio.Semaphore(2)
    app_id=os.environ.get('DISCORD_CLIENT_ID','');secret=os.environ.get('DISCORD_CLIENT_SECRET','')
    if live and (not app_id.isdigit() or not secret):raise ValueError('Discord mode requires DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET')
    async def user(request):
        token=request.headers.get('Authorization','').removeprefix('Bearer ')
        session=sessions.get(token)
        if not session or session['expires']<time.monotonic():
            sessions.pop(token,None);raise web.HTTPUnauthorized(text='Sign in through Discord first.')
        return session
    @web.middleware
    async def errors(request,handler):
        try:return await handler(request)
        except web.HTTPException as e:
            return web.json_response({'error':e.text},status=e.status)
        except Exception:
            return web.json_response({'error':'Data could not be loaded. Try again later.'},status=502)
    app=web.Application(middlewares=[errors],client_max_size=16384)
    async def config(request):return web.json_response({'service':'wwm-activity','mode':'discord' if live else 'preview','client_id':app_id})
    async def auth(request):
        if not live:raise web.HTTPForbidden(text='Discord authentication is disabled in local preview.')
        if request.headers.get('Origin')!=f'https://{app_id}.discordsays.com':raise web.HTTPForbidden(text='Untrusted origin.')
        body=await request.json();code=body.get('code')
        if not isinstance(code,str) or not 1<=len(code)<=2048:raise web.HTTPBadRequest(text='Invalid authorization code.')
        async with ClientSession(timeout=ClientTimeout(total=15)) as http:
            async with http.post('https://discord.com/api/oauth2/token',data={'client_id':app_id,'client_secret':secret,'grant_type':'authorization_code','code':code}) as response:
                if response.status!=200:raise web.HTTPUnauthorized(text='Discord authorization failed.')
                oauth=await response.json()
            async with http.get('https://discord.com/api/users/@me',headers={'Authorization':'Bearer '+oauth['access_token']}) as response:
                if response.status!=200:raise web.HTTPUnauthorized(text='Discord identity could not be verified.')
                identity=await response.json()
        token=secrets.token_urlsafe(32)
        for key in list(sessions):
            if sessions[key]['expires']<time.monotonic():sessions.pop(key)
        if len(sessions)>=1024:raise web.HTTPServiceUnavailable(text='Too many sessions. Try again later.')
        sessions[token]={'id':identity['id'],'expires':time.monotonic()+min(int(oauth.get('expires_in',3600)),3600)}
        character_number = binding(identity['id'])
        return web.json_response({'session':token,'access_token':oauth['access_token'],'user':{'name':identity.get('global_name') or identity['username'],'bound':bool(character_number),'character_number':character_number}},headers={'Cache-Control':'no-store'})
    async def snapshot(request):
        if live:raise web.HTTPForbidden(text='Saved preview data is unavailable in Discord mode.')
        return web.FileResponse(HERE/'public/snapshot.json')
    async def rank(request):
        if not live:raise web.HTTPForbidden(text='Live data is disabled in snapshot preview.')
        await user(request)
        entry=boards.get(request.query.get('board'));mode=request.query.get('mode','overall')
        if not entry:raise web.HTTPBadRequest(text='Choose an extracted board.')
        if entry['family']=='abyss':
            if mode=='path':
                paths=entry.get('paths') or []
                if entry.get('special_no_path_ui') or len(paths)!=1 or not paths[0].get('name'):raise web.HTTPBadRequest(text='No normal Path Trial for this encounter.')
                name=paths[0]['rank_name']
            elif mode in ('overall','no_hit'):name=entry['no_hit_rank_name'] if mode=='no_hit' else entry['overall_rank_name']
            else:raise web.HTTPBadRequest(text='Invalid ranking mode.')
        else:name=entry['rank_name']
        page=request.query.get('page','1')
        if not page.isdigit() or not 1<=int(page)<=50:raise web.HTTPBadRequest(text='Invalid page.')
        key=(name,int(page))
        async with slots:
            if key not in cache or time.monotonic()-cache[key][0]>180:
                from utility.wwm import get_rank_list
                response=await get_rank_list(name,page=int(page))
                if not response or response.get('code')!=0:raise web.HTTPBadGateway(text='Game ranking service unavailable.')
                if len(cache)>=256:cache.pop(next(iter(cache)))
                cache[key]=(time.monotonic(),await resolve_rank_names(response['result']))
        return web.json_response(cache[key][1])
    async def guild(request):
        if not live:raise web.HTTPForbidden(text='Live data is disabled in snapshot preview.')
        session=await user(request)
        # Check current binding per request, never trust client identity or host access.
        if not binding(session['id']):raise web.HTTPForbidden(text='Bind your character using the Discord bot before searching guild battles.')
        number=request.query.get('number','')
        if len(number)!=10 or not number.isascii() or not number.isdigit():raise web.HTTPBadRequest(text='Enter a 10-digit player Number ID.')
        async with slots:
            from utility.wwm import get_player_info
            from utility.guild_battles import get_player_guild_battles,load_participant_names
            response=await get_player_info(number,fields=['base'])
            player=(response or {}).get('result') or {}
            if (response or {}).get('code')!=0 or not player.get('id') or not player.get('hostnum'):raise web.HTTPNotFound(text='Player not found.')
            data=await get_player_guild_battles(player['id'],player['hostnum'])
            selected=request.query.get('match')
            if selected:
                match=next((m for m in data.get('matches',[]) if m['id']==selected),None)
                if match is None:raise web.HTTPNotFound(text='Match is not in this player’s returned history.')
                await load_participant_names(match)
                return web.json_response(match)
            if data.get('matches'):await load_participant_names(data['matches'][0])
        return web.json_response({**data,'own':player['id']})
    async def static(request):
        relative=request.match_info.get('path','') or 'index.html'
        path=(HERE/'public'/relative).resolve()
        if not path.is_relative_to((HERE/'public').resolve()) or not path.is_file():raise web.HTTPNotFound()
        if relative not in ('index.html','app.js','discord-sdk.js','style.css','catalogue.json') and not relative.startswith('art/'):raise web.HTTPNotFound()
        return web.FileResponse(path)
    app.router.add_get('/api/config',config);app.router.add_post('/api/auth',auth)
    app.router.add_get('/api/snapshot',snapshot);app.router.add_get('/api/rank',rank);app.router.add_get('/api/guild',guild)
    app.router.add_get('/{path:.*}',static)
    async def close(app):
        if live:
            from utility.wwm import close_session
            await close_session()
    app.on_cleanup.append(close)
    return app
if __name__=='__main__':
    from dotenv import load_dotenv
    parser=argparse.ArgumentParser();parser.add_argument('--discord',action='store_true');parser.add_argument('--port',type=int,default=8766);parser.add_argument('--profile',choices=['test','production']);args=parser.parse_args()
    load_dotenv(HERE/('.env.'+args.profile if args.profile else '.env'),override=False)
    os.chdir(BOT)
    web.run_app(create_app(args.discord),host='127.0.0.1',port=args.port,print=lambda x:print(x,flush=True))
