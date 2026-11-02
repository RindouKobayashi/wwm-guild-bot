import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
from aiohttp.test_utils import TestClient,TestServer
from control_panel.tools import validate,load_profiles,compare
from control_panel.tool_worker import PublicResolver,ToolFailure,bounded_body,public_address,execute
from control_panel.storage import Store
from control_panel.server import Panel,create_app
from control_panel.action_bridge import Bridge,serialized,lock_for

class ToolTests(unittest.TestCase):
    def test_guided_validation_and_allowlist(self):
        self.assertEqual(validate('player',{'number_id':'00123'})['number_id'],'00123')
        for key,value in [('player',{'number_id':'abc'}),('player',{'number_id':'1','token':'x'}),('rankings',{'rank':'arbitrary'}),('rankings',{'page':'101'}),('combat',{'number_id':'1','hostnum':'99999'})]:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate(key,value)
    def profile(self):return dict(id='test',origin='https://api.example.com',paths=['/status'],response_fields=['status','data','count'],query_fields=['page'],methods=['GET'])
    def test_profile_requires_exact_origin_paths_and_privacy(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'profiles.json';profile=self.profile()
            path.write_text(json.dumps([profile]));self.assertIn('test',load_profiles(path))
            for change in [dict(origin='http://api.example.com'),dict(origin='https://user:pass@api.example.com'),dict(paths=['//another.example']),dict(response_fields=[]),dict(response_fields=['token']),dict(methods=['POST']),dict(response_format='html'),dict(header_fields=['Host'])]:
                path.write_text(json.dumps([dict(profile,**change)]))
                with self.subTest(change=change),self.assertRaises(ValueError):load_profiles(path)
    def test_custom_inputs_cannot_override_destination_or_credentials(self):
        profiles={'test':self.profile()}
        self.assertEqual(validate('custom-test',{'query':{'page':2}},profiles)['query'],{'page':2})
        for value in [dict(url='https://other.example'),dict(path='/other'),dict(method='POST'),dict(header={'Authorization':'secret'}),dict(query={'token':'secret'}),dict(body={'count':1})]:
            with self.subTest(value=value),self.assertRaises(ValueError):validate('custom-test',value,profiles)
    def test_comparison_marks_omissions_unavailable(self):
        rows=compare({'a':1,'b':2,'partial':{'truncated':True}},{'a':3,'c':4,'partial':{'value':5}})
        self.assertEqual({r['change'] for r in rows},{'changed','removed','added','unavailable'})
    def test_reserved_numeric_addresses_are_blocked(self):
        for address in ['127.0.0.1','10.1.2.3','169.254.169.254','::1','::ffff:127.0.0.1','224.0.0.1','0.0.0.0']:
            self.assertFalse(public_address(address),address)
        self.assertTrue(public_address('8.8.8.8'))

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def custom(self,status=200,raw=b'{"status":"ok","count":3}',headers=None):
        async def chunks(_):
            if status==0:await asyncio.sleep(10)
            yield raw
        response=SimpleNamespace(status=status or 200,headers=headers or {},content=SimpleNamespace(iter_chunked=chunks))
        context=MagicMock();context.__aenter__=AsyncMock(return_value=response);context.__aexit__=AsyncMock(return_value=False)
        http=SimpleNamespace(request=MagicMock(return_value=context,side_effect=asyncio.TimeoutError if status==-1 else None))
        session=MagicMock();session.__aenter__=AsyncMock(return_value=http);session.__aexit__=AsyncMock(return_value=False)
        profile=dict(id='test',origin='https://api.example.com',paths=['/status'],response_fields=['status','count','data'],methods=['GET'],credential_headers={'Authorization':'FIXTURE_CREDENTIAL'})
        with patch('control_panel.tool_worker.load_profiles',return_value={'test':profile}),patch('control_panel.tool_worker.aiohttp.ClientSession',return_value=session) as factory,patch.dict(os.environ,FIXTURE_CREDENTIAL='very-private-fixture'):
            result=await execute('custom-test',{},'operation','test')
        self.assertFalse(factory.call_args.kwargs['trust_env']);self.assertFalse(http.request.call_args.kwargs['allow_redirects'])
        return result
    async def test_deadline_keeps_partial_attempts_and_rate_retry_is_bounded(self):
        result=await asyncio.wait_for(self.custom(status=0),.02)
        self.assertEqual(result['code'],'timeout');self.assertEqual(len(result['events']),1)
        self.assertEqual(result['events'][0]['data']['outcome'],'error');self.assertEqual(result['attempts'][0]['error'],'TimeoutError')
        result=await self.custom(status=429,headers={'Retry-After':'0'})
        self.assertEqual(result['code'],'rate_limited');self.assertEqual(len(result['attempts']),2)
        result=await self.custom(status=-1)
        self.assertEqual(result['code'],'timeout');self.assertEqual(len(result['attempts']),2)
    async def test_custom_success_sanitizes_credentials_and_records_correlation(self):
        result=await self.custom(raw=b'{"status":"very-private-fixture","token":"very-private-fixture","count":3,"chat":"private-chat"}')
        self.assertEqual(result['outcome'],'success');self.assertEqual(result['response']['count'],3)
        self.assertNotIn('very-private-fixture',json.dumps(result));self.assertNotIn('private-chat',json.dumps(result))
        self.assertEqual(result['events'][0]['data']['parent_id'],'operation');self.assertEqual(result['events'][0]['source'],'api_tools_custom')
    async def test_custom_failures_are_distinct(self):
        for status,raw,code in [(401,b'{}','auth_rejected'),(429,b'{}','rate_limited'),(302,b'{}','redirect_blocked'),(500,b'{}','http_error'),(200,b'not JSON','decode_failure'),(200,b'{"code":2,"status":"very-private-fixture"}','application_error')]:
            with self.subTest(status=status,code=code):
                result=await self.custom(status,raw);self.assertEqual(result['code'],code);self.assertNotIn('very-private-fixture',json.dumps(result))
    async def test_real_child_returns_safe_missing_auth_without_network(self):
        import sys
        env=dict(os.environ,WWM_TOKEN='',WWM_AUTH_MODE='static',SSLKEYLOGFILE='',GITHUB_BRANCH='test')
        env.pop('WWM_PANEL_INGEST_SECRET',None)
        child=await asyncio.create_subprocess_exec(sys.executable,'-B','-m','control_panel.tool_worker',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
        try:
            raw,_=await asyncio.wait_for(child.communicate(json.dumps(dict(tool='player',inputs={'number_id':'123'},id='fixture',environment='test')).encode()),10)
            result=json.loads(raw);self.assertEqual(result['code'],'auth_missing');self.assertEqual(child.returncode,0)
        finally:
            if child.returncode is None:child.kill();await child.wait()
    async def test_dns_checks_all_answers_and_pins_addresses(self):
        loop=asyncio.get_running_loop()
        with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',443))])):
            with self.assertRaises(ToolFailure):await PublicResolver().resolve('public.example',443)
        with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])):
            self.assertEqual((await PublicResolver().resolve('public.example',443))[0]['host'],'8.8.8.8')
    async def test_decoded_body_is_bounded(self):
        async def chunks(size):
            for _ in range(size):yield b'x'*32768
        response=SimpleNamespace(content=SimpleNamespace(iter_chunked=lambda _:chunks(65)))
        with self.assertRaises(ToolFailure):await bounded_body(response)
    async def test_missing_host_auth_does_not_login(self):
        transport=SimpleNamespace(WWM_TOKEN='',WWM_AUTH_MODE='static')
        wwm=SimpleNamespace()
        with patch.dict('sys.modules',{'settings':transport,'utility.wwm':wwm}):
            result=await execute('player',{'number_id':'123'},'op','test')
        self.assertEqual(result['code'],'auth_missing');self.assertEqual(result['events'],[])

