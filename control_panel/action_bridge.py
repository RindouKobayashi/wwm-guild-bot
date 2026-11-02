"""Runs approved message edits in the existing bot; never starts a Discord session."""
import asyncio
import functools
import hashlib
import json
import os
import secrets
import time
import uuid
from aiohttp import web
from .telemetry import parent_id

def lock_for(cog):
    if not hasattr(cog,'_panel_action_lock'):cog._panel_action_lock=asyncio.Lock()
    return cog._panel_action_lock

def serialized(function):
    @functools.wraps(function)
    async def call(self,*args,**kwargs):
        async with lock_for(self):return await function(self,*args,**kwargs)
    return call

def snapshot(message):
    value=dict(content=message.content,embeds=[e.to_dict() for e in message.embeds],components=[x.to_dict() for x in message.components])
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def visible_payload(payload):
    result={'content':payload.get('content'),'components':[]}
    view=payload.get('view')
    if view:result['components']=view.to_components()
    return result

class Bridge:
    def __init__(self,bot):self.bot=bot;self.previews={};self.outcomes={};self.port=None;self.runner=None

    def entries(self):
        items=[]
        for cog in self.bot.cogs.values():
            if type(cog).__name__=='LeaderboardCog':
                for inst in cog.instances:
                    message=getattr(inst,'message',None)
                    if message:items.append((f'leaderboard-{message.id}','Refresh leaderboard · '+inst.lb_type,cog,inst,message))
            elif type(cog).__name__=='ScheduleCog':
                message=getattr(cog,'schedule_message',None)
                if message:items.append(('schedule','Refresh schedule message',cog,None,message))
            elif type(cog).__name__=='MarketCog':
                message=getattr(cog,'last_report_message',None)
                if message:items.append(('market','Refresh market-report message',cog,None,message))
        return items

    def find(self,key):
        return next((row for row in self.entries() if row[0]==key),None)

    async def handle(self,request):
        secret=os.environ.get('WWM_PANEL_INGEST_SECRET','')
        if request.remote not in ('127.0.0.1','::1') or not secret or not secrets.compare_digest(request.headers.get('Authorization',''),'Bearer '+secret):raise web.HTTPForbidden()
        if request.method not in ('GET','POST'):raise web.HTTPMethodNotAllowed(request.method,['GET','POST'])
        if not self.bot.is_ready():return web.json_response({'error':'Bot is disconnected.'},status=409)
        # Bounded in-memory state; no arbitrary message content is stored on disk.
        now=time.time()
        self.previews={k:v for k,v in self.previews.items() if v['expires']>now}
        self.outcomes={k:v for k,v in self.outcomes.items() if v['time']>now-3600}
        if request.method=='GET':
            return web.json_response({'items':[dict(id=k,name=name,guild=str(m.guild),channel=str(m.channel),message_id=str(m.id),mode='Edit existing message') for k,name,c,i,m in self.entries()]})
        body=await request.json();key=body.get('action');entry=self.find(key)
        if not entry:return web.json_response({'error':'The configured target message is unavailable.'},status=404)
        _,name,cog,inst,message=entry
        mode=body.get('mode')
        async with lock_for(cog):
            token=parent_id.set(body.get('operation_id'))
            try:
                if mode=='preview':
                    if len(self.previews)>=20:return web.json_response({'error':'Too many previews. Wait for an older preview to expire.'},status=409)
                    current=await message.channel.fetch_message(message.id)
                    payload=await cog.panel_prepare(inst) if inst is not None else await cog.panel_prepare()
                    if not payload:return web.json_response({'error':'No report data is available for this update.'},status=409)
                    public=visible_payload(payload)
                    if len(json.dumps(public).encode())>65536:return web.json_response({'error':'This preview exceeds the display limit.'},status=409)
                    preview_id=uuid.uuid4().hex
                    self.previews[preview_id]=dict(action=key,target=message.id,expires=now+120,fingerprint=snapshot(current),payload=payload)
                    return web.json_response(dict(id=preview_id,action=key,name=name,expires=now+120,guild=str(message.guild),channel=str(message.channel),message_id=str(message.id),content=public,mode='Edit existing message',mentions='Suppressed'))
                if mode=='check':
                    current=await message.channel.fetch_message(message.id)
                    return web.json_response(dict(message_url=current.jump_url,edited_at=current.edited_at.timestamp() if current.edited_at else None,message='Target is accessible. This check does not prove an uncertain operation succeeded.'))
                if mode!='execute':raise web.HTTPBadRequest()
                key_id=body.get('preview_id','')
                if key_id in self.outcomes:return web.json_response(self.outcomes[key_id])
                preview=self.previews.get(key_id)
                if not preview or preview['action']!=key or preview['target']!=message.id or preview['expires']<=time.time():return web.json_response({'error':'Preview expired or target changed. Prepare a new preview.'},status=409)
                current=await message.channel.fetch_message(message.id)
                if snapshot(current)!=preview['fingerprint']:return web.json_response({'error':'The target changed after preview. Prepare a new preview.'},status=409)
                # Consume before dispatch. A timeout must never allow a duplicate edit.
                self.previews.pop(key_id)
                self.outcomes[key_id]=dict(state='unknown',time=time.time(),message='Dispatch outcome is unknown; do not automatically resend.')
                import discord
                updated=await current.edit(**preview['payload'],allowed_mentions=discord.AllowedMentions.none())
                result=dict(state='succeeded',time=time.time(),message='Existing message updated. Mentions were suppressed.',message_url=updated.jump_url)
                self.outcomes[key_id]=result
                return web.json_response(result)
            except (web.HTTPException,asyncio.CancelledError):raise
            except Exception:
                return web.json_response({'error':'Could not verify or update the existing target message. No replacement message was created.'},status=409)
            finally:parent_id.reset(token)

    async def start(self):
        async def bounded(request):
            try:return await asyncio.wait_for(self.handle(request),30)
            except asyncio.TimeoutError:return web.json_response({'error':'Bot action exceeded 30 seconds. Its outcome must be checked; it will not be retried.'},status=504)
        app=web.Application(client_max_size=65536);app.router.add_route('*','/actions',bounded)
        self.runner=web.AppRunner(app,access_log=None,shutdown_timeout=2);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.port=site._server.sockets[0].getsockname()[1]

    async def close(self):
        if self.runner:await self.runner.cleanup()
