import importlib.util,sys,types,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

spec=importlib.util.spec_from_file_location('activity_names_server',Path(__file__).resolve().parents[1]/'server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)

class NameTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_profiles_and_member_hosts(self):
        fetch=AsyncMock(return_value={'code':0,'result':{'member':{'base':{'nickname':'Resolved member','number_id':'1234567890'}}}})
        module=types.ModuleType('utility.wwm');module.get_bulk_players_info_multi_hostnum=fetch
        record={'rank_list':[{'pid':'team-composite','hostnum':10001,'player_info':{'leader':{'base':{'nickname':'Leader'}}},'ud':{'members':['leader','member'],'member':{'hostnum':10305}}}]}
        with patch.dict(sys.modules,{'utility.wwm':module}):result=await server.resolve_rank_names(record)
        fetch.assert_awaited_once_with({10305:['member']},fields=['base'])
        self.assertEqual(result['player_names']['leader']['name'],'Leader')
        self.assertEqual(result['player_names']['member']['name'],'Resolved member')
        self.assertNotIn('player_names',record)

    async def test_profile_outage_keeps_ranking(self):
        module=types.ModuleType('utility.wwm');module.get_bulk_players_info_multi_hostnum=AsyncMock(side_effect=RuntimeError('offline'))
        record={'rank_list':[{'pid':'player','hostnum':10301,'score':-10,'ud':{}}]}
        with patch.dict(sys.modules,{'utility.wwm':module}):result=await server.resolve_rank_names(record)
        self.assertEqual(result['rank_list'],record['rank_list'])
        self.assertEqual(result['player_names'],{})
