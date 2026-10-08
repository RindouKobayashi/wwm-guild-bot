"""Offline guild history regressions: no bot login or game API traffic."""
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from utility.guild_battles import normalize_reports,collect_battle_ids
from utility.guild_battle_view import battle_embed,PlayerGuildBattleView,metric

def report(pid='target',**extra):
    return {'SpaceId':'one','TimeStamp':1000,'EnterMode':3,'IsRedWin':True,'IsBlueWin':False,
        'RedClubId':'ours','BlueClubId':'enemy','StatisticsJsonStr':json.dumps({pid:{'camp':1,'kills':0,'total_damage':1234}}),**extra}

class NormalizationTests(unittest.TestCase):
    def test_play_contains_current_and_historical_ids(self):
        fixture={'play':{'league_info':{'recent_space_idx':['one','two']},
            'season_2_league_pk_info':{4:{'spaceidx':['two','three']}}}}
        self.assertEqual(collect_battle_ids(fixture),['one','two','three'])
    def test_only_target_and_deduplicate(self):
        matches,bad=normalize_reports([report(),report(),report('other')],'target')
        self.assertEqual(len(matches),1);self.assertEqual(bad,0)
        self.assertEqual(matches[0]['outcome'],'Win')
        self.assertNotIn('healing_done',matches[0]['stats'])

    def test_bad_stats_and_unknown_camp(self):
        matches,bad=normalize_reports([report(StatisticsJsonStr='{'),report(StatisticsJsonStr=json.dumps({'target':{'camp':9}}))],'target')
        self.assertEqual(bad,1);self.assertEqual(matches[0]['outcome'],'Unavailable')
        self.assertIsNone(matches[0]['opponent_id'])

    def test_score_mode_and_equal_flags(self):
        matches,_=normalize_reports([report(EnterMode=1,RedNewScore=110,RedOldScore=100,BlueNewScore=120,BlueOldScore=100)],'target')
        self.assertEqual(matches[0]['outcome'],'Loss')
        matches,_=normalize_reports([report(IsBlueWin=True)],'target')
        self.assertEqual(matches[0]['outcome'],'Unavailable')

    def test_embed_missing_not_zero(self):
        matches,_=normalize_reports([report()],'target')
        e=battle_embed({'status':'ok','matches':matches,'scanned':2,'names':{'enemy':'Opponent'}},'Target','123')
        fields={f.name:f.value for f in e.fields}
        self.assertEqual(fields['Kills'],'0');self.assertEqual(fields['Healing'],'—')
        self.assertIn('Opponent',e.description)
        for status in ('no_guild','unavailable'):
            self.assertTrue(battle_embed({'status':status,'matches':[]},'Target','123').description)
        self.assertEqual(metric(float('nan')),'—')

class ViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_gate_checks_requesting_user_before_fetch(self):
        # Existing test harness imports cogs with synthetic settings, never bot startup.
        import test_wwm_offline_improvements
        from cogs import wwm_cog
        view=object.__new__(wwm_cog.PlayerProfileView)
        view.owner_id=123
        interaction=SimpleNamespace(user=SimpleNamespace(id=123),response=SimpleNamespace(
            is_done=lambda:False,send_message=AsyncMock(),defer=AsyncMock()))
        cursor=SimpleNamespace(fetchone=AsyncMock(return_value=None))
        conn=SimpleNamespace(execute=AsyncMock(return_value=cursor))
        context=AsyncMock();context.__aenter__.return_value=conn
        with patch.object(wwm_cog.aiosqlite,'connect',return_value=context),patch('utility.guild_battles.get_player_guild_battles',new_callable=AsyncMock) as fetch:
            await view._handle_guild_battles(interaction)
            fetch.assert_not_awaited();interaction.response.send_message.assert_awaited_once()
            self.assertEqual(conn.execute.call_args.args[1],(123,))
            cursor.fetchone.return_value=(1,)
            self.assertTrue(await view._authorize_guild_battles(interaction))
            cursor.fetchone.return_value=None
            self.assertFalse(await view._authorize_guild_battles(interaction))

    async def test_lookup_no_guild_and_failed_profile(self):
        import utility
        from utility import guild_battles
        fake=SimpleNamespace(get_bulk_players_info=AsyncMock(return_value={'code':0,'result':{'edge':{'club':{}}}}))
        settings=SimpleNamespace(WWM_UID='fixture',RANK_GET_RANKLIST_URL='https://example.invalid/rank')
        with patch.object(utility,'wwm',fake,create=True),patch.dict(sys.modules,{'settings':settings}):
            result=await guild_battles.get_player_guild_battles('edge',1)
            self.assertEqual(result['status'],'no_guild')
            fake.get_bulk_players_info.return_value=None
            result=await guild_battles.get_player_guild_battles('edge',1)
            self.assertEqual(result['status'],'unavailable')

    async def test_lookup_filters_other_players_and_partial_failure(self):
        import utility
        from utility import guild_battles
        fake=SimpleNamespace(
            get_bulk_players_info=AsyncMock(return_value={'code':0,'result':{'edge2':{'club':{'club_id':'guild'}}}}),
            get_custom_guild_info=AsyncMock(return_value={'code':0,'result':{'play':{'league_info':{'recent_space_idx':[str(i) for i in range(11)]}}}}),
            _wwm_api_post=AsyncMock(side_effect=[{'code':0,'result':{'histories':[report('edge2'),report('other',SpaceId='other')]}},None]),
            get_club_brief_info_batch=AsyncMock(return_value=[]))
        settings=SimpleNamespace(WWM_UID='fixture',RANK_GET_RANKLIST_URL='https://example.invalid/rank')
        with patch.object(utility,'wwm',fake,create=True),patch.dict(sys.modules,{'settings':settings}):
            result=await guild_battles.get_player_guild_battles('edge2',1)
            self.assertTrue(result['partial']);self.assertEqual(len(result['matches']),1)
            self.assertIn('play',fake.get_custom_guild_info.call_args.kwargs['fields'])
            await guild_battles.get_player_guild_battles('edge2',1)
            self.assertEqual(fake._wwm_api_post.await_count,2)

    async def test_owner_and_current_binding(self):
        authorize=AsyncMock(return_value=False)
        view=PlayerGuildBattleView(1,{'status':'no_guild','matches':[]},'Target','123',authorize)
        interaction=SimpleNamespace(user=SimpleNamespace(id=2),response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction));authorize.assert_not_awaited()
        interaction.user.id=1
        self.assertFalse(await view.interaction_check(interaction));authorize.assert_awaited_once()

    async def test_profile_handler_edits_original_without_followup(self):
        import test_wwm_offline_improvements
        from cogs import wwm_cog
        view=object.__new__(wwm_cog.PlayerProfileView)
        view.owner_id=123;view.player_pid='target';view.player_hostnum=1
        view.player_nickname='Target';view.number_id='123'
        view._authorize_guild_battles=AsyncMock(return_value=True)
        interaction=SimpleNamespace(user=SimpleNamespace(id=123),response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),followup=SimpleNamespace(send=AsyncMock()))
        matches,_=normalize_reports([report()],'target')
        with patch('utility.guild_battles.load_participant_names',new_callable=AsyncMock),patch('utility.guild_battles.get_player_guild_battles',new_callable=AsyncMock,return_value={'status':'ok','matches':matches,'scanned':1}) as fetch:
            await view._handle_guild_battles(interaction)
            fetch.assert_awaited_once_with('target',1)
        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_not_awaited()
        payload=interaction.edit_original_response.call_args.kwargs
        self.assertNotIn('embed',payload)
        self.assertEqual(payload['attachments'],[])
        self.assertIsInstance(payload['view'],wwm_cog.LayoutView)
        self.assertTrue(any(getattr(x,'label',None)=='Back to player profile' for x in payload['view'].walk_children()))
        payload['view'].stop()

    async def test_large_roster_component_limits_and_missing_totals(self):
        import discord
        stats={str(i):{'camp':1 if i<40 else 2,'hostnum':1,'kills':0,'total_damage':i*100} for i in range(80)}
        stats['unknown']={'camp':9,'healing_done':0}
        matches,_=normalize_reports([report(StatisticsJsonStr=json.dumps(stats))],'0')
        matches[0]['player_names']={pid:{'name':'Participant '+pid} for pid in stats}
        view=PlayerGuildBattleView(1,{'status':'ok','matches':matches,'scanned':1},'Target','123',AsyncMock(return_value=True),back=AsyncMock())
        view.details=True;view.participant='0';view.rebuild()
        self.assertLessEqual(len(list(view.walk_children())),40)
        text='\n'.join(x.content for x in view.walk_children() if isinstance(x,discord.ui.TextDisplay))
        self.assertLessEqual(len(text),4000)
        self.assertIn('★ Searched player',text)
        self.assertIn('Healing **—** (0/40)',text)
        view.roster_page=13;view.rebuild()
        text='\n'.join(x.content for x in view.walk_children() if isinstance(x,discord.ui.TextDisplay))
        self.assertIn('Participant unknown',text)
        self.assertIn('Unknown teams stay unassigned',text)
        view.stop()

    async def test_discord_select_limit(self):
        matches,_=normalize_reports([report(SpaceId=str(i)) for i in range(39)],'target')
        view=PlayerGuildBattleView(1,{'status':'ok','matches':matches,'scanned':39},'Target','123',AsyncMock(return_value=True))
        menu=next(x for x in view.walk_children() if hasattr(x,'options'))
        self.assertEqual(len(menu.options),25)
        view.page=1;view.rebuild()
        menu=next(x for x in view.walk_children() if hasattr(x,'options'))
        self.assertEqual(len(menu.options),14)

if __name__=='__main__':unittest.main()
