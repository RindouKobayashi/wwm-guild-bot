"""Device acceptance: fixtures only, no production writes or Discord sends."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from control_panel.file_reader import Reader,FileError,PAGE,PROTOCOL
from control_panel.device_transport import profiles
from control_panel.server import Panel,create_app

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.log=self.root/'activity/logs/test/bot.log';self.log.parent.mkdir(parents=True);self.log.write_text('start\nTraceback\n  next frame\n',encoding='utf-8');self.reader=Reader(self.root)
    def tearDown(self):self.temp.cleanup()
    def key(self):return next(k for k,v in self.reader.registry().items() if v['name'].endswith('bot.log'))
    def test_registered_only_and_exact_raw_download(self):
        key=self.key();self.assertIn('Traceback',self.reader.read(key)['text'])
        for value in ('../.env',str(self.log),'nonexistent'):
            with self.assertRaises(FileError):self.reader.read(value)
        self.assertEqual(base64.b64decode(self.reader.dispatch(dict(action='bytes',id=key,raw=True,protocol=PROTOCOL))['data']),self.log.read_bytes())
    def test_secret_masking_across_page_boundary(self):
        secret='very-private-token-value';self.reader.secrets=[secret.encode()]
        self.log.write_bytes(b'x'*(PAGE-5)+secret.encode()+b'\nAuthorization: Bearer anotherfixture\n')
        key=self.key();one=self.reader.dispatch(dict(action='bytes',id=key,offset=0,protocol=PROTOCOL));two=self.reader.dispatch(dict(action='bytes',id=key,offset=PAGE,protocol=PROTOCOL))
        combined=base64.b64decode(one['data'])+base64.b64decode(two['data'])
        self.assertNotIn(secret.encode(),combined);self.assertNotIn(b'anotherfixture',combined)
        self.assertEqual(len(combined),self.log.stat().st_size)
    def test_rotation_truncation_and_utf8(self):
        key=self.key();page=self.reader.read(key,0)
        self.log.rename(self.log.with_name('bot.log.1'));self.log.write_text('新しいログ\n',encoding='utf-8')
        rotated=self.reader.read(key,200,version=page['file']['version']);self.assertIn('rotated',rotated['notice']);self.assertIn('新しい',rotated['text'])
        self.assertIn('truncated',self.reader.read(key,100)['notice'])
    def test_symlink_escape_is_rejected(self):
        outside=self.root/'private.txt';outside.write_text('not approved')
        self.log.unlink()
        try:self.log.symlink_to(outside)
        except OSError:self.skipTest('Host does not grant symlink creation')
        self.assertFalse(self.reader.registry())
    def test_bounded_search_and_multiline(self):
        self.log.write_text(('match\n'*600),encoding='utf-8');result=self.reader.search([self.key()],'match')
        self.assertEqual(len(result['matches']),500);self.assertFalse(result['complete'])
    def test_profile_validation(self):
        profile=dict(id='pi',hostname='pi.tail.example.ts.net',username='user',environment='production',repository='/home/user/bot')
        path=self.root/'devices.json';path.write_text(json.dumps([profile]));self.assertIn('pi',profiles(path))
        for extra in (dict(hostname='public.example.com'),dict(username='root; command'),dict(repository='relative'),dict(environment='other')):
            path.write_text(json.dumps([dict(profile,**extra)]))
            with self.assertRaises(ValueError):profiles(path)

class DeviceHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        fake=SimpleNamespace(client='123',origin='http://127.0.0.1:8767',logs=self.root/'logs',children={},env={},close=AsyncMock(),states=AsyncMock(return_value={}),last={})
        with patch('control_panel.server.Supervisor',return_value=fake),patch('control_panel.server.BOT',self.root):self.panel=Panel('test','owner@example.test','pc.tail.example.ts.net')
        self.panel.sample=AsyncMock();self.panel.operational=AsyncMock();self.panel.tailscale_checks=AsyncMock()
        self.client=TestClient(TestServer(create_app(self.panel)));await self.client.start_server()
        self.manager=self.panel.devices
        self.headers={'Host':'pc.tail.example.ts.net:8443','Tailscale-User-Login':'owner@example.test'}
        response=await self.client.get('/api/device/context',headers=self.headers);self.cookie=response.cookies['wwm-device'].value
        self.headers['Cookie']='wwm-device='+self.cookie
        self.post=dict(self.headers,Origin=self.panel.origin,**{'X-CSRF-Token':self.panel.csrf})
        log=self.root/'activity/logs/test/bot.log';log.parent.mkdir(parents=True);log.write_text('Authorization: Bearer very-private-fixture\nhello\n')
        self.file_id=next(k for k,v in self.manager.reader.registry().items() if v['name'].endswith('bot.log'))
    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()
    async def post_json(self,path,body):return await self.client.post(path,headers=self.post,json=body)
    async def test_owner_csrf_and_browser_binding(self):
        for path in ('/api/device/context','/api/device/targets','/api/energy-alerts','/api/backups'):
            self.assertEqual((await self.client.get(path)).status,403)
        self.assertEqual((await self.client.post('/api/terminal/sessions',headers=self.headers,json={})).status,403)
        self.assertEqual((await self.client.post('/api/device/files',headers=self.post,json={'target':'pi','connection':'other'})).status,404)
    async def test_raw_grant_expiry_and_default_download(self):
        body=dict(target='local',id=self.file_id,offset=0)
        value=await (await self.post_json('/api/device/files/read',body)).json();self.assertNotIn('very-private-fixture',value['text'])
        self.assertEqual((await self.post_json('/api/device/files/read',dict(body,raw=True))).status,403)
        self.assertEqual((await self.post_json('/api/device/files/raw-access',dict(body,reveal=True,confirmed=True))).status,200)
        value=await (await self.post_json('/api/device/files/read',dict(body,raw=True))).json();self.assertIn('very-private-fixture',value['text'])
        self.manager.grants={k:0 for k in self.manager.grants}
        self.assertEqual((await self.post_json('/api/device/files/read',dict(body,raw=True))).status,403)
        value=await (await self.post_json('/api/device/files/download',body)).read();self.assertNotIn(b'very-private-fixture',value)
    async def test_remote_target_cannot_be_confused_with_local(self):
        profile=dict(id='pi',hostname='pi.tail.example.ts.net',username='user',environment='production',confirmation='pi')
        self.manager.connections['c']=dict(id='c',browser=self.cookie,target='pi',profile=profile,state='connected',last_used=0)
        self.assertEqual((await self.post_json('/api/device/files',dict(target='different',connection='c'))).status,409)
        self.assertEqual((await self.post_json('/api/terminal/sessions',dict(target='pi',connection='c'))).status,400)
        self.manager.connections.clear()
    async def test_foreign_and_replayed_tickets(self):
        fake=SimpleNamespace(browser=self.cookie,id='session',closed=False,ws=None,base=0,produced=0,acked=0,credit=asyncio.Event(),detached=None,connection='',close=AsyncMock())
        self.manager.sessions['session']=fake
        response=await self.post_json('/api/terminal/sessions/session/attach',{});ticket=(await response.json())['ticket']
        with self.assertRaises(Exception):await self.client.ws_connect('/api/terminal/sessions/session/ws',headers=dict(self.headers,Origin='https://evil.example'))
        ws=await self.client.ws_connect('/api/terminal/sessions/session/ws',headers=dict(self.headers,Origin=self.panel.origin));await ws.send_json({'ticket':'wrong'});await ws.receive();self.assertTrue(ws.closed)
        self.assertIn(ticket,self.manager.tickets)
        self.manager.tickets[ticket]=(self.cookie,'other',time.monotonic()+30)
        ws=await self.client.ws_connect('/api/terminal/sessions/session/ws',headers=dict(self.headers,Origin=self.panel.origin));await ws.send_json({'ticket':ticket});await ws.receive();self.assertTrue(ws.closed);self.assertNotIn(ticket,self.manager.tickets)
        self.manager.sessions.clear()
    async def test_real_local_shell_and_close(self):
        response=await self.post_json('/api/terminal/sessions',{'target':'local'});self.assertEqual(response.status,201,await response.text());key=(await response.json())['id']
        ticket=(await (await self.post_json('/api/terminal/sessions/'+key+'/attach',{})).json())['ticket']
        ws=await self.client.ws_connect('/api/terminal/sessions/'+key+'/ws',headers=dict(self.headers,Origin=self.panel.origin));await ws.send_json(dict(ticket=ticket,after=0))
        command="Write-Output ('WWM-PTY-' + 'FIXTURE')\r" if os.name=='nt' else "printf '%s%s\\n' WWM-PTY- FIXTURE\r"
        await ws.send_json({'input':base64.b64encode(command.encode()).decode()})
        output=b''
        for _ in range(60):
            msg=await asyncio.wait_for(ws.receive_json(),10);output+=base64.b64decode(msg['data']);await ws.send_json({'ack':msg['seq']})
            if b'WWM-PTY-FIXTURE' in output:break
        self.assertIn(b'WWM-PTY-FIXTURE',output)
        self.assertNotIn(ticket,self.manager.tickets)
        # Replay cannot take over the live controlling connection.
        replay=await self.client.ws_connect('/api/terminal/sessions/'+key+'/ws',headers=dict(self.headers,Origin=self.panel.origin))
        await replay.send_json(dict(ticket=ticket,after=0));await replay.receive();self.assertTrue(replay.closed)
        after=msg['seq'];await ws.close()
        ticket=(await (await self.post_json('/api/terminal/sessions/'+key+'/attach',{})).json())['ticket']
        ws=await self.client.ws_connect('/api/terminal/sessions/'+key+'/ws',headers=dict(self.headers,Origin=self.panel.origin));await ws.send_json(dict(ticket=ticket,after=after))
        sleep="Start-Sleep -Seconds 20\r" if os.name=='nt' else 'sleep 20\r'
        await ws.send_json({'input':base64.b64encode(sleep.encode()).decode()});await asyncio.sleep(.25)
        await ws.send_json({'input':base64.b64encode(b'\x03').decode()})
        # Console cancellation flushes pending input. Wait for its new prompt
        # before typing another command, as a real terminal user does.
        interrupted=b'';interrupt_started=time.monotonic()
        import re
        for _ in range(100):
            message=await asyncio.wait_for(ws.receive_json(),5);chunk=base64.b64decode(message['data']);interrupted+=chunk;await ws.send_json({'ack':message['seq']})
            if b'\x1b[6n' in chunk:await ws.send_json({'input':base64.b64encode(b'\x1b[1;1R').decode()})
            clean=re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]',b'',interrupted)
            if (os.name=='nt' and b'Start-Sleep' in clean and clean.rstrip().endswith(b'>')) or (os.name!='nt' and b'^C' in clean):break
        self.assertLess(time.monotonic()-interrupt_started,6)
        command="Write-Output ('WWM-RECON-' + 'OK')\r" if os.name=='nt' else "printf '%s%s\\n' WWM-RECON- OK\r"
        await ws.send_json({'input':base64.b64encode(command.encode()).decode()});output=b'';started=time.monotonic()
        for _ in range(60):
            try:msg=await asyncio.wait_for(ws.receive_json(),5)
            except asyncio.TimeoutError:self.fail('Terminal interrupt/reconnect timed out: '+repr(output[-1200:]))
            chunk=base64.b64decode(msg['data']);output+=chunk;await ws.send_json({'ack':msg['seq']})
            if b'\x1b[6n' in chunk:await ws.send_json({'input':base64.b64encode(b'\x1b[1;1R').decode()})
            if b'WWM-RECON-OK' in output:break
        self.assertIn(b'WWM-RECON-OK',output);self.assertLess(time.monotonic()-started,6)
        self.assertEqual((await self.client.get('/api/status',headers=self.headers)).status,200)
        response=await self.client.delete('/api/terminal/sessions/'+key,headers=self.post);self.assertEqual(response.status,200)
        await ws.close();self.assertTrue(self.manager.sessions[key].closed)
        audits=await self.panel.db('device_audits');self.assertNotIn('WWM-PTY-FIXTURE',json.dumps(audits))
    async def test_unacknowledged_output_is_bounded_and_health_responds(self):
        response=await self.post_json('/api/terminal/sessions',{'target':'local'});key=(await response.json())['id']
        ticket=(await (await self.post_json('/api/terminal/sessions/'+key+'/attach',{})).json())['ticket']
        ws=await self.client.ws_connect('/api/terminal/sessions/'+key+'/ws',headers=dict(self.headers,Origin=self.panel.origin));await ws.send_json(dict(ticket=ticket,after=0))
        command="while ($true) { Write-Output ('x' * 1024) }\r" if os.name=='nt' else "yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\r"
        await ws.send_json({'input':base64.b64encode(command.encode()).decode()});await asyncio.sleep(1)
        session=self.manager.sessions[key];self.assertLessEqual(session.produced-session.acked,320*1024)
        self.assertLessEqual(sum(len(data) for _,data in session.ring),1024*1024)
        started=time.monotonic();self.assertEqual((await self.client.get('/api/status',headers=self.headers)).status,200);self.assertLess(time.monotonic()-started,1)
        await self.client.delete('/api/terminal/sessions/'+key,headers=self.post);await ws.close()
    async def test_backup_integrity_and_manifest(self):
        directory=self.root/'data';directory.mkdir();db=sqlite3.connect(directory/'schedule.db');db.execute('CREATE TABLE fixture(x)');db.execute('INSERT INTO fixture VALUES(42)');db.commit();db.close()
        response=await self.post_json('/api/backups',{'confirmed':True});self.assertEqual(response.status,202);key=(await response.json())['id'];job=self.manager.jobs[key]
        await asyncio.wait_for(job['task'],10);self.assertEqual(job['state'],'succeeded',job.get('message'));self.assertTrue(job['result']['verified'])
        path=self.manager.backup_directory()/(job['result']['id']+'.zip')
        import zipfile
        with zipfile.ZipFile(path) as archive:self.assertIn('schedule.db',archive.namelist());self.assertIn('manifest.json',archive.namelist())
    async def test_energy_inspector_does_not_call_game(self):
        response=await self.client.get('/api/energy-alerts',headers=self.headers);self.assertEqual(response.status,200);self.assertFalse((await response.json())['observations_available'])

if __name__=='__main__':unittest.main()
