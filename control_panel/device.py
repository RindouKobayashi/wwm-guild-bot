"""Owner-only device workspace. Terminal bytes and file bodies never enter audits."""
import asyncio
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import functools
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from aiohttp import web,ClientSession,ClientTimeout
import psutil
from .file_reader import Reader,PROTOCOL,FileError,PAGE
from .device_transport import profiles,SSHWorker
from .supervisor import BOT

def install(app,panel):
    manager=Devices(panel)
    routes=[('GET','/api/device/context',manager.context),('GET','/api/device/targets',manager.targets),
      ('GET','/api/device/connections',manager.connection_list),('POST','/api/device/connections',manager.connect),('GET','/api/device/connections/{id}',manager.connection_status),('DELETE','/api/device/connections/{id}',manager.disconnect),
      ('POST','/api/terminal/sessions',manager.terminal_create),('POST','/api/terminal/sessions/{id}/attach',manager.attach),('GET','/api/terminal/sessions/{id}/ws',manager.websocket),('DELETE','/api/terminal/sessions/{id}',manager.terminal_close),
      ('POST','/api/device/files',manager.files),('POST','/api/device/files/read',manager.file_read),('POST','/api/device/files/raw-access',manager.raw_access),('POST','/api/device/files/search',manager.search),('POST','/api/device/files/download',manager.download),
      ('GET','/api/device/summary',manager.summary),('GET','/api/device/processes',manager.processes),('GET','/api/device/timeline',manager.timeline),
      ('GET','/api/device/jobs/{id}',manager.job_status),('POST','/api/device/jobs/{id}/cancel',manager.job_cancel),
      ('GET','/api/energy-alerts',manager.energy),('GET','/api/backups',manager.backups),('POST','/api/backups',manager.backup_create),('POST','/api/backups/{id}/download',manager.backup_download)]
    for method,path,handler in routes:app.router.add_route(method,path,handler)
    async def start(app):manager.watch=asyncio.create_task(manager.watchdog())
    async def close(app):await manager.close()
    app.on_startup.append(start);app.on_cleanup.append(close)
    return manager

