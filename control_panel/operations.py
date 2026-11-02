"""Independent API/action operations, using the existing audit and live transport."""
import asyncio
import json
import os
import sys
import time
import uuid
import subprocess
import psutil
from aiohttp import web,ClientSession,ClientTimeout
from . import tools
from .supervisor import BOT

def install(app,panel):
    manager=Operations(panel)
    app.router.add_get('/api/tools',manager.catalogue)
    app.router.add_post('/api/tools/{id}/run',manager.run)
    app.router.add_post('/api/operations/{id}/cancel',manager.cancel)
    app.router.add_get('/api/presets',manager.presets)
    app.router.add_post('/api/presets',manager.save_preset)
    app.router.add_delete('/api/presets/{id}',manager.delete_preset)
    app.router.add_get('/api/tools/{id}/results',manager.results)
    app.router.add_get('/api/tool-results/{id}',manager.result)
    app.router.add_get('/api/tool-results/{id}/compare/{other}',manager.compare)
    app.router.add_get('/api/actions',manager.actions)
    app.router.add_post('/api/actions/{id}/{mode:preview|execute|check}',manager.action)
    app.router.add_get('/api/support/preview',manager.support_preview)
    app.router.add_get('/api/support/download',manager.support_download)
    async def cleanup(app):await manager.close()
    app.on_cleanup.append(cleanup)
    return manager

