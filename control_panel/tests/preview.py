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
            'other_url':'','dropped':{},'operations':[],'authentication':{'mode':'preview only','token_expiry':'Unknown'}})
    async def listing(request):
        kind=request.match_info['collection']
        items=[]
        for index in range(35):
            if kind=='metrics':
                data={'cpu_percent':15+7*math.sin(index/3)}
            elif kind=='logs':
                data={'level':'INFO','module':'preview','message':'UI fixture event — no bot or game connection'}
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
    async def static(request):
        path=(ROOT/(request.match_info['path'] or 'index.html')).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():raise web.HTTPNotFound()
        return web.FileResponse(path)
    app.router.add_get('/api/status',status)
    app.router.add_get('/api/{collection:requests|logs|metrics}',listing)
    app.router.add_get('/api/requests/{id}',detail)
    app.router.add_get('/api/events',events)
    app.router.add_get('/{path:.*}',static)
    return app

if __name__=='__main__':web.run_app(create_app(),host='127.0.0.1',port=8771,access_log=None)