class Devices:
    def __init__(self,panel):
        self.panel=panel;self.root=panel.store.path.parents[3] if panel.store.path.parents[2].name=='control_panel' else BOT
        self.reader=Reader(self.root,panel.environment)
        self.io=ThreadPoolExecutor(max_workers=2,thread_name_prefix='panel-files');self.reads=0;self.ssh=None
        self.browsers={};self.connections={};self.sessions={};self.tickets={};self.grants={};self.jobs={};self.watch=None
        self.backup_busy=False;self.process_flight=False
    def browser(self,request):
        key=request.cookies.get('wwm-device','')
        if key not in self.browsers:raise web.HTTPForbidden(text='Open Device workspace again to establish this browser session.')
        self.browsers[key]=time.monotonic();return key
    async def context(self,request):
        key=request.cookies.get('wwm-device','')
        if key not in self.browsers:
            if len(self.browsers)>=32:raise web.HTTPServiceUnavailable(text='Too many device browser sessions. Close an old session or restart the panel.')
            key=secrets.token_urlsafe(32)
        self.browsers[key]=time.monotonic()
        response=web.json_response(dict(hostname=self.panel.hostname,environment=self.panel.environment,username=getpass.getuser(),terminal_url='/terminal.html'))
        response.set_cookie('wwm-device',key,secure=True,httponly=True,samesite='Strict',max_age=14400,path='/')
        return response
    async def audit(self,kind,target,**data):
        try:await self.panel.db('device_audit',dict(id=uuid.uuid4().hex,time=time.time(),identity=self.panel.owner,kind=kind,target=target,**data))
        except Exception:self.panel.storage_error='Device audit could not be written. Check available disk space; recovery sessions remain available.'
    async def targets(self,request):
        self.browser(request)
        try:rows=profiles();error=None
        except Exception:rows={};error='Device profiles are invalid. Check devices.local.json on this host.'
        return web.json_response(dict(items=[dict(id='local',name='This panel host',hostname=self.panel.hostname,environment=self.panel.environment,username=getpass.getuser(),confirmation=self.panel.hostname,transport='local')]+[
            {k:v for k,v in row.items() if k in ('id','name','hostname','username','environment','confirmation')}|{'transport':'ssh'} for row in rows.values()],error=error))
    def owned_connection(self,key,browser):
        record=self.connections.get(key)
        if not record or record['browser']!=browser:raise web.HTTPNotFound(text='Connection not found in this browser.')
        return record
    async def connect(self,request):
        browser=self.browser(request);body=await request.json();profile=profiles().get(body.get('target'))
        if not profile:raise web.HTTPBadRequest(text='Choose a configured target.')
        if profile['hostname'].split('.',1)[1]!=self.panel.hostname.split('.',1)[1]:raise web.HTTPBadRequest(text='Target is outside this panel’s tailnet.')
        if profile['environment']=='production' and body.get('confirmation')!=profile['confirmation']:raise web.HTTPBadRequest(text='Type the displayed production target name.')
        if sum(c['state'] in ('connecting','authentication_required','connected') for c in self.connections.values())>=4:raise web.HTTPConflict(text='Four SSH connections are already active.')
        if self.ssh is None:self.ssh=SSHWorker()
        key=uuid.uuid4().hex
        record=dict(id=key,browser=browser,profile=profile,target=profile['id'],state='connecting',message='Connecting to the approved SSH target.',created=time.monotonic(),last_used=time.monotonic())
        self.connections[key]=record
        async def work():
            await self.ssh.call(self.ssh.connect(key,profile,record))
            await self.audit('ssh/'+record['state'],profile['id'],username=profile['username'])
        record['task']=asyncio.create_task(work())
        await self.audit('ssh/requested',profile['id'],username=profile['username'])
        return web.json_response(self.public_connection(record),status=202)
    def public_connection(self,record):return {k:v for k,v in record.items() if k in ('id','target','state','message','authentication_url')}
    async def connection_list(self,request):
        browser=self.browser(request)
        return web.json_response({'items':[self.public_connection(c) for c in self.connections.values() if c['browser']==browser]})
    async def connection_status(self,request):
        record=self.owned_connection(request.match_info['id'],self.browser(request));record['last_used']=time.monotonic()
        if record['state']=='connected' and self.ssh:
            try:await self.ssh.call(self.check_ssh(record['id']))
            except Exception:
                record.update(state='disconnected',message='SSH disconnected. Reconnect explicitly.');self.revoke(record['browser'],record['id'])
        return web.json_response(self.public_connection(record))
    async def check_ssh(self,key):self.ssh.connection(key)
    async def disconnect_record(self,record):
        for session in list(self.sessions.values()):
            if session.connection==record['id']:await session.close()
        task=record.get('task')
        if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        if self.ssh:await self.ssh.call(self.ssh.disconnect(record['id']))
        record.update(state='disconnected',message='Disconnected by owner.');self.revoke(record['browser'],record['id'])
    async def disconnect(self,request):
        record=self.owned_connection(request.match_info['id'],self.browser(request));await self.disconnect_record(record)
        await self.audit('ssh/closed',record['target']);return web.json_response(self.public_connection(record))
    def target(self,body,browser):
        target=body.get('target','local');connection=body.get('connection','')
        if target=='local':return None
        record=self.owned_connection(connection,browser)
        if record['target']!=target or record['state']!='connected':raise web.HTTPConflict(text='Connect to this exact target before continuing.')
        record['last_used']=time.monotonic();return record
    async def local_io(self,function,*args):
        if self.reads>=2:raise web.HTTPServiceUnavailable(text='File readers are busy. Try again shortly.')
        self.reads+=1
        future=asyncio.get_running_loop().run_in_executor(self.io,functools.partial(function,*args))
        # Capacity belongs to the actual worker, even if the caller cancels.
        future.add_done_callback(lambda _:setattr(self,'reads',self.reads-1))
        return await asyncio.shield(future)
    async def file_call(self,body,browser,action,**parameters):
        record=self.target(body,browser);message=dict(action=action,protocol=PROTOCOL,**parameters)
        try:
            if record:
                if self.reads>=2:raise web.HTTPServiceUnavailable(text='File readers are busy. Try again shortly.')
                self.reads+=1
                try:return await self.ssh.call(self.ssh.read(record['id'],record['profile'],message))
                finally:self.reads-=1
            return await self.local_io(self.reader.dispatch,message)
        except ValueError as exc:raise web.HTTPBadRequest(text=str(exc)) from None
    async def files(self,request):
        browser=self.browser(request);body=await request.json()
        return web.json_response(await self.file_call(body,browser,'list'))
    def raw(self,body,browser):
        if not body.get('raw'):return False
        grant=self.grants.get((browser,body.get('target','local'),body.get('connection',''),body.get('id')))
        if not grant or grant<time.monotonic():raise web.HTTPForbidden(text='Raw access expired. Reveal this file again deliberately.')
        return True
    def revoke(self,browser,connection=None):
        self.grants={k:v for k,v in self.grants.items() if not (k[0]==browser and (connection is None or k[2]==connection))}
    async def raw_access(self,request):
        browser=self.browser(request);body=await request.json();self.target(body,browser)
        self.revoke(browser)
        if body.get('reveal') is not True:return web.json_response({'hidden':True})
        if body.get('confirmed') is not True:raise web.HTTPBadRequest(text='Confirm that raw logs can contain credentials or player text.')
        registry=await self.file_call(body,browser,'list')
        if not any(i['id']==body.get('id') and i['kind']=='log' for i in registry['items']):raise web.HTTPBadRequest(text='Raw reveal is available only for a registered log file.')
        self.grants[(browser,body.get('target','local'),body.get('connection',''),body['id'])]=time.monotonic()+300
        await self.audit('file/raw-reveal',body.get('target','local'),file_id=body['id'])
        return web.json_response({'expires':time.time()+300})
    async def file_read(self,request):
        browser=self.browser(request);body=await request.json()
        return web.json_response(await self.file_call(body,browser,'read',id=body.get('id'),offset=body.get('offset'),limit=body.get('limit',PAGE),raw=self.raw(body,browser),version=body.get('version'),tail=body.get('tail',False)))
    async def start_job(self,browser,target,work):
        # Result bodies remain ephemeral; only metadata enters the operation audit.
        if sum(j['state']=='running' for j in self.jobs.values())>=2:raise web.HTTPConflict(text='Two device operations are already running.')
        for key,old in list(self.jobs.items()):
            if len(self.jobs)<20:break
            if old['state']!='running':self.jobs.pop(key,None)
        key=uuid.uuid4().hex;job=dict(id=key,browser=browser,state='running',target=target,started=time.time(),message='Working…')
        self.jobs[key]=job
        async def run():
            try:
                job['result']=await work();job.update(state='succeeded',message='Complete')
                used=0
                for old in reversed(list(self.jobs.values())):
                    if 'result' in old:
                        used+=len(json.dumps(old['result']).encode())
                        if used>8*1024*1024:old.pop('result');old['message']='Result expired from the bounded memory cache. Run again if needed.'
            except asyncio.CancelledError:job.update(state='cancelled',message='Operation cancelled.')
            except Exception as exc:job.update(state='failed',message=str(exc) if isinstance(exc,(ValueError,web.HTTPException)) else 'Device operation failed. Check available storage and target access.')
            finally:
                job['finished']=time.time();await self.audit('operation/'+job['state'],target,operation_id=key)
        job['task']=asyncio.create_task(run())
        return web.json_response({k:v for k,v in job.items() if k not in ('browser','task','result')},status=202)
    async def search(self,request):
        browser=self.browser(request);body=await request.json();self.raw(dict(body,raw=False),browser)
        return await self.start_job(browser,'file-search/'+body.get('target','local'),lambda:self.file_call(body,browser,'search',ids=body.get('ids'),query=body.get('query')))
    async def job_status(self,request):
        job=self.jobs.get(request.match_info['id'])
        if not job or job['browser']!=self.browser(request):raise web.HTTPNotFound()
        return web.json_response({k:v for k,v in job.items() if k not in ('browser','task')})
    async def job_cancel(self,request):
        job=self.jobs.get(request.match_info['id'])
        if not job or job['browser']!=self.browser(request):raise web.HTTPNotFound()
        job['task'].cancel();await asyncio.gather(job['task'],return_exceptions=True)
        return web.json_response({'state':job['state']})
    async def download(self,request):
        browser=self.browser(request);body=await request.json();raw=self.raw(body,browser)
        registry=await self.file_call(body,browser,'list');item=next((i for i in registry['items'] if i['id']==body.get('id')),None)
        if not item:raise web.HTTPNotFound()
        if item['size']>50*1024*1024:raise web.HTTPBadRequest(text='Download is limited to 50 MiB. Select a smaller rotation.')
        # Prepare before headers so errors are understandable. Text is redacted by
        # default; raw downloads preserve original bytes via the reader protocol.
        response=web.StreamResponse(headers={'Content-Type':'application/octet-stream','Content-Length':str(item['size']),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':'attachment; filename="'+Path(item['name']).name.replace('"','')+'"'})
        page=await self.file_call(body,browser,'bytes',id=body['id'],offset=0,raw=raw,version=item['version'])
        await response.prepare(request);offset=0
        try:
            while True:
                if raw:self.raw(body,browser)
                data=base64.b64decode(page['data'])[:item['size']-offset];await response.write(data);offset+=len(data)
                if page['eof'] or offset>=item['size']:break
                page=await self.file_call(body,browser,'bytes',id=body['id'],offset=offset,raw=raw,version=item['version'])
        finally:await self.audit('file/download',body.get('target','local'),file_id=body['id'],raw=raw)
        return response
    async def terminal_create(self,request):
        browser=self.browser(request);body=await request.json();record=self.target(body,browser)
        environment=record['profile']['environment'] if record else self.panel.environment
        confirmation=record['profile']['confirmation'] if record else self.panel.hostname
        if environment=='production' and body.get('confirmation')!=confirmation:raise web.HTTPBadRequest(text='Type the production target machine name to open its shell.')
        if sum(not s.closed for s in self.sessions.values())>=2:raise web.HTTPConflict(text='Two terminal sessions are already open. Close one first.')
        session=TerminalSession(self,browser,record)
        self.sessions[session.id]=session
        try:await session.start()
        except Exception:
            await session.close();raise web.HTTPServiceUnavailable(text='Terminal could not start. Check SSH or the optional Windows terminal dependency.') from None
        await self.audit('terminal/opened',session.target,session_id=session.id,username=record['profile']['username'] if record else getpass.getuser())
        return web.json_response(dict(id=session.id,target=session.target,environment=environment),status=201)
    def owned_session(self,key,browser):
        session=self.sessions.get(key)
        if not session or session.browser!=browser or session.closed:raise web.HTTPNotFound(text='Terminal session is closed or unavailable in this browser.')
        return session
    async def attach(self,request):
        browser=self.browser(request);session=self.owned_session(request.match_info['id'],browser)
        if session.ws and not session.ws.closed:raise web.HTTPConflict(text='This terminal already has a controlling connection.')
        self.tickets={k:v for k,v in self.tickets.items() if v[:2]!=(browser,session.id)}
        ticket=secrets.token_urlsafe(32);self.tickets[ticket]=(browser,session.id,time.monotonic()+30)
        return web.json_response({'ticket':ticket,'expires_in':30})
    async def websocket(self,request):
        browser=self.browser(request)
        if request.headers.get('Origin')!=self.panel.origin:raise web.HTTPForbidden(text='Untrusted terminal origin.')
        session=self.owned_session(request.match_info['id'],browser)
        ws=web.WebSocketResponse(max_msg_size=100000,heartbeat=15);await ws.prepare(request)
        try:
            first=await asyncio.wait_for(ws.receive_json(),5)
            if not isinstance(first,dict):raise ValueError('Invalid attachment frame')
            ticket=self.tickets.pop(first.get('ticket',''),None)
            if not ticket or ticket[:2]!=(browser,session.id) or ticket[2]<time.monotonic():await ws.close(code=1008,message=b'Attachment ticket expired or invalid');return ws
            if session.ws and not session.ws.closed:await ws.close(code=1008);return ws
            after=int(first.get('after',0))
            if after<session.base or after>session.produced:await ws.close(code=1008,message=b'Reconnect buffer unavailable; open a new terminal');return ws
            session.ws=ws;session.detached=None
            session.acked=max(session.acked,after);session.credit.set()
            sender=asyncio.create_task(session.send(ws,after))
            try:
                async for msg in ws:
                    if msg.type!=web.WSMsgType.TEXT:break
                    item=json.loads(msg.data)
                    if not isinstance(item,dict):raise ValueError('Invalid terminal frame')
                    if 'ack' in item:
                        ack=int(item['ack'])
                        if not session.acked<=ack<=session.produced:raise ValueError('Invalid acknowledgement')
                        session.acked=ack;session.credit.set()
                    elif 'input' in item:
                        data=base64.b64decode(item['input'],validate=True)
                        if len(data)>65536:raise ValueError('Input too large')
                        await session.write(data);session.last_input=time.monotonic()
                    elif 'resize' in item:
                        rows,cols=map(int,item['resize'])
                        if not 5<=rows<=200 or not 10<=cols<=500:raise ValueError('Invalid terminal size')
                        await session.resize(rows,cols)
            finally:sender.cancel();await asyncio.gather(sender,return_exceptions=True)
        except (ValueError,KeyError,TypeError,asyncio.TimeoutError):await ws.close(code=1008)
        finally:
            if session.ws is ws:session.ws=None;session.detached=time.monotonic()
            self.revoke(browser,session.connection)
        return ws
    async def terminal_close(self,request):
        session=self.owned_session(request.match_info['id'],self.browser(request));await session.close()
        return web.json_response({'closed':True})
    async def summary(self,request):
        self.browser(request)
        return web.json_response(dict(hostname=self.panel.hostname,environment=self.panel.environment,username=getpass.getuser(),uptime_seconds=time.time()-psutil.boot_time(),host=self.panel.status.get('host',{}),tailscale=self.panel.supervisor.tailscale_cache if hasattr(self.panel.supervisor,'tailscale_cache') else {},services=self.panel.status.get('services',{})))
    async def processes(self,request):
        self.browser(request)
        def collect():
            rows=[]
            for process in psutil.process_iter(['pid','name','memory_info','create_time','cpu_percent']):
                with suppress(psutil.Error):
                    info=process.info;rows.append(dict(pid=info['pid'],name=info['name'],memory_bytes=info['memory_info'].rss,cpu_percent=info['cpu_percent'],started=info['create_time']))
            return sorted(rows,key=lambda r:-r['memory_bytes'])[:200]
        return web.json_response({'items':await self.local_io(collect)})
    async def timeline(self,request):
        self.browser(request)
        return web.json_response(dict(audit=await self.panel.history.read('device_audits'),operations=self.panel.operational_cache['operations'],incidents=self.panel.operational_cache['incidents']))
    async def energy(self,request):
        self.browser(request)
        def collect():
            path=self.root/'data/guild_verification.db'
            if not path.exists():return []
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)
            try:
                rows=db.execute('SELECT e.user_id,e.threshold,e.is_enabled,e.last_energy,e.last_seen_ts,e.was_above,e.last_alert_ts FROM energy_alerts e ORDER BY e.user_id LIMIT 500').fetchall()
                return [dict(zip(('user_id','threshold','enabled','last_energy','last_seen','was_above','last_alert'),row)) for row in rows]
            except sqlite3.Error:return []
            finally:db.close()
        items=await self.local_io(collect);observations={};cycle={}
        try:
            port=self.panel.workspace.bridge_port()
            async with ClientSession(timeout=ClientTimeout(total=1),trust_env=False) as http:
                async with http.get(f'http://127.0.0.1:{port}/energy',headers={'Authorization':'Bearer '+self.panel.secret}) as response:
                    if response.status==200:
                        value=await response.json();observations=value.get('items',{});cycle=value.get('cycle',{})
        except Exception:pass
        now=time.time()
        for item in items:
            observation=observations.get(str(item['user_id']))
            item['evaluation']=observation
            item['reason']='Paused' if not item['enabled'] else observation.get('reason') if observation and now-observation['time']<120 else 'No recent evaluation report; cached values alone do not prove current energy.'
            item['cooldown_remaining']=max(0,3600-(now-item['last_alert'])) if item['last_alert'] else 0
            item['rearm_at']=max(0,item['threshold']-30)
        return web.json_response(dict(items=items,cycle=cycle,observations_available=bool(observations),note='Read-only. Opening this inspector does not call the game API or send an alert.'))
    def backup_directory(self):return self.panel.store.path.parent/'backups'
    def backup_sources(self):
        names=('guild_verification.db','schedule.db','market.db','presets.db','giveaways.db','event_log.db','activity.db','homestead.db','sect_monitor.db','ranking_map.db','stickies.db','guild_monitor.db')
        return [self.root/'data'/name for name in names if (self.root/'data'/name).is_file() and not (self.root/'data'/name).is_symlink()]
    async def backups(self,request):
        self.browser(request)
        def inspect():
            items=[];directory=self.backup_directory()
            if directory.exists():
                for path in sorted(directory.glob('*.manifest.json'),reverse=True)[:7]:
                    with suppress(OSError,ValueError):items.append(json.loads(path.read_text()))
            sources=[dict(name=p.name,size=p.stat().st_size) for p in self.backup_sources()]
            return dict(items=items,sources=sources,estimated_bytes=sum(p['size'] for p in sources),budget_bytes=1024**3,maximum=7,excludes=['Credentials','Saved-login material','Raw logs'],separate_capture_times=True)
        return web.json_response(await self.local_io(inspect))
    async def backup_create(self,request):
        browser=self.browser(request);body=await request.json()
        if body.get('confirmed') is not True:raise web.HTTPBadRequest(text='Review the included databases before creating a backup.')
        if self.backup_busy:raise web.HTTPConflict(text='A backup is already running.')
        self.backup_busy=True
        cancel=__import__('threading').Event()
        def create_snapshot():
            directory=self.backup_directory();directory.mkdir(parents=True,exist_ok=True)
            existing=list(directory.glob('*.zip'));sources=self.backup_sources()
            if not sources:raise ValueError('No registered bot databases are present.')
            total=sum(p.stat().st_size for p in directory.glob('*') if p.is_file());estimate=sum(p.stat().st_size for p in sources)
            if len(existing)>=7 or total+estimate>1024**3:raise ValueError('Backup retention limit reached. Download and remove an old snapshot using the host terminal before creating another.')
            if shutil.disk_usage(directory).free<estimate*2+100*1024*1024:raise ValueError('Insufficient space for a safe database snapshot.')
            import tempfile
            key=uuid.uuid4().hex;target=directory/(key+'.zip');deadline=time.monotonic()+30;manifest=dict(id=key,created=time.time(),environment=self.panel.environment,hostname=self.panel.hostname,databases=[],verified=False)
            try:
                with tempfile.TemporaryDirectory(dir=directory,prefix='snapshot-') as temp,zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
                    for source in sources:
                        if cancel.is_set() or time.monotonic()>deadline:raise ValueError('Backup cancelled or exceeded 30 seconds.')
                        relative=source.relative_to(self.root).as_posix()
                        # The reader's no-follow handle check protects source registration.
                        with self.reader.open_regular(relative):pass
                        captured=time.time();src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=.2);dst_path=Path(temp)/source.name;dst=sqlite3.connect(dst_path)
                        def progress(*_):
                            if cancel.is_set() or time.monotonic()>deadline:raise ValueError('Backup cancelled or exceeded 30 seconds.')
                        try:
                            src.backup(dst,pages=128,progress=progress,sleep=.01)
                            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Snapshot integrity check failed.')
                            schema=dst.execute('PRAGMA user_version').fetchone()[0]
                        finally:src.close();dst.close()
                        hasher=hashlib.sha256()
                        with dst_path.open('rb') as stream:
                            for chunk in iter(lambda:stream.read(1024*1024),b''):
                                progress();hasher.update(chunk)
                        digest=hasher.hexdigest();archive.write(dst_path,source.name)
                        manifest['databases'].append(dict(name=source.name,captured=captured,sha256=digest,schema=schema,size=dst_path.stat().st_size))
                    manifest['verified']=True;archive.writestr('manifest.json',json.dumps(manifest,indent=2))
                if sum(p.stat().st_size for p in directory.glob('*') if p.is_file())>1024**3:raise ValueError('Snapshot exceeded the backup storage budget.')
                (directory/(key+'.manifest.json')).write_text(json.dumps(manifest,indent=2))
                return manifest
            except BaseException:target.unlink(missing_ok=True);raise
        def create():
            try:return create_snapshot()
            finally:self.backup_busy=False
        async def work():
            try:return await self.local_io(create)
            except asyncio.CancelledError:cancel.set();raise
            except BaseException:self.backup_busy=False;raise
        try:return await self.start_job(browser,'backup/create',work)
        except BaseException:self.backup_busy=False;raise
    async def backup_download(self,request):
        self.browser(request);body=await request.json()
        if body.get('confirmed') is not True:raise web.HTTPBadRequest(text='Confirm downloading databases which can contain member data.')
        key=request.match_info['id']
        if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise web.HTTPNotFound()
        directory=self.backup_directory();path=directory/(key+'.zip')
        if not path.is_file() or path.is_symlink():raise web.HTTPNotFound()
        await self.audit('backup/download','local',backup_id=key)
        return web.FileResponse(path,headers={'Content-Disposition':'attachment; filename="wwm-backup-'+key+'.zip"'})
    async def watchdog(self):
        while True:
            await asyncio.sleep(5);now=time.monotonic()
            for session in list(self.sessions.values()):
                if not session.closed and (now-session.created>14400 or now-session.last_input>1800 or (session.detached is not None and now-session.detached>60)):await session.close()
                if session.closed:self.sessions.pop(session.id,None)
            for record in self.connections.values():
                if record['state']=='connected' and (now-record['created']>14400 or (now-record['last_used']>600 and not any(s.connection==record['id'] for s in self.sessions.values()))):await self.disconnect_record(record)
            self.tickets={k:v for k,v in self.tickets.items() if v[2]>now};self.grants={k:v for k,v in self.grants.items() if v>now}
            self.jobs={k:v for k,v in self.jobs.items() if v['state']=='running' or time.time()-v.get('finished',time.time())<600}
            self.connections={k:v for k,v in self.connections.items() if v['state'] in ('connected','connecting','authentication_required') or now-v['created']<600}
            self.browsers={k:v for k,v in self.browsers.items() if now-v<14400}
    async def close(self):
        if self.watch:self.watch.cancel();await asyncio.gather(self.watch,return_exceptions=True)
        for session in list(self.sessions.values()):await session.close()
        for record in list(self.connections.values()):await self.disconnect_record(record)
        tasks=[j['task'] for j in self.jobs.values() if not j['task'].done()]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.ssh:await self.ssh.shutdown()
        await asyncio.to_thread(self.io.shutdown,True)

