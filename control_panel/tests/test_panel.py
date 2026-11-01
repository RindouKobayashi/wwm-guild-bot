"""Offline acceptance tests. No Discord, Tailscale configuration or game calls."""
import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp.test_utils import TestClient, TestServer
from control_panel.privacy import preview, redact, REQUEST_FIELDS
from control_panel.storage import Store
from control_panel.locks import FileLock
from control_panel.setup import route_state
from control_panel.supervisor import Supervisor
from control_panel.server import Panel, create_app
from control_panel import telemetry

class PrivacyTests(unittest.TestCase):
    def test_secrets_and_personal_data_are_dropped_recursively(self):
        value = {'code': 0, 'token': 'credential-fixture', 'result': {'id': 'public-id', 'cookie': 'credential-fixture',
                 'nickname': 'private-player', 'message': 'private-chat', 'data': [{'secret': 'credential-fixture', 'score': 3}]}}
        result = json.dumps(preview(value))
        for secret in ('credential-fixture','private-player','private-chat'):
            self.assertNotIn(secret, result)
        self.assertIn('public-id', result)

    def test_no_nested_response_fields_in_request(self):
        self.assertNotIn('result', preview({'result': {'id': 'bad'}, 'page': 1}, REQUEST_FIELDS))

    def test_binary_large_array_nan_and_preview_size(self):
        self.assertIn('binary', preview({'data': b'private'})['data'])
        result = preview({'data': list(range(100))})
        self.assertEqual(result['data'][-1]['omitted_items'], 60)
        self.assertLessEqual(len(json.dumps(preview({'data': ['x'*512]*100})).encode()), 16384)
        self.assertIn('omitted', preview({'score': math.nan}))

    def test_redacts_env_bearer_and_query(self):
        with patch.dict(os.environ, {'DISCORD_API_TOKEN': 'privatefixture'}):
            text = redact('\x1b[31mprivatefixture Bearer abcdef https://example.test/path?token=hidden')
        self.assertNotIn('privatefixture', text)
        self.assertNotIn('abcdef', text)
        self.assertNotIn('hidden', text)
        self.assertNotIn('\x1b', text)

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)/'history.db'; self.store = Store(self.path)
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def event(self, kind='request', when=None, key='r1'):
        return {'kind': kind, 'time': time.time() if when is None else when, 'source':'bot', 'data':{'id':key,'level':'ERROR'}}
    def test_pagination_filters_and_detail(self):
        self.store.append([self.event(key=str(n)) for n in range(5)])
        page = self.store.query('request',limit=2)
        older = self.store.query('request',before=page['next_cursor'],limit=2)
        self.assertFalse({x['cursor'] for x in page['items']} & {x['cursor'] for x in older['items']})
        self.assertEqual(self.store.request('3')['data']['id'], '3')
        self.assertEqual(len(self.store.query('request',source='activity')['items']),0)
    def test_retention_and_interrupted_operations(self):
        self.store.append([self.event(when=time.time()-73*3600), self.event('metric',time.time()-5*86400)])
        self.store.operation(item={'id':'op1','state':'running','started':time.time()})
        self.store.prune()
        self.assertEqual(len(self.store.query('request')['items']),0)
        self.assertEqual(len(self.store.query('metric')['items']),1)
        self.store.close(); self.store = Store(self.path)
        self.assertEqual(self.store.operation(key='op1')['state'], 'interrupted')
    def test_budget_reclaims_physical_space(self):
        self.store.budget = 256*1024
        events = [dict(self.event(), data={'id':str(n),'data':'x'*4096}) for n in range(100)]
        self.store.append(events)
        for _ in range(10):self.store.prune()
        self.assertLess(self.store.storage_bytes(),self.store.budget)