class Operations:
    def __init__(self,panel):self.panel=panel;self.tasks={};self.api_active=False;self.action_active=False

    async def start(self,target,work):
        p=self.panel
        record=dict(id=uuid.uuid4().hex,state='queued',started=time.time(),environment=p.environment,identity=p.owner,target=target,message='Queued')
        await p.db('operation',item=record)
        p.operation_records[record['id']]=record
        async def execute():
            try:
                record.update(state='running',message='Working…');p.publish('operation')
                result=await work(record)
                record.update(state=result.pop('_state','succeeded'),message=result.pop('message','Complete'),**result)
            except asyncio.CancelledError:
                record.update(state='cancelled' if target.startswith('tool/') else 'unknown',message='Operation stopped. An already-sent request cannot be recalled.')
            except Exception:
                record.update(state='unknown' if target.endswith('/execute') else 'failed',message='The operation could not be verified. Check host setup and target availability; it was not automatically retried.')
            finally:
                record['finished']=time.time()
                try:await p.db('operation',item={k:v for k,v in record.items() if k!='preview'})
                finally:p.publish('operation');self.tasks.pop(record['id'],None)
        task=asyncio.create_task(execute());self.tasks[record['id']]=task
        # Cancellation can happen before the coroutine has entered its try/finally.
        def released(done):
            if target.startswith('tool/'):self.api_active=False
            else:self.action_active=False
        task.add_done_callback(released)
        for key,item in list(p.operation_records.items()):
            if len(p.operation_records)<=100:break
            if item.get('state') not in ('queued','running'):p.operation_records.pop(key,None)
        return web.json_response(record,status=202)

    async def catalogue(self,request):return web.json_response(tools.catalogue())

    async def run(self,request):
        if self.api_active:raise web.HTTPConflict(text='A lookup is already running on this machine. Wait or cancel it first.')
        body=await request.json();tool=request.match_info['id']
        try:inputs=tools.validate(tool,body.get('inputs',{}))
        except (ValueError,TypeError) as exc:raise web.HTTPBadRequest(text=str(exc)) from None
        self.api_active=True
        async def work(record):
            process=None
            try:
                env=dict(self.panel.supervisor.env)
                env.pop('WWM_PANEL_INGEST_SECRET',None) # result pipe is the sole worker transport
                flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                process=await asyncio.create_subprocess_exec(sys.executable,'-B','-m','control_panel.tool_worker',cwd=BOT,env=env,
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=1024*1024,**flags)
                payload=json.dumps(dict(tool=tool,inputs=inputs,id=record['id'],environment=self.panel.environment)).encode()
                try:
                    stdout,_=await asyncio.wait_for(process.communicate(payload),30)
                except asyncio.TimeoutError:
                    return dict(_state='failed',message='Lookup exceeded 30 seconds. The worker was stopped; no automatic retry will run.')
                if process.returncode or len(stdout)>512*1024:raise RuntimeError('Invalid worker result')
                result=json.loads(stdout)
                for event in result.pop('events',[]):self.panel.enqueue(event)
                item=dict(result,id=record['id'],tool=tool,time=time.time(),environment=self.panel.environment)
                await self.panel.db('tool_result',record['id'],item)
                return dict(_state='failed' if result['outcome']=='error' else 'succeeded',message=result['message'],result_id=record['id'])
            finally:
                if process and process.returncode is None:
                    # Windows venv launchers can forward to another interpreter.
                    # Stop only descendants of this still-owned worker handle.
                    try:
                        children=psutil.Process(process.pid).children(recursive=True)
                        for child in reversed(children):
                            try:child.kill()
                            except psutil.Error:pass
                    except (psutil.Error,AttributeError):pass
                    process.kill();await process.wait()
        try:return await self.start('tool/'+tool,work)
        except BaseException:self.api_active=False;raise

    async def cancel(self,request):
        key=request.match_info['id'];record=self.panel.operation_records.get(key)
        if not record or not record['target'].startswith('tool/'):raise web.HTTPConflict(text='Only active API lookups support cancellation.')
        task=self.tasks.get(key)
        if task:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.tasks.pop(key,None)
            if record['state'] in ('queued','running'):
                record.update(state='cancelled',finished=time.time(),message='Lookup cancelled. An already-sent request cannot be recalled.')
                await self.panel.db('operation',item=record)
        return web.json_response(record)

    async def presets(self,request):return web.json_response({'items':await self.panel.db('presets')})
    async def save_preset(self,request):
        body=await request.json();name=body.get('name','')
        if not isinstance(name,str) or not name.strip() or len(name)>80:raise web.HTTPBadRequest(text='Give the preset a name of 1–80 characters.')
        try:inputs=tools.validate(body.get('tool',''),body.get('inputs',{}))
        except ValueError as exc:raise web.HTTPBadRequest(text=str(exc)) from None
        return web.json_response(await self.panel.db('save_preset',name.strip(),body['tool'],inputs),status=201)
    async def delete_preset(self,request):
        if not await self.panel.db('delete_preset',request.match_info['id']):raise web.HTTPNotFound()
        return web.json_response({'deleted':True})
    async def results(self,request):return web.json_response({'items':await self.panel.history.read('tool_results',request.match_info['id'])})
    async def result(self,request):
        item=await self.panel.history.read('tool_result',request.match_info['id'])
        if item is None:raise web.HTTPNotFound(text='This result is no longer retained.')
        return web.json_response(item)
    async def compare(self,request):
        left=await self.panel.history.read('tool_result',request.match_info['id']);right=await self.panel.history.read('tool_result',request.match_info['other'])
        if not left or not right:raise web.HTTPNotFound(text='Both results must still be retained.')
        if left['tool']!=right['tool']:raise web.HTTPBadRequest(text='Choose two results from the same tool.')
        return web.json_response(dict(left={k:left[k] for k in ('id','time','environment')},right={k:right[k] for k in ('id','time','environment')},changes=tools.compare(left['response'],right['response'])))

    def bridge_port(self):
        p=self.panel;beat=p.heartbeats.get('bot');state=p.status.get('services',{}).get('bot',{})
        if p.busy:raise web.HTTPConflict(text='Wait for the current service operation or diagnostic before updating messages.')
        if not beat or time.time()-beat['time']>15 or not beat['data'].get('connected') or state.get('ownership')!='owned' or not p.supervisor.matches_process('bot',beat.get('pid'),beat.get('instance')):
            raise web.HTTPConflict(text='Start a managed, connected bot before running message actions.')
        port=beat['data'].get('action_port')
        if not isinstance(port,int) or not 1024<=port<=65535:raise web.HTTPConflict(text='Restart the managed bot to enable approved message actions.')
        return port

    async def bridge(self,body=None):
        port=self.bridge_port()
        async with ClientSession(timeout=ClientTimeout(total=35),trust_env=False) as http:
            async with http.request('POST' if body else 'GET',f'http://127.0.0.1:{port}/actions',json=body,headers={'Authorization':'Bearer '+self.panel.secret},allow_redirects=False) as response:
                if response.content_length and response.content_length>128*1024:raise RuntimeError('Bridge response too large')
                raw=await response.content.read(128*1024+1)
                if len(raw)>128*1024:raise RuntimeError('Bridge response too large')
                value=json.loads(raw)
                if response.status!=200:raise web.HTTPConflict(text=value.get('error','Bot action unavailable.'))
                return value

    async def actions(self,request):
        try:return web.json_response(dict(await self.bridge(),available=True))
        except web.HTTPException as exc:return web.json_response(dict(items=[],available=False,reason=exc.text))
        except Exception:return web.json_response(dict(items=[],available=False,reason='The bot action connection is unavailable.'))

    async def action(self,request):
        action=request.match_info['id'];mode=request.match_info['mode'];body=await request.json()
        if self.action_active and mode!='execute':raise web.HTTPConflict(text='Another message action is in progress.')
        if mode=='execute':
            key=body.get('preview_id','');saved=await self.panel.db('action_preview',key)
            if not saved or saved['action']!=action:raise web.HTTPConflict(text='Prepare a preview for this action first.')
            if saved.get('operation_id'):
                item=await self.panel.db('operation',key=saved['operation_id']);return web.json_response(item,status=202)
            if saved['expires']<=time.time():raise web.HTTPConflict(text='Preview expired. Prepare a new preview.')
            if body.get('confirmed') is not True:raise web.HTTPBadRequest(text='Confirm the exact preview before publishing.')
            if self.panel.environment=='production' and body.get('confirmation')!=self.panel.hostname:raise web.HTTPBadRequest(text='Type the production machine name to confirm.')
        self.bridge_port()
        if self.action_active:raise web.HTTPConflict(text='Another message action is in progress.')
        self.action_active=True
        async def work(record):
            if mode=='execute':
                saved['operation_id']=record['id'];await self.panel.db('action_preview',key,saved)
            result=await self.bridge(dict(action=action,mode=mode,preview_id=body.get('preview_id'),operation_id=record['id']))
            if mode=='preview':
                # Only target metadata persists; content remains ephemeral in bot RAM.
                meta={k:v for k,v in result.items() if k!='content'}
                await self.panel.db('action_preview',result['id'],meta)
                record['preview']=result
                return dict(message='Preview ready. Nothing has been sent to Discord.')
            return dict(_state=result.get('state','succeeded'),message=result.get('message','Target checked.'),message_url=result.get('message_url'))
        try:return await self.start('action/'+action+'/'+mode,work)
        except BaseException:self.action_active=False;raise

    async def support_preview(self,request):
        return web.json_response(dict(includes=['Current health and performance counters','Up to 100 sanitized request summaries','Up to 100 sanitized log events','Up to 50 incidents without notes'],excludes=['Credentials and saved-login material','Presets and raw tool inputs','Posting previews and message bodies','Owner identity and CSRF/ingestion secrets']))

    async def support_download(self,request):
        p=self.panel
        status={k:v for k,v in p.status.items() if k not in ('operations','incidents')}
        logs=await p.history.read('query','log',limit=100)
        requests=await p.history.read('query','request',limit=100,summary=True)
        incidents=[{k:v for k,v in i.items() if k not in ('note','context')} for i in p.operational_cache['incidents'][:50]]
        return web.json_response(dict(status=status,performance=p.history.performance(),requests=requests,logs=logs,incidents=incidents),headers={'Content-Disposition':'attachment; filename="wwm-support.json"'})

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
