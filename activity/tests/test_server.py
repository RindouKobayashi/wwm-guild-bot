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
            self.assertEqual((await client.get('/api/player?identifier=1028077590')).status,403)
            self.assertEqual((await client.post('/api/auth',json={'code':'x'})).status,403)
            self.assertEqual((await client.get('/server.py')).status,404)
            self.assertEqual((await client.get('/app.js')).headers.get('Cache-Control'),'no-cache')
    async def test_discord_requires_identity_and_never_serves_snapshot(self):
        with patch.dict(os.environ,DISCORD_CLIENT_ID='123456',DISCORD_CLIENT_SECRET='offline-test'):
            app=server.create_app(live=True)
        # Cleanup should not import the game transport; no live request is made.
        app.on_cleanup.clear()
        async with TestClient(TestServer(app)) as client:
            for path in ('/api/rank?board=abyss:3','/api/guild?number=1028077590','/api/player?identifier=1028077590'):
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
                self.assertIsNone(data['user']['character_number'])
                headers={'Authorization':'Bearer '+data['session']}
                denied=await client.get('/api/guild?number=1028077590&user_id=999',headers=headers)
                self.assertEqual(denied.status,403)
                denied=await client.get('/api/player?identifier=1028077590&user_id=999',headers=headers)
                self.assertEqual(denied.status,403)
            with patch.object(server,'binding',return_value='bound-character') as check:
                response=await client.get('/api/guild?number=invalid',headers=headers)
                self.assertEqual(response.status,400);check.assert_called_once_with('123')
            with patch.object(server,'ClientSession',return_value=context(http)),patch.object(server,'binding',return_value='0012345678'):
                response=await client.post('/api/auth',json={'code':'offline-code'},headers={'Origin':'https://123456.discordsays.com'})
                data=await response.json();self.assertEqual(data['user']['character_number'],'0012345678')
                headers={'Authorization':'Bearer '+data['session']}
                for identifier in ('', '123', '1'*65):
                    self.assertEqual((await client.get('/api/player',params={'identifier':identifier},headers=headers)).status,400)

    async def test_player_profile_uses_resolved_host_and_allows_missing_guild(self):
        import sys,types
        def context(value):
            cm=MagicMock();cm.__aenter__=AsyncMock(return_value=value);cm.__aexit__=AsyncMock(return_value=False);return cm
        oauth=MagicMock(status=200);oauth.json=AsyncMock(return_value={'access_token':'offline','expires_in':3600})
        identity=MagicMock(status=200);identity.json=AsyncMock(return_value={'id':'123','username':'Viewer'})
        http=MagicMock();http.post.return_value=context(oauth);http.get.return_value=context(identity)
        transport=types.ModuleType('utility.wwm')
        transport.resolve_player_identifier=AsyncMock(return_value=('internal-pid',10403,{}))
        transport.fetch_player_data_by_pid=AsyncMock(return_value={'code':0,'result':{'base':{'nickname':'Player','number_id':'0012345678','school':4},'club':{'club_id':'guild','hostnum':10404}}})
        transport.get_custom_guild_info=AsyncMock(side_effect=RuntimeError('guild unavailable'))
        constants=types.ModuleType('utility.api_constants');constants.SCHOOL_NAMES={4:'Silver Needle'}
        with patch.dict(os.environ,DISCORD_CLIENT_ID='123456',DISCORD_CLIENT_SECRET='offline'):
            app=server.create_app(live=True)
        app.on_cleanup.clear()
        with patch.dict(sys.modules,{'utility.wwm':transport,'utility.api_constants':constants}),patch.object(server,'binding',return_value='0012345678'),patch.object(server,'ClientSession',return_value=context(http)):
            async with TestClient(TestServer(app)) as client:
                auth=await client.post('/api/auth',json={'code':'offline'},headers={'Origin':'https://123456.discordsays.com'})
                headers={'Authorization':'Bearer '+(await auth.json())['session']}
                response=await client.get('/api/player?identifier=ExactNickname',headers=headers)
                self.assertEqual(response.status,200)
                data=await response.json();self.assertEqual(data['sect'],'Silver Needle')
                self.assertEqual(data['guild'],{'status':'joined','name':None})
                self.assertNotIn('internal-pid',str(data))
                transport.fetch_player_data_by_pid.assert_awaited_once()
                self.assertEqual(transport.fetch_player_data_by_pid.call_args.kwargs['hostnum'],10403)
                self.assertIn('kongfu',transport.fetch_player_data_by_pid.call_args.kwargs['fields'])
                self.assertIn('gameplay_resources',transport.fetch_player_data_by_pid.call_args.kwargs['fields'])
                transport.get_custom_guild_info.assert_awaited_once_with('guild',hostnum=10404,fields={'base':[]})
                transport.get_player_combat_plan=AsyncMock(return_value={'code':0,'result':{'wear_equips':{'1':{'No':999999999,'ex':{'base_affixes':[[1001,1]]}}}}})
                equipment_response=await client.get('/api/player?identifier=ExactNickname&section=equipment',headers=headers)
                self.assertEqual(equipment_response.status,200)
                gear=await equipment_response.json();self.assertEqual(gear['items'][0]['slot'],'Primary Weapon')
                transport.get_player_combat_plan.assert_awaited_once_with('internal-pid',10403)
                transport.fetch_player_data_by_pid.return_value={'code':0,'result':{'title_prop':{'titles':{'3001':{'level':1}}}}}
                collection_response=await client.get('/api/player?identifier=ExactNickname&section=collections',headers=headers)
                self.assertEqual(collection_response.status,200)
                self.assertEqual((await collection_response.json())['buckets']['titles'][0]['name'],'Rainbow Slayer')
                transport.fetch_player_data_by_pid.return_value={'code':0,'result':{'homeworld_data':{'home_info':{'home-id':{}}}}}
                transport.get_homeland_info=AsyncMock(return_value={'code':0,'result':{'home-id':{'homeland_base':{'name':'Home','level':0}}}})
                home_response=await client.get('/api/player?identifier=ExactNickname&section=homestead',headers=headers)
                self.assertEqual(home_response.status,200)
                transport.get_homeland_info.assert_awaited_once_with(hostnum2pids={10403:['home-id']})
                self.assertEqual((await home_response.json())['rows'][1]['value'],0)
                transport.fetch_player_data_by_pid.return_value={'code':0,'result':{'jieyuan_info':{'xialv_info':{'1':{'pid':'partner-pid','hostnum':10406,'favor':0}}}}}
                transport.get_bulk_players_info_multi_hostnum=AsyncMock(return_value={'code':0,'result':{'partner-pid':{'base':{'nickname':'Partner','number_id':'1234567890'}}}})
                transport.get_topics_likes=AsyncMock(return_value={'code':0,'result':{'topic':{'n_likes':0}}})
                social_response=await client.get('/api/player?identifier=ExactNickname&section=social',headers=headers)
                self.assertEqual(social_response.status,200)
                social=await social_response.json();self.assertEqual(social['likes'],0)
                self.assertEqual(social['partners'][0]['name'],'Partner')
                self.assertNotIn('partner-pid',str(social))
                transport.get_bulk_players_info_multi_hostnum.assert_awaited_once_with({10406:['partner-pid']},fields=['base'])
                transport.resolve_player_identifier.return_value=(None,None,None)
                self.assertEqual((await client.get('/api/player?identifier=Missing',headers=headers)).status,404)
                self.assertEqual((await client.get('/api/player?identifier=Player&section=chat',headers=headers)).status,400)