class TerminalSession:
    def __init__(self,manager,browser,record):
        self.manager=manager;self.browser=browser;self.record=record;self.connection=record['id'] if record else '';self.target=record['target'] if record else 'local';self.id=uuid.uuid4().hex
        self.created=self.last_input=time.monotonic();self.detached=self.created;self.closed=False;self.ws=None;self.process=None;self.pump=None
        self.ring=deque();self.produced=0;self.base=0;self.acked=0;self.changed=asyncio.Event();self.credit=asyncio.Event();self.credit.set()
    async def start(self):
        if self.record:await self.manager.ssh.call(self.manager.ssh.terminal(self.connection,self.id,24,80))
        else:
            flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            # Absolute script path avoids importing settings or private modules.
            self.process=await asyncio.create_subprocess_exec(sys.executable,'-B',str(Path(__file__).with_name('terminal_worker.py')),cwd=self.manager.root,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=100000,**flags)
        self.pump=asyncio.create_task(self.read_loop())
    async def read_loop(self):
        try:
            while not self.closed:
                while self.produced-self.acked>=256*1024:
                    self.credit.clear();await self.credit.wait()
                if self.record:data=await self.manager.ssh.call(self.manager.ssh.term_read(self.id))
                else:
                    line=await self.process.stdout.readline()
                    if not line:break
                    value=json.loads(line)
                    if value.get('exit'):break
                    data=base64.b64decode(value['data'])
                if not data:break
                self.produced+=len(data);self.ring.append((self.produced,data))
                while self.ring and self.produced-self.ring[0][0]>1024*1024:
                    end,_=self.ring.popleft();self.base=end
                self.changed.set()
        except (Exception,asyncio.CancelledError):pass
        finally:
            if not self.closed:asyncio.create_task(self.close())
    async def send(self,ws,after):
        while not self.closed:
            self.changed.clear()
            for end,data in list(self.ring):
                if end>after:
                    start=end-len(data);chunk=data[max(0,after-start):]
                    await ws.send_json(dict(seq=end,data=base64.b64encode(chunk).decode()));after=end
            await self.changed.wait()
    async def write(self,data):
        if self.record:await self.manager.ssh.call(self.manager.ssh.term_write(self.id,data))
        else:self.process.stdin.write(json.dumps({'data':base64.b64encode(data).decode()}).encode()+b'\n');await self.process.stdin.drain()
    async def resize(self,rows,cols):
        if self.record:await self.manager.ssh.call(self.manager.ssh.term_resize(self.id,rows,cols))
        else:self.process.stdin.write(json.dumps({'resize':[rows,cols]}).encode()+b'\n');await self.process.stdin.drain()
    async def close(self):
        if self.closed:return
        self.closed=True;self.changed.set();self.credit.set()
        if self.ws and not self.ws.closed:
            with suppress(asyncio.TimeoutError):await asyncio.wait_for(self.ws.close(message=b'Terminal session closed'),1)
        if self.pump and self.pump is not asyncio.current_task():self.pump.cancel();await asyncio.gather(self.pump,return_exceptions=True)
        if self.record:
            with suppress(Exception):await self.manager.ssh.call(self.manager.ssh.term_close(self.id))
        elif self.process:
            # Drain the bounded pipe while shutting down: an unread full pipe
            # otherwise prevents asyncio subprocess wait from completing.
            async def discard():
                while await self.process.stdout.read(65536):pass
            drain=asyncio.create_task(discard())
            descendants=[]
            with suppress(psutil.Error):descendants=psutil.Process(self.process.pid).children(recursive=True)
            with suppress(Exception):self.process.stdin.write(b'{"close":true}\n');await self.process.stdin.drain()
            try:await asyncio.wait_for(self.process.wait(),3)
            except asyncio.TimeoutError:
                with suppress(psutil.Error):
                    for child in reversed(psutil.Process(self.process.pid).children(recursive=True)):
                        with suppress(psutil.Error):child.kill()
                with suppress(ProcessLookupError):self.process.kill()
                await self.process.wait()
            # A Windows venv launcher or console host may exit before its shell.
            # Retain process identities captured while the worker was still owned.
            for child in descendants:
                with suppress(psutil.Error):
                    if child.is_running():child.kill()
            if descendants:await asyncio.to_thread(psutil.wait_procs,descendants,timeout=2)
            drain.cancel();await asyncio.gather(drain,return_exceptions=True)
        self.ring.clear()
        await self.manager.audit('terminal/closed',self.target,session_id=self.id)