class WorkspaceTests(unittest.TestCase):
    def test_custom_requests_do_not_change_wwm_health_or_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'history.db');now=time.time()
            events=[dict(kind='request',time=now,source='api_tools',data=dict(id='wwm',endpoint='/player',status=200,outcome='success',duration_ms=10))]
            events += [dict(kind='request',time=now+n,source='api_tools_custom',data=dict(id='custom'+str(n),endpoint='/other',status=401,outcome='error',duration_ms=300)) for n in range(3)]
            store.append(events);summary=store.analytics(since=now-1,until=now+4)['summary']
            self.assertEqual(summary['total'],1);self.assertEqual(summary['custom_requests'],3);self.assertEqual(summary['p95_ms'],10)
            self.assertFalse(store.recent_auth()['rejected']);store.close()
    def test_migration_presets_result_retention_and_restart_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'history.db';store=Store(path)
            for n in range(50):store.save_preset('Lookup '+str(n),'player',{'number_id':str(n)})
            with self.assertRaises(ValueError):store.save_preset('overflow','player',{})
            key=store.presets()[0]['id'];self.assertTrue(store.delete_preset(key));self.assertEqual(len(store.presets()),49)
            store.tool_result('old',dict(tool='player',time=time.time()-73*3600,environment='test',response={}))
            self.assertIsNone(store.tool_result('old'))
            store.action_preview('expired',dict(expires=time.time()-1))
            store.db.execute('UPDATE action_previews SET time=?',(time.time()-121,));store.db.commit();store.prune_workspace()
            self.assertIsNone(store.action_preview('expired'))
            store.operation(dict(id='op',state='running',target='action/schedule/execute',started=time.time()))
            store.close();store=Store(path)
            self.assertEqual(store.operation(key='op')['state'],'unknown');self.assertEqual(len(store.presets()),49);store.close()

class ToolsHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.fake=SimpleNamespace(client='123',origin='http://127.0.0.1:8767',logs=Path(self.temp.name),children={},env={},close=AsyncMock(),states=AsyncMock(return_value={}),last={},matches_process=MagicMock(return_value=True))
        with patch('control_panel.server.Supervisor',return_value=self.fake),patch('control_panel.server.BOT',Path(self.temp.name)):
            self.panel=Panel('test','owner@example.test','pc.test.ts.net')
        self.panel.sample=AsyncMock();self.panel.operational=AsyncMock();self.panel.tailscale_checks=AsyncMock()
        self.panel.status={'services':{'bot':{'ownership':'owned'}}}
        self.panel.heartbeats={'bot':dict(time=time.time(),pid=123,instance='owned',data=dict(connected=True,action_port=10001))}
        self.client=TestClient(TestServer(create_app(self.panel)));await self.client.start_server()
        self.headers={'Host':'pc.test.ts.net:8443','Tailscale-User-Login':'owner@example.test'}
        self.post=dict(self.headers,Origin=self.panel.origin,**{'X-CSRF-Token':self.panel.csrf})
    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()
    async def finished(self,key):
        for _ in range(100):
            record=self.panel.operation_records[key]
            if record['state'] not in ('queued','running'):return record
            await asyncio.sleep(.01)
        self.fail('Operation did not finish')
    async def test_new_routes_require_authorization_and_csrf(self):
        for path in ['/api/tools','/api/actions','/api/presets','/api/support/preview','/api/tool-results/x']:
            self.assertEqual((await self.client.get(path)).status,403)
        for path in ['/api/tools/player/run','/api/presets','/api/actions/schedule/preview','/api/operations/x/cancel']:
            self.assertEqual((await self.client.post(path,headers=self.headers,json={})).status,403)
        self.assertEqual((await self.client.delete('/api/presets/x',headers=self.headers)).status,403)
    async def test_preset_roundtrip_and_support_excludes_inputs(self):
        response=await self.client.post('/api/presets',headers=self.post,json=dict(name='Private fixture',tool='player',inputs={'number_id':'987654321'}));self.assertEqual(response.status,201)
        key=(await response.json())['id']
        self.assertEqual(len((await (await self.client.get('/api/presets',headers=self.headers)).json())['items']),1)
        bundle=await (await self.client.get('/api/support/download',headers=self.headers)).text()
        self.assertNotIn('987654321',bundle);self.assertNotIn(self.panel.secret,bundle);self.assertNotIn(self.panel.csrf,bundle)
        self.assertEqual((await self.client.delete('/api/presets/'+key,headers=self.post)).status,200)
    async def test_tool_worker_busy_result_and_cancellation(self):
        process=SimpleNamespace(returncode=None,kill=MagicMock(),wait=AsyncMock())
        gate=asyncio.Event()
        async def communicate(body):
            await gate.wait();process.returncode=0
            return json.dumps(dict(outcome='success',message='Lookup complete.',response={'score':3},events=[],attempts=[])).encode(),b''
        process.communicate=communicate
        with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=process)):
            response=await self.client.post('/api/tools/player/run',headers=self.post,json={'inputs':{'number_id':'123'}})
            self.assertEqual(response.status,202);key=(await response.json())['id']
            self.assertEqual((await self.client.post('/api/tools/player/run',headers=self.post,json={'inputs':{'number_id':'123'}})).status,409)
            self.assertEqual((await self.client.get('/api/status',headers=self.headers)).status,200)
            response=await self.client.post('/api/operations/'+key+'/cancel',headers=self.post,json={})
            self.assertEqual((await response.json())['state'],'cancelled');process.kill.assert_called_once();self.assertFalse(self.panel.workspace.api_active)
    async def test_action_preview_not_persisted_and_execute_idempotent(self):
        preview=dict(id='preview',action='schedule',expires=time.time()+120,content={'content':'private preview fixture'})
        bridge=AsyncMock(side_effect=[preview,dict(state='succeeded',message='Updated',message_url='https://discord.com/channels/1/2/3')])
        with patch.object(self.panel.workspace,'bridge',bridge):
            response=await self.client.post('/api/actions/schedule/preview',headers=self.post,json={});key=(await response.json())['id'];record=await self.finished(key)
            self.assertIn('private preview fixture',json.dumps(record['preview']))
            persisted=await self.panel.db('operation',key=key);self.assertNotIn('private preview fixture',json.dumps(persisted))
            body=dict(preview_id='preview',confirmed=True)
            response=await self.client.post('/api/actions/schedule/execute',headers=self.post,json=body);key=(await response.json())['id'];await self.finished(key)
            response=await self.client.post('/api/actions/schedule/execute',headers=self.post,json=body);self.assertEqual((await response.json())['id'],key)
            self.assertEqual(bridge.await_count,2)
    async def test_stale_external_and_production_confirmation(self):
        self.panel.busy=True
        self.assertEqual((await self.client.post('/api/actions/schedule/preview',headers=self.post,json={})).status,409)
        self.panel.busy=False
        self.panel.heartbeats['bot']['time']=time.time()-16
        self.assertEqual((await self.client.post('/api/actions/schedule/preview',headers=self.post,json={})).status,409)
        self.panel.heartbeats['bot']['time']=time.time();self.panel.status['services']['bot']['ownership']='external'
        self.assertEqual((await self.client.post('/api/actions/schedule/preview',headers=self.post,json={})).status,409)
        self.panel.status['services']['bot']['ownership']='owned';self.panel.environment='production'
        await self.panel.db('action_preview','p',dict(action='schedule',expires=time.time()+120))
        response=await self.client.post('/api/actions/schedule/execute',headers=self.post,json=dict(preview_id='p',confirmed=True))
        self.assertEqual(response.status,400)

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel=SimpleNamespace(fetch_message=AsyncMock())
        self.message=SimpleNamespace(id=3,content='before',embeds=[],components=[],channel=self.channel,guild='Test guild',jump_url='https://discord.com/channels/1/2/3')
        self.message.edit=AsyncMock(return_value=self.message);self.channel.fetch_message.return_value=self.message
        self.cog=type('ScheduleCog',(),{})();self.cog.schedule_message=self.message;self.cog.panel_prepare=AsyncMock(return_value={'content':'after'})
        self.bot=SimpleNamespace(is_ready=lambda:True,cogs={'schedule':self.cog});self.bridge=Bridge(self.bot)
        self.env=patch.dict(os.environ,WWM_PANEL_INGEST_SECRET='bridge-secret');self.env.start()
        from aiohttp import web
        app=web.Application();app.router.add_route('*','/actions',self.bridge.handle)
        self.client=TestClient(TestServer(app));await self.client.start_server();self.headers={'Authorization':'Bearer bridge-secret'}
    async def asyncTearDown(self):await self.client.close();self.env.stop()
    async def post(self,body):return await self.client.post('/actions',headers=self.headers,json=dict(action='schedule',**body))
    async def test_preview_sends_nothing_then_edits_exact_payload_once(self):
        preview=await (await self.post(dict(mode='preview'))).json();self.message.edit.assert_not_called()
        body=dict(mode='execute',preview_id=preview['id'])
        self.assertEqual((await (await self.post(body)).json())['state'],'succeeded')
        self.assertEqual((await (await self.post(body)).json())['state'],'succeeded')
        self.message.edit.assert_awaited_once();kwargs=self.message.edit.call_args.kwargs
        self.assertEqual(kwargs['content'],'after');self.assertFalse(kwargs['allowed_mentions'].everyone);self.assertFalse(kwargs['allowed_mentions'].users)
    async def test_expiry_changed_target_and_missing_message_do_not_publish(self):
        preview=await (await self.post(dict(mode='preview'))).json();self.bridge.previews[preview['id']]['expires']=time.time()-1
        self.assertEqual((await self.post(dict(mode='execute',preview_id=preview['id']))).status,409)
        preview=await (await self.post(dict(mode='preview'))).json();self.message.content='changed'
        self.assertEqual((await self.post(dict(mode='execute',preview_id=preview['id']))).status,409);self.message.edit.assert_not_called()
        self.channel.fetch_message.side_effect=RuntimeError('missing')
        self.assertEqual((await self.post(dict(mode='preview'))).status,409);self.message.edit.assert_not_called()
    async def test_scheduled_lock_blocks_manual_dispatch(self):
        async with lock_for(self.cog):
            task=asyncio.create_task(self.post(dict(mode='preview')));await asyncio.sleep(.02);self.cog.panel_prepare.assert_not_called()
        response=await task;self.assertEqual(response.status,200)
    async def test_untrusted_bridge_request_denied(self):
        self.assertEqual((await self.client.get('/actions')).status,403)

