"""History contention, real SQLite cancellation, migration and large-window tests."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient,TestServer
from control_panel.storage import Store
from control_panel.history import History,HistoryBusy
from control_panel.server import Panel,create_app

SLOW_SQL='WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers'

class MetadataTests(unittest.TestCase):
    def test_backfill_resumes_and_preserves_bodies(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'history.db';db=sqlite3.connect(path)
            db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY,time REAL,kind TEXT,source TEXT,body TEXT)')
            for n in range(600):
                event={'kind':'request','time':time.time()-10,'source':'bot','data':{'id':str(n),'outcome':'success','duration_ms':n,'request':{'page':1}}}
                db.execute('INSERT INTO events(time,kind,source,body) VALUES(?,?,?,?)',(event['time'],'request','bot',json.dumps(event)))
            db.commit();db.close()
            store=Store(path)
            self.assertFalse(store.indexing()['complete'])
            self.assertFalse(store.backfill()['complete']);store.close()
            store=Store(path)
            self.assertEqual(store.indexing()['cursor'],250)
            while not store.backfill()['complete']:pass
            self.assertEqual(store.analytics()['summary']['total'],600)
            self.assertEqual(store.request('42')['data']['request'],{'page':1})
            self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0],600)
            store.close()

    def test_metadata_and_payload_are_atomic_and_pagination_handles_late_events(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'history.db');now=time.time()
            events=[{'kind':'request','source':'bot','time':now-offset,'data':{'id':str(offset),'duration_ms':offset}} for offset in (1,3,2,4)]
            store.append(events)
            first=store.query('request',limit=2);second=store.query('request',limit=2,before=first['next_cursor'])
            self.assertEqual([e['data']['id'] for e in first['items']+second['items']],['1','2','3','4'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM event_meta').fetchone()[0],4)
            store.db.execute('DELETE FROM events WHERE seq=1');store.db.commit()
            self.assertEqual(store.db.execute('SELECT count(*) FROM event_meta').fetchone()[0],3)
            page=store.query('request',summary=True)
            self.assertNotIn('request',page['items'][0]['data'])
            self.assertIn('attempt_count',page['items'][0]['data'])
            store.close()

    def test_large_history_exact_percentiles_and_small_response(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'history.db');now=time.time();count=30000
            # Large sanitized payloads are deliberately irrelevant to aggregation.
            for offset in range(0,count,500):
                store.append([{'kind':'request','time':now-100-n%800,'source':'bot',
                    'data':{'id':str(n),'endpoint':'/player','outcome':'success','duration_ms':n,
                            'request':{'data':'x'*2048},'attempts':[{'status':200}]}}
                    for n in range(offset,offset+500)])
            started=time.monotonic();result=store.analytics(since=now-3600,until=now)
            elapsed=time.monotonic()-started
            self.assertEqual(result['summary']['total'],count)
            self.assertEqual(result['summary']['median_ms'],14999.5)
            self.assertEqual(result['summary']['p95_ms'],28499)
            self.assertLessEqual(len(result['series']),120)
            self.assertLess(len(json.dumps(result)),50000)
            self.assertLess(elapsed,3)
            store.close()

class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'history.db');self.history=History(self.store,.1)
    async def asyncTearDown(self):
        await self.history.close();self.store.close();self.temp.cleanup()
    async def test_sqlite_is_actually_interrupted_at_deadline(self):
        def slow(reader,**kwargs):return reader.db.execute(SLOW_SQL).fetchone()
        with patch.object(Store,'analytics',slow):
            started=time.monotonic()
            with self.assertRaises(HistoryBusy):await self.history.read('analytics',range_seconds=900)
            self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.history.stats['timed_out_queries'],1)
        self.assertEqual(self.history.performance()['active_reads'],0)
    async def test_two_reads_maximum_and_disconnected_waiter_stops_work(self):
        self.history.budget=1
        def slow(reader,**kwargs):return reader.db.execute(SLOW_SQL).fetchone()
        with patch.object(Store,'analytics',slow):
            tasks=[asyncio.create_task(self.history.read('analytics',range_seconds=value)) for value in (900,3600)]
            await asyncio.sleep(.03)
            with self.assertRaises(HistoryBusy):await self.history.read('analytics',range_seconds=86400)
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            for _ in range(50):
                if not self.history.active:break
                await asyncio.sleep(.01)
            self.assertFalse(self.history.active)
            self.assertEqual(self.history.stats['cancelled_queries'],2)
    async def test_cache_sharing_and_normalized_windows(self):
        self.history.budget=3
        original=Store.analytics;calls=[]
        def measured(reader,**kwargs):
            calls.append(kwargs);time.sleep(.02);return original(reader,**kwargs)
        with patch.object(Store,'analytics',measured),patch('control_panel.history.time.time',return_value=1900000001):
            one,two=await asyncio.gather(self.history.read('analytics',range_seconds=900),self.history.read('analytics',range_seconds=900))
            third=await self.history.read('analytics',range_seconds=900)
        self.assertEqual(len(calls),1);self.assertEqual(one,two);self.assertEqual(two,third)
        self.assertEqual(one['until'],1900000000);self.assertEqual(one['since'],1899999100)
        self.assertTrue(one['indexing']['complete']);self.assertEqual(one['as_of'],one['until'])
        self.assertEqual(self.history.stats['shared_queries'],1);self.assertEqual(self.history.stats['cache_hits'],1)
    async def test_range_cache_survives_a_ten_second_clock_boundary(self):
        with patch('control_panel.history.time.time',return_value=1900000009):
            first=await self.history.read('analytics',range_seconds=900)
        with patch('control_panel.history.time.time',return_value=1900000011):
            second=await self.history.read('analytics',range_seconds=900)
        self.assertEqual(first,second)
        self.assertEqual(self.history.stats['cache_hits'],1)
    async def test_cancelling_one_shared_waiter_preserves_other(self):
        original=Store.analytics
        def measured(reader,**kwargs):time.sleep(.04);return original(reader,**kwargs)
        self.history.budget=3
        with patch.object(Store,'analytics',measured):
            one=asyncio.create_task(self.history.read('analytics',range_seconds=900))
            two=asyncio.create_task(self.history.read('analytics',range_seconds=900))
            await asyncio.sleep(.01);one.cancel()
            await asyncio.gather(one,return_exceptions=True)
            self.assertEqual((await two)['summary']['total'],0)
        self.assertEqual(self.history.stats['cancelled_queries'],0)
    async def test_reader_does_not_block_writer(self):
        self.history.budget=.2
        def slow(reader,**kwargs):return reader.db.execute(SLOW_SQL).fetchone()
        with patch.object(Store,'analytics',slow):
            query=asyncio.create_task(self.history.read('analytics',range_seconds=900));await asyncio.sleep(.02)
            started=time.monotonic()
            self.store.append([{'kind':'log','source':'bot','time':time.time(),'data':{'message':'safe fixture'}}])
            self.assertLess(time.monotonic()-started,.1)
            await asyncio.gather(query,return_exceptions=True)

class HealthIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_second_health_tick_advances_while_both_readers_are_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor=SimpleNamespace(client='123',logs=Path(temp),children={},last={},close=AsyncMock(),
                refresh_tailscale=AsyncMock(),states=AsyncMock(return_value={}))
            with patch('control_panel.server.Supervisor',return_value=supervisor),patch('control_panel.server.BOT',Path(temp)):
                panel=Panel('test','owner@example.test','pc.test.ts.net')
            client=TestClient(TestServer(create_app(panel)));await client.start_server()
            headers={'Host':'pc.test.ts.net:8443','Tailscale-User-Login':'owner@example.test'}
            def slow(reader,**kwargs):return reader.db.execute(SLOW_SQL).fetchone()
            tasks=[]
            try:
                for _ in range(100):
                    if panel.status.get('sampled_at'):break
                    await asyncio.sleep(.01)
                first=panel.status['sampled_at']
                with patch.object(Store,'analytics',slow):
                    tasks=[asyncio.create_task(panel.history.read('analytics',range_seconds=window)) for window in (900,3600)]
                    await asyncio.gather(*tasks,return_exceptions=True)
                    tasks=[asyncio.create_task(panel.history.read('analytics',range_seconds=window)) for window in (86400,259200)]
                    await asyncio.sleep(2.3)
                    started=time.monotonic();response=await client.get('/api/status',headers=headers);state=await response.json()
                    self.assertLess(time.monotonic()-started,.3)
                    self.assertGreaterEqual(state['sampled_at']-first,4.5)
                    self.assertLess(time.time()-state['sampled_at'],1)
                    self.assertEqual(state['performance']['active_reads'],2)
                    await asyncio.gather(*tasks,return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await client.close()

    async def test_live_health_and_ingestion_continue_during_slow_history(self):
        with tempfile.TemporaryDirectory() as temp:
            child=SimpleNamespace(pid=99,poll=lambda:None)
            supervisor=SimpleNamespace(client='123',logs=Path(temp),children={'bot':child},last={},
                close=AsyncMock(),matches_process=lambda *args:True,refresh_tailscale=AsyncMock(),
                states=AsyncMock(return_value={'bot':{'state':'Healthy','ownership':'owned'}}))
            with patch('control_panel.server.Supervisor',return_value=supervisor),patch('control_panel.server.BOT',Path(temp)):
                panel=Panel('test','owner@example.test','pc.test.ts.net')
            panel.history.budget=.1
            client=TestClient(TestServer(create_app(panel)));await client.start_server()
            headers={'Host':'pc.test.ts.net:8443','Tailscale-User-Login':'owner@example.test'}
            try:
                # If health ever awaits history/writer work, this fails immediately.
                with patch.object(panel,'db',new_callable=AsyncMock,side_effect=RuntimeError('writer unavailable')):
                    for _ in range(30):
                        response=await client.get('/api/status',headers=headers);status=await response.json()
                        if status.get('sampled_at'):break
                        await asyncio.sleep(.01)
                    self.assertLess(time.time()-status['sampled_at'],1)
                def slow(reader,**kwargs):return reader.db.execute(SLOW_SQL).fetchone()
                with patch.object(Store,'analytics',slow):
                    history=asyncio.create_task(client.get('/api/analytics?range_seconds=900',headers=headers))
                    await asyncio.sleep(.02)
                    started=time.monotonic();response=await client.get('/api/status',headers=headers)
                    self.assertEqual(response.status,200);self.assertLess(time.monotonic()-started,.3)
                    event={'kind':'heartbeat','source':'bot','environment':'test','time':time.time(),'pid':99,
                           'instance':'fixture','data':{'connected':True}}
                    response=await client.post('/internal/ingest',headers={'Authorization':'Bearer '+panel.secret},json={'events':[event]})
                    self.assertEqual(response.status,200);self.assertTrue(panel.heartbeats['bot']['data']['connected'])
                    result=await history;self.assertEqual(result.status,503)
                    self.assertEqual((await result.json())['code'],'history_busy')
                    self.assertEqual(result.headers['Retry-After'],'2')
                self.assertEqual((await client.get('/api/status',headers=headers)).status,200)
                panel.history.budget=1
                with patch.object(Store,'analytics',slow):
                    abandoned=asyncio.create_task(client.get('/api/analytics?range_seconds=3600',headers=headers))
                    await asyncio.sleep(.03);abandoned.cancel()
                    await asyncio.gather(abandoned,return_exceptions=True)
                    for _ in range(100):
                        if not panel.history.active:break
                        await asyncio.sleep(.01)
                    self.assertFalse(panel.history.active)
                    self.assertGreaterEqual(panel.history.stats['cancelled_queries'],1)
            finally:await client.close()

if __name__=='__main__':unittest.main()
