"""Read-only UI fixture server, never imports credentials or starts services.

python -m control_panel.tests.preview; open http://127.0.0.1:8771
This is deliberately separate from the authenticated production application.
"""
import math
import time
from pathlib import Path
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]/'public'

def create_app():
    app=web.Application()
    async def status(request):
        services={name:{'state':'Healthy','ownership':'external','pid':None,'uptime_seconds':5400,
            'local_health':'Healthy','configured':True,'public_reachability':'Unknown — not externally verified',
            'heartbeat':{'connected':True,'gateway_latency_ms':42,'tasks':[]}}
            for name in ('bot','activity','tunnel')}
        return web.json_response({'environment':'test','hostname':'fixture.preview.ts.net','host_label':'UI preview · fixture data',
            'bot_id':'000000000000000000','sampled_at':time.time(),'csrf':'fixture','busy':True,'services':services,
            'host':{'cpu_percent':14,'memory_percent':36,'memory_used':3*1024**3,'memory_total':8*1024**3,'disk_free':48*1024**3,'temperature_c':None},
            'other_url':'','dropped':{},'operations':[],'incidents':[],
            'authentication':{'mode':'preview only','token_expiry':'Unknown'}})
    async def listing(request):
        kind=request.match_info['collection']
        items=[]
        for index in range(35):
            if kind=='metrics':
                data={'cpu_percent':15+7*math.sin(index/3)}
            elif kind=='logs':
                data={'level':'INFO','module':'preview','message':'UI fixture event — no bot or game connection'}
            elif kind=='commands':
                data={'id':'fixture-command','name':'player','type':'application','outcome':'success','duration_ms':125}
            else:
                data={'id':str(index),'parent_id':None,'endpoint':'/fixture/player','method':'POST','duration_ms':100+45*math.sin(index/3),
                    'status':200,'outcome':'success','application_code':0,'attempts':[{'number':1,'status':200,'duration_ms':112}],
                    'request':{'page':1},'response':{'code':0,'result':{'score':123}},'request_bytes':25,'response_bytes':125}
            items.append({'cursor':35-index,'time':time.time()-index*60,'source':'bot','kind':kind,'data':data})
        return web.json_response({'items':items,'next_cursor':None})
    async def detail(request):
        return web.json_response({'cursor':1,'time':time.time(),'source':'bot','data':{'id':request.match_info['id'],
            'endpoint':'/fixture/player','method':'POST','status':200,'outcome':'success','duration_ms':112,
            'request':{'page':1},'response':{'code':0,'result':{'score':123}},'attempts':[{'number':1,'status':200}]}})
    async def events(request):
        raise web.HTTPServiceUnavailable(text='Preview uses polling')
    async def analytics(request):
        return web.json_response({'summary':{'total':35,'success':35,'error':0,'unknown':0,'retries':0,'timeouts':0,
            'activity_requests':0,'median_ms':110,'p95_ms':145,'success_rate':1,'error_rate':0,'unknown_rate':0},
            'series':[{'time':time.time()-index*60,'count':1,'errors':0,'mean_ms':100+45*math.sin(index/3)} for index in range(35)],
            'endpoints':[{'endpoint':'/fixture/player','label':'Player lookup (fixture)','count':35,'errors':0}]})
    async def jobs(request):
        return web.json_response({'fresh':True,'history':[],'items':[{'name':'fixture.guild_monitor','instrumented':True,'running':True,
            'phase':'completed','last_success':time.time()-30,'last_start':time.time()-31,'duration_ms':125,'consecutive_failures':0,'next_run':time.time()+30}]})
    async def command_summary(request):
        return web.json_response({'total':35,'success':35,'errors':0,'p95_ms':125,
                                 'slowest':[{'name':'player','count':35,'errors':0,'mean_ms':125,'max_ms':125}]})
    async def investigation(request):
        return web.json_response({'root_id':'fixture-command','events':[{'kind':'command','source':'bot','time':time.time()-1,
            'data':{'id':'fixture-command','name':'player','outcome':'success','duration_ms':125}},
            {'kind':'request','source':'bot','time':time.time(),'data':{'id':request.match_info['id'],'endpoint':'/fixture/player',
            'outcome':'success','duration_ms':112,'attempts':[{'number':1,'status':200,'duration_ms':112}]}}],
            'logs':[{'kind':'log','source':'bot','time':time.time(),'data':{'level':'INFO','message':'Safe fixture event'}}],'truncated':False})
    async def static(request):
        path=(ROOT/(request.match_info['path'] or 'index.html')).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():raise web.HTTPNotFound()
        return web.FileResponse(path)
    app.router.add_get('/api/status',status)
    app.router.add_get('/api/analytics',analytics)
    app.router.add_get('/api/jobs',jobs)
    app.router.add_get('/api/commands/summary',command_summary)
    app.router.add_get('/api/requests/{id}/investigation',investigation)
    app.router.add_get('/api/{collection:requests|logs|metrics|commands}',listing)
    app.router.add_get('/api/requests/{id}',detail)
    app.router.add_get('/api/events',events)
    app.router.add_get('/{path:.*}',static)
    return app

if __name__=='__main__':web.run_app(create_app(),host='127.0.0.1',port=8771,access_log=None)