class ActionBuilderTests(unittest.IsolatedAsyncioTestCase):
    def methods(self,file,class_name,names,namespace):
        import ast
        tree=ast.parse((Path(__file__).resolve().parents[2]/'cogs'/file).read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==class_name)
        methods=[n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name in names]
        isolated=ast.Module(body=[ast.ClassDef(name='Subject',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
        ast.fix_missing_locations(isolated);exec(compile(isolated,file,'exec'),namespace)
        return namespace['Subject']()
    async def test_leaderboard_preview_never_assigns_roles_and_scheduler_reuses_builder(self):
        import datetime
        view=SimpleNamespace()
        namespace=dict(serialized=serialized,_LeaderboardInstance=object,discord=SimpleNamespace(utils=SimpleNamespace(utcnow=lambda:datetime.datetime.now(datetime.timezone.utc))),LeaderboardView=MagicMock(return_value=view),BreakingArmyView=MagicMock(return_value=view),logger=MagicMock())
        cog=self.methods('leaderboard_cog.py','LeaderboardCog',{'panel_prepare','_publish_one'},namespace)
        cog._fetch_data=AsyncMock(return_value=([],0));cog._assign_elegance_roles=AsyncMock()
        inst=SimpleNamespace(lb_type='elegance',message=SimpleNamespace(edit=AsyncMock()),channel=SimpleNamespace(send=AsyncMock()))
        self.assertIs((await cog.panel_prepare(inst))['view'],view);cog._assign_elegance_roles.assert_not_called();inst.message.edit.assert_not_called()
        cog._fetch_data.reset_mock();await cog._publish_one(inst)
        cog._fetch_data.assert_awaited_once();cog._assign_elegance_roles.assert_awaited_once();inst.message.edit.assert_awaited_once()
    async def test_schedule_preparation_only_builds_content(self):
        cog=self.methods('schedule_cog.py','ScheduleCog',{'panel_prepare'},{});cog.build_schedule_message=AsyncMock(return_value='Schedule preview')
        self.assertEqual(await cog.panel_prepare(),{'content':'Schedule preview'})
    async def test_market_preparation_never_sends_or_saves(self):
        import datetime
        view=SimpleNamespace();namespace=dict(datetime=datetime,NewWeekMarketView=MagicMock(return_value=view),MarketReportView=MagicMock(),logger=MagicMock())
        cog=self.methods('market_cog.py','MarketCog',{'panel_prepare'},namespace)
        cog.market_channel=SimpleNamespace(send=AsyncMock());cog._get_market_day_number=MagicMock(return_value=1)
        cog._fetch_and_process=AsyncMock(return_value={'mode':'new_week','good_ids':[]});cog._get_next_update_ts=MagicMock(return_value=0)
        cog._get_countdown_timestamp=MagicMock(return_value='Later');cog._get_known_goods=AsyncMock(return_value=[]);cog._save_config=AsyncMock()
        self.assertIs((await cog.panel_prepare())['view'],view);cog.market_channel.send.assert_not_called();cog._save_config.assert_not_called()

if __name__=='__main__':unittest.main()