class RouteAndLockTests(unittest.TestCase):
    def route(self, public=False, proxy='http://127.0.0.1:8770'):
        return {'Web':{'pc.test.ts.net:8443':{'Handlers':{'/':{'Proxy':proxy}}}},'AllowFunnel':{'pc.test.ts.net:8443':public}}
    def test_matching_private_route(self):
        self.assertTrue(route_state(self.route(), 'pc.test.ts.net'))
        self.assertTrue(route_state({'Foreground':{'session':self.route()}}, 'pc.test.ts.net'))
    def test_public_and_other_routes_rejected(self):
        for route in [self.route(True),self.route(proxy='http://127.0.0.1:1234')]:
            with self.assertRaises(RuntimeError):route_state(route,'pc.test.ts.net')
        self.assertFalse(route_state({'Web':{'pc.test.ts.net:443':{}}},'pc.test.ts.net'))
    def test_duplicate_lock_rejected_and_released(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'lock'; first=FileLock(path)
            try:
                with self.assertRaises(RuntimeError):FileLock(path)
            finally:first.close()
            FileLock(path).close()

class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        value=object.__new__(Supervisor)
        value.guard=asyncio.Lock();value.lock=None;value.children={};value.handles={};value.last={}
        value.states=AsyncMock(return_value={k:{'ownership':'none'} for k in ('bot','activity','tunnel')})
        value.start=AsyncMock();value.stop=AsyncMock(return_value='Graceful shutdown')
        return value
    async def test_dependency_order(self):
        value=self.make();await value.action('stack','start')
        self.assertEqual([c.args[0] for c in value.start.await_args_list],['activity','tunnel','bot'])
        await value.action('stack','stop')
        self.assertEqual([c.args[0] for c in value.stop.await_args_list],['bot','tunnel','activity'])
    async def test_external_preflight_does_not_partially_restart(self):
        value=self.make();value.states.return_value['tunnel']['ownership']='external'
        with self.assertRaises(RuntimeError):await value.action('stack','restart')
        value.stop.assert_not_awaited();value.start.assert_not_awaited()
    async def test_failure_rolls_back_new_services(self):
        value=self.make();value.start.side_effect=[None,RuntimeError('tunnel failed')]
        with self.assertRaises(RuntimeError):await value.action('stack','start')
        value.stop.assert_awaited_once_with('activity')

    async def test_real_owned_worker_stops_without_affecting_unowned_processes(self):
        # Exercise OS process-group signalling with a disposable local worker,
        # not a Discord bot or a Tailscale process.
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            value=object.__new__(Supervisor)
            value.logs=Path(temp);value.env=dict(os.environ);value.children={};value.handles={}
            value.expected_running=set();value.readers={};value.process_metrics={};value.identities={};value.last={};value.lock=None
            code="import json,os,sys; print('fixture ready',flush=True); message=json.loads(sys.stdin.readline()); sys.exit(0 if message == {'action':'shutdown','instance':os.environ['WWM_PANEL_PROCESS_ID']} else 1)"
            value.spawn('bot',[sys.executable,'-u','-c',code])
            try:
                for _ in range(40):
                    path=Path(temp)/'bot.log'
                    if path.exists() and path.stat().st_size:break
                    await asyncio.sleep(.05)
                self.assertIsNone(value.children['bot'].poll())
                result=await value.stop('bot')
                self.assertEqual(result,'Graceful shutdown')
                self.assertEqual(value.children['bot'].poll(),0)
            finally:
                if value.children['bot'].poll() is None:
                    value.children['bot'].kill();value.children['bot'].wait()

    async def test_forced_shutdown_is_reported(self):
        import subprocess
        value=object.__new__(Supervisor)
        child=MagicMock();child.pid=99999999;child.poll.return_value=None
        child.wait.side_effect=[subprocess.TimeoutExpired('fixture',10),0]
        value.children={'bot':child};value.handles={'bot':MagicMock()};value.readers={};value.lock=None;value.last={}
        result=await value.stop('bot')
        self.assertIn('Forced termination',result)
        child.terminate.assert_called_once()

class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_preserves_result_and_groups_attempts(self):
        result={'code':0,'result':{'score':1,'token':'never-export'}}
        @telemetry.observe_wwm
        async def call(url,payload):
            telemetry.attempt(number=1,status=401)
            telemetry.attempt(number=2,status=200,request_bytes=10,response_bytes=20)
            return result
        with patch.object(telemetry,'enabled',return_value=True),patch.object(telemetry,'emit') as emit,patch.dict('sys.modules', {'settings':SimpleNamespace(WWM_API_URL='https://game.test/player')}):
            actual=await call('https://game.test/player',{'page':1,'token':'never-export'})
        self.assertIs(actual,result)
        data=emit.call_args.args[1]
        self.assertEqual(len(data['attempts']),2)
        self.assertEqual(data['outcome'],'success')
        self.assertNotIn('never-export',json.dumps(data))
    async def test_unknown_endpoint_metadata_only_and_http_200_not_enough(self):
        @telemetry.observe_wwm
        async def call(url,payload):
            telemetry.attempt(number=1,status=200)
            return {'result':{'score':1}}
        with patch.object(telemetry,'enabled',return_value=True),patch.object(telemetry,'emit') as emit,patch.dict('sys.modules',{'settings':SimpleNamespace()}):
            await call('https://unknown.test/private',{'data':'hidden'})
        self.assertEqual(emit.call_args.args[1]['outcome'],'unknown')
        self.assertEqual(emit.call_args.args[1]['request'],{'omitted':'Metadata-only endpoint'})
    async def test_exceptions_preserved(self):
        @telemetry.observe_wwm
        async def call(url,payload):raise TimeoutError('fixture')
        with patch.object(telemetry,'enabled',return_value=True),patch.object(telemetry,'emit'),patch.dict('sys.modules',{'settings':SimpleNamespace()}):
            with self.assertRaises(TimeoutError):await call('https://unknown.test',{})

class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        fake=SimpleNamespace(client='123',origin='http://127.0.0.1:8767',logs=Path(self.temp.name),children={},
                             env={},action=AsyncMock(return_value='Started'),close=AsyncMock(),
                             states=AsyncMock(return_value={}),last={})
        with patch('control_panel.server.Supervisor',return_value=fake),patch('control_panel.server.BOT',Path(self.temp.name)):
            self.panel=Panel('test','owner@example.test','pc.test.ts.net')
        self.panel.sample=AsyncMock(return_value=None)
        self.client=TestClient(TestServer(create_app(self.panel)))
        await self.client.start_server()
        self.headers={'Host':'pc.test.ts.net:8443','Tailscale-User-Login':'owner@example.test'}
        self.post_headers=dict(self.headers,Origin=self.panel.origin,**{'X-CSRF-Token':self.panel.csrf})
    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()
    async def test_all_browser_paths_require_owner(self):
        for path in ['/','/assets/app.js','/api/status','/api/events','/api/logs','/api/export']:
            response=await self.client.get(path)
            self.assertEqual(response.status,403,path)
        response=await self.client.get('/api/status',headers=dict(self.headers,**{'Tailscale-User-Login':'other@example.test'}))
        self.assertEqual(response.status,403)
        response=await self.client.get('/api/status',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['csrf'],self.panel.csrf)
    async def test_host_csrf_and_unknown_actions(self):
        response=await self.client.get('/api/status',headers=dict(self.headers,Host='public.test'))
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/services/bot/start',headers=self.headers,json={})
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/services/bot/delete',headers=self.post_headers,json={})
        self.assertEqual(response.status,404)
    async def test_operation_accepted_audited_and_conflicts(self):
        response=await self.client.post('/api/services/bot/start',headers=self.post_headers,json={})
        self.assertEqual(response.status,202)
        record=await response.json()
        await asyncio.sleep(.05)
        response=await self.client.get('/api/operations/'+record['id'],headers=self.headers)
        item=await response.json()
        self.assertEqual(item['state'],'succeeded')
        self.assertEqual(item['identity'],'owner@example.test')
        self.panel.busy=True
        response=await self.client.post('/api/services/bot/start',headers=self.post_headers,json={})
        self.assertEqual(response.status,409)
    async def test_production_confirmation(self):
        self.panel.environment='production'
        response=await self.client.post('/api/services/bot/stop',headers=self.post_headers,json={})
        self.assertEqual(response.status,400)
        response=await self.client.post('/api/services/bot/stop',headers=self.post_headers,json={'confirmation':'pc.test.ts.net production'})
        self.assertEqual(response.status,202)
    async def test_ingestion_secret_and_environment_isolation(self):
        response=await self.client.post('/internal/ingest',json={'events':[]},headers=self.post_headers)
        self.assertEqual(response.status,403)
        headers={'Authorization':'Bearer '+self.panel.secret}
        event={'kind':'request','source':'bot','time':time.time(),'environment':'production','data':{'id':'r1'}}
        response=await self.client.post('/internal/ingest',json={'events':[event]},headers=headers)
        self.assertEqual(response.status,400)
        event['environment']='test'
        response=await self.client.post('/internal/ingest',json={'events':[event]},headers=headers)
        self.assertEqual(response.status,200)
    async def test_export_excludes_secrets(self):
        self.panel.status={'environment':'test','operations':[{'identity':'private'}],
                           'incidents':[{'note':'private-note-fixture'}]}
        response=await self.client.get('/api/export',headers=self.headers)
        body=await response.text()
        self.assertNotIn(self.panel.secret,body);self.assertNotIn(self.panel.csrf,body);self.assertNotIn('private',body)
    async def test_queue_full_does_not_block(self):
        for _ in range(1100):self.panel.enqueue({'kind':'log'})
        self.assertGreater(self.panel.dropped['panel'],0)
    async def test_history_read_failure_returns_service_error(self):
        with patch.object(self.panel.history,'read',side_effect=OSError('history unavailable')):
            response=await self.client.get('/api/logs',headers=self.headers)
        self.assertEqual(response.status,503)

class CombinedStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_launcher_starts_stack_and_audits_it(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor=SimpleNamespace(client='123',logs=Path(temp),children={},
                action=AsyncMock(return_value='Stack started'),close=AsyncMock())
            with patch('control_panel.server.Supervisor',return_value=supervisor),patch('control_panel.server.BOT',Path(temp)):
                panel=Panel('production','owner@example.test','pi.test.ts.net')
            panel.start_stack=True
            panel.sample=AsyncMock(return_value=None)
            client=TestClient(TestServer(create_app(panel)))
            try:
                await client.start_server()
                await asyncio.wait_for(panel.operation_task,2)
                supervisor.action.assert_awaited_once_with('stack','start')
                audit=await panel.db('operations')
                self.assertEqual(audit[0]['state'],'succeeded')
                self.assertEqual(audit[0]['identity'],'local launcher')
                self.assertFalse(panel.busy)
            finally:
                await client.close()

    async def test_startup_failure_keeps_panel_available(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor=SimpleNamespace(client='123',logs=Path(temp),children={},
                action=AsyncMock(side_effect=RuntimeError('Externally managed service')),close=AsyncMock())
            with patch('control_panel.server.Supervisor',return_value=supervisor),patch('control_panel.server.BOT',Path(temp)):
                panel=Panel('production','owner@example.test','pi.test.ts.net')
            panel.start_stack=True
            panel.sample=AsyncMock(return_value=None)
            client=TestClient(TestServer(create_app(panel)))
            try:
                await client.start_server()
                await asyncio.wait_for(panel.operation_task,2)
                audit=await panel.db('operations')
                self.assertEqual(audit[0]['state'],'failed')
                response=await client.get('/api/status',headers={'Host':'pi.test.ts.net:8443','Tailscale-User-Login':'owner@example.test'})
                self.assertEqual(response.status,200)
                self.assertFalse(panel.busy)
            finally:
                await client.close()

if __name__=='__main__':unittest.main()
