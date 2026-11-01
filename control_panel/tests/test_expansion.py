"""Expansion acceptance: full-window totals, correlation, job outcomes and incidents."""
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
from control_panel.storage import Store
from control_panel.analytics import failure_reason
from control_panel.incidents import Monitor
from control_panel import jobs, telemetry
from control_panel.server import Panel, create_app

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'history.db';self.store=Store(self.path)
        self.now=time.time()
    def tearDown(self):self.store.close();self.temp.cleanup()
    def request(self,index,outcome='success',source='bot',duration=None,attempts=None,**extra):
        return {'kind':'request','source':source,'time':self.now-10,'data':dict(id=str(index),endpoint='/player',
            label='Player lookup',outcome=outcome,status=200 if outcome=='success' else 403,
            duration_ms=index if duration is None else duration,attempts=attempts or [{'number':1,'status':200}],**extra)}
    def test_analytics_cover_more_than_visible_page_and_do_not_double_count_activity(self):
        events=[self.request(n) for n in range(1,501)]
        events += [self.request('activity',source='activity',duration=1)]
        events += [self.request('error',outcome='error',duration=0,attempts=[{'number':1,'error':'TimeoutError'},{'number':2,'status':403}])]
        self.store.append(events)
        result=self.store.analytics(since=self.now-3600,until=self.now)
        counts=result['summary']
        self.assertEqual(counts['total'],501);self.assertEqual(counts['activity_requests'],1)
        self.assertEqual(counts['success'],500);self.assertEqual(counts['error'],1)
        self.assertEqual(counts['retries'],1);self.assertEqual(counts['timeouts'],1)
        self.assertEqual(counts['median_ms'],250);self.assertEqual(counts['p95_ms'],475)
        self.assertEqual(result['endpoints'][0]['count'],501)
        self.assertEqual(sum(bucket['count'] for bucket in result['series']),501)
        self.assertTrue(any(bucket['mean_ms'] is None for bucket in result['series']))
        self.assertEqual(len(self.store.query('request')['items']),100)
    def test_filters_match_analytics_and_listing(self):
        self.store.append([self.request(1),self.request(2,'error',duration=200,attempts=[{},{}]),self.request(3,'unknown')])
        filters=dict(outcome='error',http_status='403',min_duration='100',retried='true',endpoint_name='/player')
        self.assertEqual(self.store.analytics(**filters)['summary']['total'],1)
        self.assertEqual(len(self.store.query('request',**filters)['items']),1)
        for values in [dict(http_status='700'),dict(min_duration='nan'),dict(since=float('nan')),dict(retried='invalid')]:
            with self.assertRaises(ValueError):self.store.analytics(**values)
    def test_command_summary_uses_complete_window(self):
        self.store.append([{'kind':'command','source':'bot','time':self.now-10,'data':{'id':str(n),
            'name':'player','type':'application','outcome':'error' if n==300 else 'success','duration_ms':n}}
            for n in range(301)])
        result=self.store.command_summary(since=self.now-3600)
        self.assertEqual(result['total'],301);self.assertEqual(result['errors'],1)
        self.assertEqual(result['slowest'][0]['count'],301);self.assertEqual(result['slowest'][0]['max_ms'],300)
    def test_investigation_links_caller_siblings_and_logs(self):
        events=[self.request('a',parent_id='command-root'),self.request('b',parent_id='command-root')]
        events += [{'kind':'command','source':'bot','time':self.now-11,'data':{'id':'command-root','name':'player'}},
                   {'kind':'log','source':'bot','time':self.now-9,'data':{'request_id':'a','message':'Safe fixture'}}]
        self.store.append(events)
        result=self.store.investigation('a')
        self.assertEqual(result['root_id'],'command-root');self.assertEqual(len(result['events']),3)
        self.assertEqual(len(result['logs']),1)
        self.assertIsNone(self.store.investigation('missing'))
    def test_incident_grouping_acknowledgement_recovery_and_new_episode(self):
        self.store.incident('job_failure','job',True,occurrence='a')
        self.store.incident('job_failure','job',True,occurrence='a')
        self.store.incident('job_failure','job',True,occurrence='b')
        item=self.store.incidents()[0];self.assertEqual(item['count'],2)
        self.assertTrue(self.store.acknowledge(item['id'],'Investigating'))
        self.assertEqual(self.store.incidents()[0]['state'],'active')
        self.store.incident('job_failure','job',False)
        self.assertEqual(self.store.incidents()[0]['state'],'recovered')
        self.store.incident('job_failure','job',True,occurrence='c')
        self.assertEqual(len(self.store.incidents()),2)
    def test_unknown_http_200_not_reported_as_authorization_recovery(self):
        events=[self.request(n,'error') for n in range(3)]
        self.store.append(events);self.assertTrue(self.store.recent_auth()['rejected'])
        self.store.append([self.request(4)])
        self.assertTrue(self.store.recent_auth()['recovered'])
    def test_failure_explanations_do_not_echo_untrusted_error_text(self):
        self.assertIn('does not prove',failure_reason({'status':403}))
        self.assertEqual(failure_reason({'attempts':[{'error':'TimeoutError'}]}),'Upstream request timed out.')
        self.assertNotIn('credential',failure_reason({'error':'credential-fixture','outcome':'error'}))

