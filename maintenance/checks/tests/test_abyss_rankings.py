import hashlib
from unittest.mock import AsyncMock, patch
from test_wwm_offline_improvements import OfflineCase, BOT, wwm, ranking
from utility.abyss_rankings import AbyssAssets, AbyssRankingView, result_text

class AbyssTests(__import__("unittest").IsolatedAsyncioTestCase):
    setUp = OfflineCase.setUp
    cog = OfflineCase.cog
    interaction = OfflineCase.interaction
    response = OfflineCase.response
    def test_catalogue_and_original_art(self):
        assets=AbyssAssets(BOT/'data/abyss_assets')
        self.assertEqual(len(assets.data()['trials']),54)
        self.assertEqual(assets.resolve('Ye Wanshan')['id'],3)
        for entry in assets.data()['trials']:
            self.assertEqual(hashlib.sha256(assets.image(entry).read_bytes()).hexdigest(),entry['sha256'])
        self.assertIsNone(assets.image({'image':'../../settings.py'}))
    def test_scores(self):
        self.assertEqual(result_text([-32.756],'overall'),'0:32.756')
        self.assertEqual(result_text([-1704067200],'no_hit'),'2024-01-01')
        self.assertEqual(result_text([], 'overall'),'—')
    async def test_command_route(self):
        cog=self.cog();interaction=self.interaction()
        with patch.object(wwm,'get_rank_list',AsyncMock(return_value=self.response())) as api:
            await ranking.RankingCog.ranking_view.callback(cog,interaction,'abyss:3')
        self.assertIn('recall_tag2_4',api.call_args.args[0])
        view=interaction.edit_original_response.call_args.kwargs['view']
        self.assertIsInstance(view,AbyssRankingView)
        for f in interaction.edit_original_response.call_args.kwargs['attachments']:f.close()
    async def test_removed_mode_no_request(self):
        with patch.object(wwm,'get_rank_list',AsyncMock()) as api:
            interaction=self.interaction()
            await ranking.RankingCog.ranking_view.callback(self.cog(),interaction,'abyss:3',abyss_mode='build')
            api.assert_not_called()
            self.assertIn('Fastest clears',interaction.edit_original_response.call_args.kwargs['content'])
            self.assertNotIn('preset', ranking.RankingCog.ranking_view._params)
            self.assertEqual([c.value for c in ranking.RankingCog.ranking_view._params['abyss_mode'].choices], ['overall','no_hit','path'])
    async def test_target_first_rank_and_unknown_skills(self):
        cog=self.cog();entry=cog.abyss_assets.resolve('3')
        view=AbyssRankingView(cog,123,cog.abyss_assets,entry,target='p')
        response=self.response();response['result']['my_rank']=0
        response['result']['rank_list'][0].update(pid='p',ud={'battle_skills':[999,0,0,0,0,0,0]})
        with patch.object(wwm,'get_rank_list',AsyncMock(return_value=response)):
            await view.load(jump=True)
        self.assertTrue(view.target_found)
        self.assertEqual(view.mapped('martial',[999,0]),'ID 999, Empty')
    async def test_empty_error_and_page_clamp(self):
        cog=self.cog();view=AbyssRankingView(cog,123,cog.abyss_assets,cog.abyss_assets.resolve('3'))
        with patch.object(wwm,'get_rank_list',AsyncMock(return_value={'code':0,'result':{'rank_total_len':0}})):
            await view.load(99)
        self.assertEqual(view.page,1);self.assertEqual(view.rows,[])
        with patch.object(wwm,'get_rank_list',AsyncMock(return_value={'code':1,'result':None})):
            await view.load()
        self.assertTrue(view.error)

    async def test_abyss_category_autocomplete(self):
        from types import SimpleNamespace
        cog=self.cog()
        interaction=SimpleNamespace(namespace=SimpleNamespace(rank_type=None))
        for query in ('abyss', 'Abyss ', 'abyss:', 'abyss trial'):
            choices=await cog.dungeon_acomplete(interaction,query)
            self.assertTrue(choices,query)
            self.assertLessEqual(len(choices),25)
            self.assertTrue(all(c.value.startswith('abyss:') for c in choices))
        for query in ('abyss Ye Wanshan','abyss:3','abyss trial Ye Wanshan'):
            choices=await cog.dungeon_acomplete(interaction,query)
            self.assertEqual([c.value for c in choices],['abyss:3'])
        interaction.namespace.rank_type='abyss'
        choices=await cog.dungeon_acomplete(interaction,'Ye Wanshan')
        self.assertEqual([c.value for c in choices],['abyss:3'])

    async def test_path_command_uses_boss_path_board_and_name(self):
        cog=self.cog();interaction=self.interaction()
        with patch.object(wwm,'get_rank_list',AsyncMock(return_value=self.response())) as api:
            await ranking.RankingCog.ranking_view.callback(cog,interaction,'abyss:3',abyss_mode='path')
        self.assertEqual(api.call_args.args[0],'DynamicRank___tag1_recall_liupai___recall_tag2_4_12')
        kwargs=interaction.edit_original_response.call_args.kwargs
        payload=str(kwargs['view'].to_components())
        self.assertIn('Bellstrike',payload)
        self.assertNotIn('Preset',payload)
        self.assertNotIn('preset',ranking.RankingCog.ranking_view._params)
        for file in kwargs['attachments']:file.close()

    async def test_special_encounters_have_no_path_requests(self):
        for trial in ('abyss:1','abyss:75'):
            interaction=self.interaction()
            with patch.object(wwm,'get_rank_list',AsyncMock()) as api:
                await ranking.RankingCog.ranking_view.callback(self.cog(),interaction,trial,abyss_mode='path')
            api.assert_not_called()
            self.assertIn('special rules',interaction.edit_original_response.call_args.kwargs['content'])

    async def test_missing_or_ambiguous_path_fails_without_request(self):
        from copy import deepcopy
        for paths in ([],[{'name':None,'rank_name':'example'}],[{'name':'A','rank_name':'a'},{'name':'B','rank_name':'b'}]):
            cog=self.cog();entry=deepcopy(cog.abyss_assets.resolve('3'));entry['paths']=paths
            with patch.object(cog.abyss_assets,'resolve',return_value=entry), patch.object(wwm,'get_rank_list',AsyncMock()) as api:
                interaction=self.interaction()
                await ranking.RankingCog.ranking_view.callback(cog,interaction,'abyss:3',abyss_mode='path')
            api.assert_not_called()
            self.assertIn('No single named',interaction.edit_original_response.call_args.kwargs['content'])

    async def test_path_keeps_encounter_recorded_martial_arts(self):
        cog=self.cog();entry=cog.abyss_assets.resolve('55')
        view=AbyssRankingView(cog,123,cog.abyss_assets,entry,mode='path',target='p')
        response=self.response();response['result']['my_rank']=0
        response['result']['rank_list'][0].update(pid='p',ud={'battle_skills':[10104,0,0,151,152,153,154]})
        with patch.object(wwm,'get_rank_list',AsyncMock(return_value=response)):
            await view.load(jump=True)
        self.assertEqual(view.rank_name,'DynamicRank___tag1_recall_liupai___recall_tag2_56_69')
        self.assertIn('ID 10104',str(view.to_components()))
        self.assertTrue(view.target_found)

    async def test_recorded_martial_names_reuse_profile_mapping(self):
        cog=self.cog();view=AbyssRankingView(cog,123,cog.abyss_assets,cog.abyss_assets.resolve('3'))
        self.assertEqual(view.mapped('martial',[10102,10202]),'Nameless Sword, Nameless Spear')
        self.assertEqual(view.mapped('martial',[10101,10201]),'Strategic Sword, Heavenquaker Spear')
        self.assertEqual(view.mapped('martial',[10104,0]),'ID 10104, Empty')
        view.names['martial']={'10102':'Extracted sword name'}
        self.assertEqual(view.mapped('martial',[10102]),'Extracted sword name')
