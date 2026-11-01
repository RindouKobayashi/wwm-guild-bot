"""Loopback-only private control plane. Never mounted on the public Activity."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from email.header import decode_header, make_header
import functools
import json
import os
from pathlib import Path
import secrets
import socket
import time
import uuid
import tempfile
import math
from aiohttp import web
import psutil
from .privacy import redact
from .storage import Store
from .supervisor import BOT, Supervisor, FileLock
from .incidents import Monitor
from .version import versions
from .history import History, HistoryBusy

PUBLIC = Path(__file__).parent/'public'

class Panel:
    def __init__(self, environment, owner, hostname, other_url=''):
        self.environment, self.owner, self.hostname, self.other_url = environment, owner, hostname, other_url
        self.origin = 'https://' + hostname + ':8443'
        self.secret, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.supervisor = Supervisor(environment, self.secret)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='panel-store')
        self.store = Store(BOT/'control_panel'/'data'/environment/'history.db')
        self.history = History(self.store)
        self.pending = asyncio.Queue(1024)
        self.heartbeats, self.subscribers = {}, set()
        self.dropped, self.storage_error = {}, None
        self.status = {}; self.tasks = []; self.busy = False
        self.created = time.time()
        self.legacy_offsets = {}
        self.operation_task = None
        self.monitor = Monitor()
        self.running_versions = versions()
        self.disk_versions = self.running_versions
        self.operational_cache={'incidents':[],'operations':[],'updated_at':None}
        self.indexing_cache={'complete':False,'cursor':0,'target':None}
        self.authentication_mode=self.auth_mode()
        self.health_delay_ms=0
        self.operation_records={}
        self.storage_cache={}

    async def db(self, method, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.executor,
            functools.partial(getattr(self.store, method), *args, **kwargs))

    def publish(self, kind):
        for queue in self.subscribers.copy():
            with suppress(asyncio.QueueFull):
                queue.put_nowait(kind)

    async def writer(self):
        tick = 0
        last_prune=0
        while True:
            await asyncio.sleep(1)
            events = []
            while len(events) < 128:
                try:
                    events.append(self.pending.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                if events:
                    await self.db('append', events)
                    self.publish('update')
                self.indexing_cache=await self.db('backfill')
                tick += 1
                pressure=self.store.storage_bytes()>self.store.budget*.9
                maintenance=tick%60==0 or (pressure and time.monotonic()-last_prune>=5)
                if maintenance:
                    await self.db('prune')
                    last_prune=time.monotonic()
                self.storage_cache={'physical_bytes':self.store.storage_bytes(),'budget_bytes':self.store.budget}
                if events or maintenance:
                    self.storage_error = None
            except Exception:
                self.storage_error = 'History write failed. Check available disk space and directory permissions.'

    async def sample(self):
        tick = 0
        while True:
            cycle_started=time.monotonic()
            try:
                try:
                    services=await asyncio.wait_for(self.supervisor.states(include_tailscale=False),timeout=2)
                except asyncio.TimeoutError:
                    services={name:dict(value,state='Unknown') for name,value in self.supervisor.last.items()}
                now = time.time()
                for name, service in services.items():
                    beat = self.heartbeats.get(name)
                    service['heartbeat'] = beat['data'] if beat else None
                    service['last_seen'] = beat['time'] if beat else None
                    if service['ownership'] == 'owned' and name != 'tunnel':
                        if not beat or now-beat['time'] > 15:
                            service['state'] = 'Unknown'
                        elif name == 'bot':
                            service['state'] = 'Healthy' if beat['data'].get('connected') else 'Degraded'
                    if name == 'activity' and service['ownership'] == 'owned' and service['local_health'] != 'Healthy':
                        service['state'] = 'Degraded'
                memory = psutil.virtual_memory(); disk = psutil.disk_usage(str(BOT))
                temperature = None
                if hasattr(psutil, 'sensors_temperatures'):
                    with suppress(Exception):
                        temps = psutil.sensors_temperatures()
                        temperature = next((row.current for rows in temps.values() for row in rows), None)
                host = {'cpu_percent': psutil.cpu_percent(), 'memory_percent': memory.percent,
                        'memory_used': memory.used, 'memory_total': memory.total, 'disk_free': disk.free,
                        'temperature_c': temperature}
                self.status = {'environment': self.environment, 'hostname': self.hostname, 'host_label': socket.gethostname(),
                    'bot_id': self.supervisor.client, 'services': services, 'host': host, 'sampled_at': now,
                    'other_url': self.other_url, 'dropped': self.dropped, 'storage_error': self.storage_error,
                    'authentication': {'mode': self.authentication_mode, 'token_expiry': 'Unknown'},
                    'busy': self.busy, 'capabilities': ['start', 'stop', 'restart', 'diagnostics'],
                    'operations': self.operational_cache['operations']}
                self.status.update(incidents=self.operational_cache['incidents'], progress=getattr(self.supervisor,'progress',[]),
                    history_updated_at=self.operational_cache['updated_at'],indexing=self.indexing_cache,
                    versions={'running':self.running_versions,'disk':self.disk_versions,
                        'restart_required':self.running_versions['runtime_id']!=self.disk_versions['runtime_id']})
                if tick % 12 == 0:
                    self.enqueue({'kind': 'metric', 'time': now, 'source': 'panel', 'environment': self.environment,
                                  'data': host})
                self.publish('status')
                self.health_delay_ms=max(0,round((time.monotonic()-cycle_started-5)*1000,2))
                tick += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status['health_error']='Status collection failed; previous values are stale.'
            await asyncio.sleep(max(0,5-(time.monotonic()-cycle_started)))

    async def operational(self):
        tick=0
        while True:
            try:
                if self.status:
                    await self.monitor.check(self,self.status.get('services',{}),self.status['host'])
                incidents=await self.db('incidents')
                operations=await self.db('operations')
                self.operational_cache=dict(incidents=incidents,operations=operations,updated_at=time.time())
                if tick%12==0:self.disk_versions=await asyncio.to_thread(versions)
                tick+=1
            except asyncio.CancelledError:raise
            except Exception:
                pass  # Health sampling remains independent of history failures.
            await asyncio.sleep(5)

    async def tailscale_checks(self):
        while True:
            try:
                await self.supervisor.refresh_tailscale()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(30)

    def auth_mode(self):
        from dotenv import dotenv_values
        return dotenv_values(BOT/'.env').get('WWM_AUTH_MODE', 'legacy')

    def enqueue(self, event):
        try:
            self.pending.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped['panel'] = self.dropped.get('panel', 0) + 1

    async def legacy(self):
        """Bounded tail handles truncation/replacement; raw text never crosses ingestion."""
        while True:
            for service, name in [('bot', 'bot'), ('activity', 'activity'), ('tunnel', 'tunnel')]:
                path = self.supervisor.logs/(name+'.log')
                try:
                    stat = path.stat()
                    previous = self.legacy_offsets.get(str(path))
                    offset = previous[1] if previous and previous[0] == stat.st_ino and previous[1] <= stat.st_size else max(0, stat.st_size-32768)
                    with path.open('rb') as stream:
                        stream.seek(offset)
                        lines = stream.read(32768).splitlines()
                        self.legacy_offsets[str(path)] = (stat.st_ino, stream.tell())
                    for line in lines[-100:]:
                        # Legacy files can contain arbitrary user content. Preserve
                        # severity/module diagnostics, never expose the raw body.
                        text = line.decode('utf8', 'replace')
                        level = next((level for level in ('CRITICAL','ERROR','WARNING','DEBUG') if level in text), 'INFO')
                        self.enqueue({'kind': 'log', 'time': time.time(), 'source': service, 'environment': self.environment,
                            'data': {'level': level, 'message': 'Legacy process output ('+str(len(line))+' bytes); inspect local file for full text.', 'module': 'legacy'}})
                except OSError:
                    pass
            await asyncio.sleep(5)

    async def operation(self, record, work):
        self.operation_records[record['id']]=record
        while len(self.operation_records)>30:self.operation_records.pop(next(iter(self.operation_records)))
        try:
            record['state'] = 'running'
            await self.db('operation', item=record)
            record['message'] = await work()
            record['state'] = 'succeeded'
        except asyncio.CancelledError:
            record.update(state='interrupted', message='Panel shutdown interrupted this operation.')
            raise
        except Exception as exc:
            record.update(state='failed', message=redact(str(exc)))
        finally:
            record['finished'] = time.time()
            self.busy = False
            await self.db('operation', item=record)
            self.publish('operation')

    async def diagnostic(self, check):
        if check == 'configuration':
            from activity.run_stack import configuration
            await asyncio.to_thread(configuration, self.environment)
            return 'Bot identity, environment, runtime files and tunnel executable validated.'
        if check == 'storage':
            directory = BOT/'control_panel'/'data'/self.environment
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b'panel-write-check'); probe.flush()
            if psutil.disk_usage(str(directory)).free < 300*1024*1024:
                raise RuntimeError('Less than 300 MiB free disk space; free storage before continuing.')
            return 'Panel data directory is writable and disk has space for the bounded history budget.'
        if check == 'auth':
            try:
                from wwm_auth.context import load
                await asyncio.to_thread(load)
            except Exception:
                raise RuntimeError('Saved-login context is unavailable or invalid. Check the host setup; no network request was made.') from None
            return 'Saved-login context passed local validation. No login was attempted and expiry remains unknown.'
        if check == 'assets':
            required = [BOT/'activity/public/app.js', BOT/'activity/public/catalogue.json', PUBLIC/'index.html']
            if not all(path.is_file() and path.stat().st_size for path in required):
                raise RuntimeError('Required runtime asset missing or empty.')
            return 'Activity catalogue, frontend and panel bundle are present.'
        if check == 'api':
            # Existing CLI performs a bounded login and protected own-character lookup.
            env = dict(self.supervisor.env, WWM_PANEL_SOURCE='diagnostic')
            proc = await asyncio.create_subprocess_exec(sys.executable, '-B', '-m', 'wwm_auth', '--fetch',
                cwd=BOT, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 90)
            except asyncio.TimeoutError:
                proc.kill(); await proc.wait()
                raise RuntimeError('API validation exceeded 90 seconds; child stopped.') from None
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill(); await proc.wait()
                raise
            if proc.returncode:
                raise RuntimeError('API validation failed. Check local saved-login setup; no credential values were captured.')
            return 'Saved-login retrieval and protected character validation passed. Token expiry remains unknown.'
        raise RuntimeError('Unknown diagnostic')

import sys

def create_app(panel):
    @web.middleware
    async def security(request, handler):
        try:
            if request.path == '/internal/ingest':
                if request.remote not in ('127.0.0.1', '::1') or not secrets.compare_digest(
                    request.headers.get('Authorization', ''), 'Bearer '+panel.secret):
                    raise web.HTTPForbidden(text='Ingestion denied')
            else:
                login = str(make_header(decode_header(request.headers.get('Tailscale-User-Login', ''))))
                if not secrets.compare_digest(login, panel.owner):
                    raise web.HTTPForbidden(text='Use your authorized private Tailscale URL.')
                if request.host != panel.hostname + ':8443':
                    raise web.HTTPForbidden(text='Untrusted host')
                if request.method not in ('GET', 'HEAD'):
                    if request.headers.get('Origin') != panel.origin or not secrets.compare_digest(
                        request.headers.get('X-CSRF-Token', ''), panel.csrf):
                        raise web.HTTPForbidden(text='Invalid request origin or CSRF token')
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.text}, status=exc.status)
        except HistoryBusy as exc:
            response=web.json_response({'error':str(exc),'code':'history_busy','retryable':True},status=503,headers={'Retry-After':'2'})
        except (ValueError, TypeError, json.JSONDecodeError):
            response = web.json_response({'error': 'Invalid request'}, status=400)
        except Exception:
            response = web.json_response({'error': 'Panel operation failed; check disk space and local runtime.'}, status=503)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"})
        return response

    app = web.Application(middlewares=[security], client_max_size=1024*1024)
    async def status(request):
        return web.json_response(dict(panel.status, csrf=panel.csrf, busy=panel.busy, storage_error=panel.storage_error,
            performance=dict(panel.history.performance(),writer_queue=panel.pending.qsize(),health_sample_delay_ms=panel.health_delay_ms),
            indexing=panel.indexing_cache,storage=panel.storage_cache))

    async def read(request, method, *args, **kwargs):
        task=asyncio.create_task(panel.history.read(method,*args,**kwargs))
        try:
            while not task.done():
                await asyncio.wait({task},timeout=.1)
                if request.transport is None or request.transport.is_closing():
                    task.cancel()
                    raise asyncio.CancelledError()
            return await task
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def listing(request):
        kind = {'requests': 'request', 'logs': 'log', 'metrics': 'metric','commands':'command'}[request.match_info['collection']]
        result = await read(request,'query', kind, before=request.query.get('before'), limit=request.query.get('limit', 100),
            summary=request.query.get('summary')=='true',
            search=request.query.get('q', ''), source=request.query.get('source', ''), level=request.query.get('level', ''),
            since=max(float(request.query.get('since', 0)), time.time()-(14*86400 if kind == 'metric' else 72*3600)),
            **request_filters(request))
        return web.json_response(result)

    def request_filters(request):
        return {key:request.query[key] for key in ('until','outcome','http_status','min_duration','retried','endpoint_name') if key in request.query}

    async def analytics(request):
        dates={'range_seconds':request.query['range_seconds']} if 'range_seconds' in request.query else {'since':float(request.query.get('since',0))}
        if 'range_seconds' in dates and ('since' in request.query or 'until' in request.query):raise web.HTTPBadRequest(text='Choose a range or explicit dates.')
        return web.json_response(await read(request,'analytics',**dates,
            search=request.query.get('q',''),source=request.query.get('source',''),**request_filters(request)))

    async def command_summary(request):
        since=float(request.query.get('since',0))
        if 'range_seconds' in request.query:
            seconds=int(request.query['range_seconds'])
            if seconds not in (900,3600,86400,259200):raise web.HTTPBadRequest(text='Invalid range')
            since=int(time.time()//10)*10-seconds
        return web.json_response(await read(request,'command_summary',since=since))

    async def investigation(request):
        result = await read(request,'investigation',request.match_info['id'])
        if result is None:
            raise web.HTTPNotFound()
        return web.json_response(result)

    async def jobs(request):
        beat = panel.heartbeats.get('bot')
        return web.json_response({'items':(beat['data'].get('tasks',[]) if beat else []),
            'fresh':bool(beat and time.time()-beat['time']<=15), 'history':await read(request,'jobs')})

    async def incidents(request):
        return web.json_response({'items':await read(request,'incidents')})

    async def acknowledge(request):
        body = await request.json()
        note = body.get('note','')
        if not isinstance(note,str) or len(note)>280:
            raise web.HTTPBadRequest(text='Notes must be at most 280 characters.')
        if not await panel.db('acknowledge',request.match_info['id'],redact(note)):
            raise web.HTTPNotFound()
        panel.publish('update')
        return web.json_response({'acknowledged':True,'time':time.time(),'note':redact(note)})

    async def detail(request):
        item = await read(request,'request', request.match_info['id'])
        if not item:
            raise web.HTTPNotFound()
        return web.json_response(item)

    async def ingest(request):
        body = await request.json()
        events = body.get('events', [])
        if not isinstance(events, list) or len(events) > 64:
            raise web.HTTPBadRequest()
        for event in events:
            if not isinstance(event, dict) or event.get('environment') != panel.environment:
                raise web.HTTPBadRequest()
            if event.get('version',1) not in (1,2):
                raise web.HTTPBadRequest(text='Unsupported telemetry version')
            if event.get('source') not in ('bot', 'activity', 'diagnostic') or event.get('kind') not in ('log', 'request', 'heartbeat','job','command'):
                raise web.HTTPBadRequest()
            if not isinstance(event.get('data'), dict) or not isinstance(event.get('time'), (int, float)):
                raise web.HTTPBadRequest()
            if not math.isfinite(event['time']):
                raise web.HTTPBadRequest()
            if event['kind']=='job' and (not isinstance(event['data'].get('name'),str) or len(event['data']['name'])>100):
                raise web.HTTPBadRequest()
            if len(json.dumps(event).encode()) > 64*1024:
                raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=len(json.dumps(event)))
            # Only the exact child process launched by this panel may update health.
            process = panel.supervisor.children.get(event['source'])
            if event['kind'] == 'heartbeat':
                if process and panel.supervisor.matches_process(event['source'], event.get('pid'), event.get('instance')):
                    event['time'] = time.time()
                    panel.heartbeats[event['source']] = event
            else:
                panel.enqueue(event)
            panel.dropped[event['source']] = max(0, int(body.get('dropped', 0)))
        return web.json_response({'accepted': len(events)})

    async def start_operation(request):
        if panel.busy:
            raise web.HTTPConflict(text='Another operation is in progress.')
        body = await request.json()
        if request.match_info.get('service'):
            service, action = request.match_info['service'], request.match_info['action']
            if service not in ('stack','bot','activity','tunnel') or action not in ('start','stop','restart'):
                raise web.HTTPNotFound()
            if panel.environment == 'production' and action in ('stop','restart') and body.get('confirmation') != panel.hostname + ' production':
                raise web.HTTPBadRequest(text='Confirm the production machine and environment.')
            work = lambda: panel.supervisor.action(service, action)
            target = service + '/' + action
        else:
            check = request.match_info['check']
            if check not in ('configuration', 'assets', 'api','storage','auth'):
                raise web.HTTPNotFound()
            work = lambda: panel.diagnostic(check)
            target = 'diagnostic/' + check
        record = {'id': uuid.uuid4().hex, 'state': 'queued', 'started': time.time(), 'environment': panel.environment,
                  'identity': panel.owner, 'target': target, 'message': 'Queued'}
        panel.busy = True
        try:
            await panel.db('operation', item=record)
        except Exception:
            panel.busy = False; raise
        panel.operation_task = asyncio.create_task(panel.operation(record, work))
        return web.json_response(record, status=202)

    async def operation(request):
        item=panel.operation_records.get(request.match_info['id'])
        if item is None:item=await read(request,'operation',key=request.match_info['id'])
        if not item:
            raise web.HTTPNotFound()
        return web.json_response(item)

    async def export(request):
        data = {'status': panel.status, 'requests': await read(request,'query', 'request', limit=100),
                'logs': await read(request,'query', 'log', limit=100)}
        # Identity and CSRF/ingestion secrets are not part of support exports.
        data['status'] = {key: value for key, value in panel.status.items() if key not in ('operations','incidents')}
        return web.json_response(data, headers={'Content-Disposition':'attachment; filename="wwm-support.json"'})

    async def events(request):
        if len(panel.subscribers) >= 8:
            raise web.HTTPServiceUnavailable(text='Too many live connections')
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control':'no-store',
            'X-Accel-Buffering':'no', 'X-Content-Type-Options':'nosniff'})
        await response.prepare(request)
        queue = asyncio.Queue(8); panel.subscribers.add(queue)
        try:
            while True:
                try:
                    kind = await asyncio.wait_for(queue.get(), 10)
                except asyncio.TimeoutError:
                    kind = 'ping'
                await response.write(('event: '+kind+'\ndata: {}\n\n').encode())
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            panel.subscribers.discard(queue)
        return response

    async def static(request):
        relative = request.match_info['path'] or 'index.html'
        path = (PUBLIC/relative).resolve()
        if not path.is_relative_to(PUBLIC.resolve()) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app.router.add_post('/internal/ingest', ingest)
    app.router.add_get('/api/status', status)
    app.router.add_get('/api/analytics',analytics)
    app.router.add_get('/api/commands/summary',command_summary)
    app.router.add_get('/api/jobs',jobs)
    app.router.add_get('/api/incidents',incidents)
    app.router.add_post('/api/incidents/{id}/acknowledge',acknowledge)
    app.router.add_get('/api/{collection:requests|logs|metrics|commands}', listing)
    app.router.add_get('/api/requests/{id}/investigation',investigation)
    app.router.add_get('/api/requests/{id}', detail)
    app.router.add_get('/api/events', events)
    app.router.add_post('/api/services/{service}/{action}', start_operation)
    app.router.add_post('/api/diagnostics/{check}', start_operation)
    app.router.add_get('/api/operations/{id}', operation)
    app.router.add_get('/api/export', export)
    app.router.add_get('/{path:.*}', static)

    async def startup(app):
        panel.tasks.extend([asyncio.create_task(panel.writer()), asyncio.create_task(panel.sample()), asyncio.create_task(panel.legacy()),
                            asyncio.create_task(panel.operational()),asyncio.create_task(panel.tailscale_checks())])
        if getattr(panel, 'start_stack', False):
            record = {'id': uuid.uuid4().hex, 'state': 'queued', 'started': time.time(),
                      'environment': panel.environment, 'identity': 'local launcher',
                      'target': 'stack/start', 'message': 'Requested by manual combined launcher'}
            panel.busy = True
            await panel.db('operation', item=record)
            panel.operation_task = asyncio.create_task(panel.operation(record, lambda: panel.supervisor.action('stack', 'start')))
    async def cleanup(app):
        if panel.operation_task:
            panel.operation_task.cancel()
            await asyncio.gather(panel.operation_task, return_exceptions=True)
        for task in panel.tasks:
            task.cancel()
        await asyncio.gather(*panel.tasks, return_exceptions=True)
        await panel.supervisor.close()
        await panel.history.close()
        await panel.db('close')
        panel.executor.shutdown(wait=True)
    app.on_startup.append(startup); app.on_cleanup.append(cleanup)
    return app