class MigrationTests(unittest.TestCase):
    def test_existing_unversioned_history_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'history.db';db=sqlite3.connect(path)
            db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY,time REAL,kind TEXT,source TEXT,body TEXT)')
            event={'time':time.time(),'kind':'request','source':'bot','data':{'id':'old','duration_ms':1,'outcome':'success'}}
            db.execute('INSERT INTO events VALUES(1,?,?,?,?)',(event['time'],'request','bot',json.dumps(event)));db.commit();db.close()
            store=Store(path)
            try:
                self.assertEqual(store.request('old')['data']['id'],'old')
                self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0],2)
            finally:store.close()
    def test_future_schema_refused_without_downgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'history.db';db=sqlite3.connect(path);db.execute('PRAGMA user_version=99');db.close()
            with self.assertRaises(RuntimeError):Store(path)
            db=sqlite3.connect(path);self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],99);db.close()

class JobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):jobs.states.clear()
    async def test_return_values_failures_and_cancellation_preserved(self):
        @jobs.observe_job('fixture.job')
        async def run(mode):
            if mode=='fail':raise ValueError('private-error')
            if mode=='cancel':raise asyncio.CancelledError()
            return 42
        with patch.object(jobs,'enabled',return_value=True),patch.object(jobs,'emit') as emit:
            self.assertEqual(await run('success'),42)
            success=jobs.states['fixture.job']['last_success']
            for _ in range(3):
                with self.assertRaises(ValueError):await run('fail')
            self.assertEqual(jobs.states['fixture.job']['consecutive_failures'],3)
            self.assertEqual(jobs.states['fixture.job']['last_success'],success)
            with self.assertRaises(asyncio.CancelledError):await run('cancel')
            self.assertEqual(jobs.states['fixture.job']['phase'],'cancelled')
            self.assertNotIn('private-error',json.dumps([call.args for call in emit.call_args_list]))
            await run('success');self.assertEqual(jobs.states['fixture.job']['consecutive_failures'],0)
        self.assertIsNone(telemetry.parent_id.get())
    async def test_concurrent_jobs_have_separate_request_contexts(self):
        values={}
        def make(name):
            @jobs.observe_job(name)
            async def run():
                before=telemetry.parent_id.get();await asyncio.sleep(.01)
                values[name]=(before,telemetry.parent_id.get())
            return run
        with patch.object(jobs,'enabled',return_value=True),patch.object(jobs,'emit'):
            await asyncio.gather(make('a')(),make('b')())
        self.assertEqual(values['a'][0],values['a'][1]);self.assertNotEqual(values['a'][0],values['b'][0])
    async def test_logged_error_marks_iteration_failed_without_changing_result(self):
        @jobs.observe_job('handled')
        async def run():jobs.report_error();return 'handled'
        with patch.object(jobs,'enabled',return_value=True),patch.object(jobs,'emit'):
            self.assertEqual(await run(),'handled')
        self.assertEqual(jobs.states['handled']['phase'],'failed')
    async def test_disabled_instrumentation_is_noop(self):
        @jobs.observe_job('disabled')
        async def run():return 'unchanged'
        with patch.object(jobs,'enabled',return_value=False),patch.object(jobs,'emit') as emit:
            self.assertEqual(await run(),'unchanged');emit.assert_not_called()

    async def test_real_discord_loop_accepts_instrumented_coroutine(self):
        from discord.ext import tasks
        @tasks.loop(seconds=60)
        @jobs.observe_job('fixture.loop')
        async def loop():return 17
        with patch.object(jobs,'enabled',return_value=True),patch.object(jobs,'emit'):
            self.assertEqual(await loop(),17)
        self.assertEqual(jobs.states['fixture.loop']['phase'],'completed')

class RequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_logs_use_request_id_and_caller_is_restored(self):
        @telemetry.observe_wwm
        async def request(url,payload):
            telemetry.emit('log',{'request_id':telemetry.parent_id.get(),'message':'Safe fixed event'})
            telemetry.attempt(number=1,status=200)
            return {'code':0}
        token=telemetry.parent_id.set('command-root')
        try:
            with patch.object(telemetry,'enabled',return_value=True),patch.object(telemetry,'emit') as emit,patch.dict('sys.modules',{'settings':SimpleNamespace()}):
                await request('https://fixture.invalid/api',{})
            log=next(call.args[1] for call in emit.call_args_list if call.args[0]=='log')
            event=next(call.args[1] for call in emit.call_args_list if call.args[0]=='request')
            self.assertEqual(log['request_id'],event['id']);self.assertEqual(event['parent_id'],'command-root')
            self.assertEqual(telemetry.parent_id.get(),'command-root')
        finally:telemetry.parent_id.reset(token)

class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_command_records_name_without_options(self):
        import discord
        from control_panel.commands import ObservedTree
        client=discord.Client(intents=discord.Intents.none());tree=ObservedTree(client)
        interaction=SimpleNamespace(data={'name':'player','options':[{'name':'secret','value':'private-fixture'}]},extras={})
        with patch('control_panel.commands.enabled',return_value=True):
            self.assertTrue(await tree.interaction_check(interaction))
        data=interaction.extras['panel_command']
        self.assertEqual(telemetry.parent_id.get(),data['id'])
        self.assertNotIn('private-fixture',json.dumps(data))
        with patch('control_panel.commands.emit') as emit,patch('discord.app_commands.CommandTree.on_error',new_callable=AsyncMock) as original:
            await tree.on_error(interaction,RuntimeError('private-fixture'))
            original.assert_awaited_once()
            self.assertNotIn('private-fixture',json.dumps(emit.call_args.args))
        telemetry.parent_id.set(None)
        await client.close()

    async def test_prefix_hooks_restore_context_and_exclude_arguments(self):
        from control_panel.commands import install
        class Bot:
            def before_invoke(self,callback):self.start=callback;return callback
            def after_invoke(self,callback):self.end=callback;return callback
            def listen(self,*args):return lambda callback:callback
        bot=Bot();install(bot)
        context=SimpleNamespace(command=SimpleNamespace(qualified_name='guild lookup'),command_failed=False,
                                args=['private-fixture'],author='private-user')
        with patch('control_panel.commands.enabled',return_value=True),patch('control_panel.commands.emit') as emit:
            await bot.start(context)
            self.assertIsNotNone(telemetry.parent_id.get())
            await bot.end(context)
        self.assertIsNone(telemetry.parent_id.get())
        self.assertNotIn('private-fixture',json.dumps(emit.call_args.args));self.assertNotIn('private-user',json.dumps(emit.call_args.args))

