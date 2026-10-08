import importlib.util,os,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock,MagicMock
from aiohttp.test_utils import TestClient,TestServer
SPEC=importlib.util.spec_from_file_location('activity_server',Path(__file__).resolve().parents[1]/'server.py')
server=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(server)
class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_is_snapshot_only(self):
        async with TestClient(TestServer(server.create_app())) as client:
            self.assertEqual((await client.get('/api/config')).status,200)
            self.assertEqual((await client.get('/api/snapshot')).status,200)
            self.assertEqual((await client.get('/snapshot.json')).status,404)
            self.assertEqual((await client.get('/api/guild?number=1028077590')).status,403)
            self.assertEqual((await client.post('/api/auth',json={'code':'x'})).status,403)
            self.assertEqual((await client.get('/server.py')).status,404)
    async def test_discord_requires_identity_and_never_serves_snapshot(self):
        with patch.dict(os.environ,DISCORD_CLIENT_ID='123456',DISCORD_CLIENT_SECRET='offline-test'):
            app=server.create_app(live=True)
        # Cleanup should not import the game transport; no live request is made.
        app.on_cleanup.clear()
        async with TestClient(TestServer(app)) as client:
            for path in ('/api/rank?board=abyss:3','/api/guild?number=1028077590'):
                self.assertEqual((await client.get(path)).status,401)
            self.assertEqual((await client.get('/api/snapshot')).status,403)
            self.assertEqual((await client.get('/snapshot.json')).status,404)
            self.assertEqual((await client.post('/api/auth',json={'code':'x'},headers={'Origin':'https://evil.example'})).status,403)
    def test_discord_configuration_required(self):
        with patch.dict(os.environ,DISCORD_CLIENT_ID='',DISCORD_CLIENT_SECRET=''):
            with self.assertRaises(ValueError):server.create_app(live=True)
    def test_binding_is_read_only_and_checked_by_discord_identity(self):
        import sqlite3,tempfile
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();db=root/'data/guild_verification.db'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE verified_members(user_id INTEGER PRIMARY KEY,character_uid TEXT)')
                conn.execute('INSERT INTO verified_members VALUES (123,"1028077590")')
            conn.close()
            with patch.object(server,'BOT',root):
                self.assertEqual(server.binding('123'),'1028077590')
                self.assertIsNone(server.binding('999'))

    async def test_verified_discord_identity_still_requires_own_binding(self):
        def context(value):
            cm=MagicMock();cm.__aenter__=AsyncMock(return_value=value);cm.__aexit__=AsyncMock(return_value=False);return cm
        oauth=MagicMock(status=200);oauth.json=AsyncMock(return_value={'access_token':'offline-token','expires_in':3600})
        identity=MagicMock(status=200);identity.json=AsyncMock(return_value={'id':'123','username':'Offline viewer'})
        http=MagicMock();http.post.return_value=context(oauth);http.get.return_value=context(identity)
        with patch.dict(os.environ,DISCORD_CLIENT_ID='123456',DISCORD_CLIENT_SECRET='offline-test'):
            app=server.create_app(live=True)
        app.on_cleanup.clear()
        async with TestClient(TestServer(app)) as client:
            with patch.object(server,'ClientSession',return_value=context(http)),patch.object(server,'binding',return_value=None):
                response=await client.post('/api/auth',json={'code':'offline-code'},headers={'Origin':'https://123456.discordsays.com'})
                data=await response.json();self.assertEqual(response.status,200);self.assertFalse(data['user']['bound'])
                headers={'Authorization':'Bearer '+data['session']}
                denied=await client.get('/api/guild?number=1028077590&user_id=999',headers=headers)
                self.assertEqual(denied.status,403)
            with patch.object(server,'binding',return_value='bound-character') as check:
                response=await client.get('/api/guild?number=invalid',headers=headers)
                self.assertEqual(response.status,400);check.assert_called_once_with('123')