class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_sustained_connection_rule_and_recovery(self):
        monitor=Monitor();panel=SimpleNamespace(storage_error=None,dropped={},db=AsyncMock(return_value={'rejected':False,'recovered':False}))
        host={'cpu_percent':1,'memory_percent':1,'disk_free':10**10}
        services={'bot':{'ownership':'owned','state':'Degraded'}}
        with patch('control_panel.incidents.time.time',return_value=100):await monitor.check(panel,services,host)
        rules=panel.db.call_args.args[1];self.assertFalse(next(rule[2] for rule in rules if rule[0]=='discord_connection'))
        with patch('control_panel.incidents.time.time',return_value=161):await monitor.check(panel,services,host)
        rules=panel.db.call_args.args[1];self.assertTrue(next(rule[2] for rule in rules if rule[0]=='discord_connection'))
        services['bot']['state']='Healthy'
        with patch('control_panel.incidents.time.time',return_value=162):await monitor.check(panel,services,host)
        rules=panel.db.call_args.args[1];self.assertFalse(next(rule[2] for rule in rules if rule[0]=='discord_connection'))
    def test_threshold_validation(self):
        for values in [{'cpu_percent':101},{'sustained_seconds':0},{'unknown':1}]:
            with self.assertRaises(ValueError):Monitor(values)
    async def test_planned_stop_does_not_open_process_exit_incident(self):
        monitor=Monitor();panel=SimpleNamespace(storage_error=None,dropped={},db=AsyncMock(return_value={'rejected':False,'recovered':False}))
        host={'cpu_percent':1,'memory_percent':1,'disk_free':10**10}
        services={'bot':{'ownership':'none','state':'Stopped','expected_running':False}}
        await monitor.check(panel,services,host)
        rules=panel.db.call_args.args[1];self.assertFalse(next(rule[2] for rule in rules if rule[0]=='process_exit'))
        services['bot']['expected_running']=True
        await monitor.check(panel,services,host)
        rules=panel.db.call_args.args[1];self.assertTrue(next(rule[2] for rule in rules if rule[0]=='process_exit'))

class ExpansionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();supervisor=SimpleNamespace(client='123',logs=Path(self.temp.name),children={},close=AsyncMock())
        with patch('control_panel.server.Supervisor',return_value=supervisor),patch('control_panel.server.BOT',Path(self.temp.name)):
            self.panel=Panel('test','owner@example.test','pc.test.ts.net')
        self.panel.sample=AsyncMock(return_value=None)
        self.client=TestClient(TestServer(create_app(self.panel)));await self.client.start_server()
        self.headers={'Host':'pc.test.ts.net:8443','Tailscale-User-Login':'owner@example.test'}
    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()
    async def test_new_endpoints_require_owner(self):
        for path in ['/api/analytics','/api/jobs','/api/incidents','/api/commands','/api/requests/missing/investigation']:
            response=await self.client.get(path);self.assertEqual(response.status,403)
        for path in ['/api/analytics','/api/jobs','/api/incidents','/api/commands']:
            response=await self.client.get(path,headers=self.headers);self.assertEqual(response.status,200,path)
    async def test_acknowledgement_requires_csrf_and_does_not_resolve(self):
        await self.panel.db('incident','fixture','bot',True)
        item=(await self.panel.db('incidents'))[0]
        response=await self.client.post('/api/incidents/'+item['id']+'/acknowledge',headers=self.headers,json={'note':'checked'})
        self.assertEqual(response.status,403)
        headers=dict(self.headers,Origin=self.panel.origin,**{'X-CSRF-Token':self.panel.csrf})
        response=await self.client.post('/api/incidents/'+item['id']+'/acknowledge',headers=headers,json={'note':'checked'})
        self.assertEqual(response.status,200)
        result=(await self.panel.db('incidents'))[0];self.assertEqual(result['state'],'active');self.assertEqual(result['note'],'checked')
    async def test_old_and_new_telemetry_versions_accepted(self):
        headers={'Authorization':'Bearer '+self.panel.secret}
        for version in [None,1,2,99]:
            event={'kind':'log','source':'bot','time':time.time(),'environment':'test','data':{'message':'safe'}}
            if version is not None:event['version']=version
            response=await self.client.post('/internal/ingest',headers=headers,json={'events':[event]})
            self.assertEqual(response.status,400 if version==99 else 200)
    async def test_invalid_filter_is_bad_request(self):
        response=await self.client.get('/api/analytics?min_duration=nan',headers=self.headers)
        self.assertEqual(response.status,400)

if __name__=='__main__':unittest.main()
