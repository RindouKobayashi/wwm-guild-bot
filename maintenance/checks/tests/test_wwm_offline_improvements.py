"""Offline regression checks. No bot instance, credentials, login or API traffic.

Run with the bot environment: python -B -m unittest discover -s maintenance/checks/tests -p test_wwm_offline_improvements.py
"""
import ast
import asyncio
import csv
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

BOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
sys.path.insert(0, str(BOT))

# Import the real cog/view/utility implementations with synthetic settings.
# Never load settings.py, dotenv, bot.py, persisted credentials or cog_load.
settings = types.ModuleType('settings')
for relative in ('utility/wwm.py', 'utility/api_constants.py', 'cogs/ranking_cog.py', 'cogs/wwm_cog.py'):
    tree = ast.parse((BOT / relative).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'settings':
            for alias in node.names:
                setattr(settings, alias.name, 'offline-fixture')
settings.BASE_DIR = BOT
settings.logger = logging.getLogger('offline-wwm-tests')
settings.logger.addHandler(logging.NullHandler())
settings.branch = 'dev'
sys.modules['settings'] = settings

from utility import wwm
from utility.affix_display import format_affix_range
from utility.affix_mapper import _map_recursive
from utility.dungeon_assets import DungeonAssets
from cogs import ranking_cog as ranking
from cogs.wwm_cog import PlayerProfileView, build_collection_data
from maintenance.assets.import_dungeon_assets import build as build_assets, choose_asset, direct_resources, dungeon_family, resolve_boss_art, resolve_dungeon_card, resolve_reviewed_card, copy_art


class OfflineCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network = patch.object(wwm.aiohttp.ClientSession, '_request', side_effect=AssertionError('Network disabled in offline tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def cog(self):
        cog = ranking.RankingCog(types.SimpleNamespace())
        cog._resolve_user_pid = AsyncMock(return_value=None)
        cog._fetch_entry = AsyncMock(return_value=None)
        cog.record_usage = AsyncMock()
        cog._respond = AsyncMock()
        return cog

    def interaction(self):
        return types.SimpleNamespace(user=types.SimpleNamespace(id=123), edit_original_response=AsyncMock(), response=types.SimpleNamespace(defer=AsyncMock()))

    def response(self, total=20, my_rank=-1):
        return {'code':0,'result':{'rank_total_len':total,'my_rank':my_rank,'my_data':{'score':-75},'rank_list':[{'score':-75,'ud':None,'player_info':{'base':{'nickname':'Test'}}}]}}

    async def test_extracted_lookup_ignores_community_names_and_never_reads_db(self):
        cog=self.cog()
        cog.load_cache=AsyncMock(side_effect=AssertionError('Registry must not be read'))
        cog._entries=[{'rank_type':'hr','dungeon_id':1,'name':'Invented custom alias'}]
        result=await cog.resolve_dungeon_input('Dream Jinming Pool', 'hr')
        self.assertEqual((result['status'],result['dungeon_id']),('ok',1))
        self.assertEqual((await cog.resolve_dungeon_input('Invented custom alias'))['status'],'not_found')
        choices=await cog._autocomplete_choices('Dream Jinming Pool','hr')
        self.assertTrue(any(c.value=='hr:1' for c in choices))
        cog.load_cache.assert_not_awaited()

    async def test_numeric_ids_keep_team_namespace(self):
        cog=self.cog()
        self.assertEqual((await cog.resolve_dungeon_input('hr:1'))['entry']['name'],'Dream Jinming Pool')
        self.assertEqual((await cog.resolve_dungeon_input('st:1'))['entry']['name'],'Formless Pass')
        self.assertEqual((await cog.resolve_dungeon_input('1'))['status'],'multiple')
        self.assertEqual((await cog.resolve_dungeon_input('99999','st'))['dungeon_id'],99999)

    async def test_number_resolution_preserves_pid_and_host_after_full_fetch(self):
        lookup={'code':0,'result':{'id':'wanted','hostnum':10403}}
        data={'code':0,'result':{'wanted':{'base':{'nickname':'Offline'}}}}
        with patch.object(wwm,'_wwm_api_post',AsyncMock(side_effect=[lookup,data])):
            pid,host,profile=await wwm.resolve_player_identifier('1234567890')
        self.assertEqual((pid,host),('wanted',10403))
        self.assertEqual(profile['base']['nickname'],'Offline')

    async def test_numeric_lookup_failure_does_not_fall_through_to_nickname(self):
        with patch.object(wwm,'get_player_info',AsyncMock(return_value={'code':1,'result':{'id':'wrong'}})), patch.object(wwm,'find_people_by_nickname',AsyncMock()) as nickname:
            self.assertEqual(await wwm.resolve_player_identifier('1234567890'),(None,None,None))
            nickname.assert_not_awaited()

    async def test_activity_is_derived_from_snapshot_without_network(self):
        now=int(ranking.discord.utils.utcnow().timestamp())
        view=PlayerProfileView(player_nickname='Offline',number_id='1234567890',online_hours=120,create_time=now-10*86400,owner_id=123)
        interaction=self.interaction()
        await view._handle_activity(interaction)
        text=json.dumps(view.to_components())
        self.assertIn('12.00 hours per calendar day',text)
        self.assertIn('120.0 hours',text)
        self.assertIn('not a live session tracker',text)

    async def test_current_bundle_attaches_real_dungeon_art(self):
        cog=self.cog();interaction=self.interaction()
        with patch.object(ranking,'get_rank_list',AsyncMock(return_value=self.response())):
            await cog.show_ranking_results(interaction,'hr',1)
        args=interaction.edit_original_response.call_args.kwargs
        self.assertEqual(len(args['attachments']),1)
        self.assertIn('attachment://dungeon-banner.png',json.dumps(args['view'].to_components()))

    async def test_activity_handles_missing_and_invalid_timestamps(self):
        view=PlayerProfileView(player_nickname='Offline',number_id='1234567890',online_hours=float('nan'),create_time=float('inf'),owner_id=123)
        await view._handle_activity(self.interaction())
        text=json.dumps(view.to_components())
        self.assertNotIn('Average since creation',text)
        self.assertNotIn('Recorded playtime',text)

    async def test_player_profile_and_collection_construct_offline(self):
        profile=PlayerProfileView(player_nickname='Offline',number_id='1234567890',collection_data=build_collection_data({}))
        self.assertTrue(profile.to_components())

    async def test_full_ranking_page_stays_within_text_budget(self):
        cog=self.cog();view=ranking.RankingResultsView(cog,'hr',1,'rank_team10_dungeon_1')
        view.rank_list=[{'score':-75,'ud':{'ts':1791336898},'player_info':{'base':{'nickname':'*'*48}}} for _ in range(20)]
        view.total_entries=500;view.total_pages=25;view._rebuild()
        def texts(items):
            for item in items:
                if item.get('type')==10:yield item['content']
                yield from texts(item.get('components',[]))
        self.assertLessEqual(sum(map(len,texts(view.to_components()))),4000)

    async def test_rank_art_is_attached_and_serializes_offline(self):
        cog = self.cog(); interaction = self.interaction()
        candidate = cog.dungeon_assets.get('hr',1)
        root=BOT/'data/ranking_assets'
        run=root/json.loads((root/'latest.json').read_text())['run']
        candidate['path']=run/candidate['file']
        cog.dungeon_assets=types.SimpleNamespace(get=lambda *args:candidate, entries=lambda:[])
        with patch.object(ranking,'get_rank_list' ,AsyncMock(return_value=self.response())):
            await cog.show_ranking_results(interaction,'hr',1)
        cog._respond.assert_not_awaited()
        args = interaction.edit_original_response.call_args.kwargs
        self.assertEqual(len(args['attachments']),1)
        text=json.dumps(args['view'].to_components())
        self.assertIn('attachment://dungeon-banner.png',text)
        self.assertIn('Original dungeon selection artwork',text)
        self.assertIn('specify a player',text)

    async def test_usage_counter_failure_keeps_successful_view(self):
        cog=self.cog();cog.record_usage.side_effect=RuntimeError('database unavailable')
        interaction=self.interaction()
        with patch.object(ranking,'get_rank_list',AsyncMock(return_value=self.response())):
            await cog.show_ranking_results(interaction,'hr',1)
        self.assertEqual(interaction.edit_original_response.await_count,1)
        self.assertIsInstance(interaction.edit_original_response.call_args.kwargs['view'],ranking.RankingResultsView)

    async def test_missing_art_is_text_only(self):
        cog=self.cog();cog.dungeon_assets=types.SimpleNamespace(get=lambda *args:None, entries=lambda:[])
        interaction=self.interaction()
        with patch.object(ranking,'get_rank_list',AsyncMock(return_value=self.response())):
            await cog.show_ranking_results(interaction,'hr',999)
        args=interaction.edit_original_response.call_args.kwargs
        self.assertEqual(args['attachments'],[])
        self.assertNotIn('attachment://',json.dumps(args['view'].to_components()))

    async def test_explicit_page_one_does_not_rejump_to_target(self):
        cog=self.cog();interaction=self.interaction()
        api=AsyncMock(return_value=self.response(my_rank=4))
        with patch.object(ranking,'get_rank_list',api):
            await cog.show_ranking_results(interaction,'st',1,target_pid='target',jump_to_target=False)
        self.assertEqual(api.await_count,1)
        self.assertEqual(api.call_args.kwargs['page'],1)

    async def test_initial_player_lookup_jumps_once(self):
        cog=self.cog();interaction=self.interaction()
        api=AsyncMock(return_value=self.response(total=60,my_rank=45))
        with patch.object(ranking,'get_rank_list',api):
            await cog.show_ranking_results(interaction,'st',1,target_pid='target')
        self.assertEqual([c.kwargs['page'] for c in api.await_args_list],[1,3])
        self.assertEqual(interaction.edit_original_response.call_args.kwargs['view'].page,3)

    async def test_out_of_range_page_is_refetched(self):
        cog=self.cog();interaction=self.interaction()
        api=AsyncMock(return_value=self.response(total=20))
        with patch.object(ranking,'get_rank_list',api):
            await cog.show_ranking_results(interaction,'st',1,page=999)
        self.assertEqual([c.kwargs['page'] for c in api.await_args_list],[999,1])
        self.assertEqual(interaction.edit_original_response.call_args.kwargs['view'].page,1)

    async def test_prev_navigation_disables_target_jump(self):
        cog=self.cog();cog.show_ranking_results=AsyncMock()
        view=ranking.RankingResultsView(cog,'st',1,'rank_team_dungeon_1',page=2,target_pid='target',owner_id=123)
        await view._handle_prev(self.interaction())
        self.assertFalse(cog.show_ranking_results.call_args.kwargs['jump_to_target'])

    async def test_team_back_keeps_gallery_reference(self):
        cog=self.cog();back=ranking.RankingResultsView(cog,'hr',1,'rank_team10_dungeon_1',owner_id=123)
        back.banner={'path':Path('offline-fixture.png'),'shared_by_boards':1}
        team=ranking.TeamDetailView(cog,'hr',back.rank_name,1,-75,[],back,owner_id=123)
        self.assertIn('attachment://dungeon-banner.png',json.dumps(team.to_components()))
        interaction=self.interaction();await team._handle_back(interaction)
        self.assertNotIn('attachments',interaction.edit_original_response.call_args.kwargs)

    async def test_explicit_request_token_is_honored(self):
        response=MagicMock(status=200)
        response.read=AsyncMock(return_value=wwm.msgpack.packb({'code':0}))
        context=MagicMock();context.__aenter__=AsyncMock(return_value=response);context.__aexit__=AsyncMock(return_value=False)
        session=MagicMock();session.post.return_value=context
        with patch.object(wwm,'get_session',AsyncMock(return_value=session)),patch.object(wwm,'get_default_headers',AsyncMock(return_value={'h72-ms-token':'default'})):
            result=await wwm._wwm_api_post('https://offline.invalid',{},token='explicit')
        self.assertEqual(result,{'code':0});self.assertEqual(session.post.call_args.kwargs['headers']['h72-ms-token'],'explicit')

    async def test_player_lookup_uses_resolved_server_and_exact_pid(self):
        post=AsyncMock(side_effect=[{'code':0,'result':{'id':'wanted','hostnum':10403}}, {'code':0,'result':{'other':{'base':{}},'wanted':{'base':{'nickname':'Right'}}}}])
        with patch.object(wwm,'_wwm_api_post',post):result=await wwm.get_player_info('1234567890')
        self.assertEqual(post.await_args_list[1].args[1]['hostnum2pids'],{10403:['wanted']})
        self.assertEqual(result['result']['base']['nickname'],'Right')

    async def test_missing_requested_pid_does_not_return_another_player(self):
        lookup={'code':0,'result':{'id':'wanted','hostnum':10403}}
        with patch.object(wwm,'_wwm_api_post',AsyncMock(side_effect=[lookup,{'code':0,'result':{'other':{'base':{}}}}])):
            result=await wwm.get_player_info('1234567890')
        self.assertEqual(result,lookup)

    async def test_pid_fetch_never_accepts_first_unrelated_record(self):
        with patch.object(wwm,'_wwm_api_post',AsyncMock(return_value={'code':0,'result':{'other':{'base':{}}}})):
            self.assertIsNone(await wwm.fetch_player_data_by_pid('wanted'))

    async def test_ranking_error_uses_v2_view(self):
        cog=self.cog();interaction=self.interaction()
        with patch.object(ranking,'get_rank_list',AsyncMock(side_effect=RuntimeError('private error'))):
            await cog.show_ranking_results(interaction,'hr',1)
        args=interaction.edit_original_response.call_args.kwargs
        self.assertIsNone(args['content']);self.assertEqual(args['attachments'],[])
        self.assertNotIn('private error',json.dumps(args['view'].to_components()))


class PureTests(unittest.TestCase):
    def fixture(self, root, resources):
        (root/'dungeons').mkdir();(root/'dungeon_library').mkdir()
        ranks=[{'team':'hr','dungeon_id':'1','rank_name':'rank_team10_dungeon_1','team_agrees':'True','leaderboard_title':'Test Top 500'}]
        dungeons=[{'name':'Test','mode':'normal','info_pic':resource,'row_id':str(i),'fuben_id':str(100+i)} for i,resource in enumerate(resources)]
        catalogue = {}
        for d in dungeons:
            key='trial_displays:'+d['row_id']
            instance=int(d['fuben_id']);entity=1000+instance;profile=2000+instance;replay=3000+instance
            boss='Test Boss '+d['row_id']
            catalogue[key]={'family':'trial_displays','id':int(d['row_id']),'names':[d['name']],
                            'variants':[{'raw':{'id':int(d['row_id']),'fuben_id':instance,'info_pic':d['info_pic']}}],
                            'art':[{'owner':key,'field':'info_pic','relation':'direct table field','resource':d['info_pic']}]}
            catalogue['instances:'+str(instance)]={'family':'instances','id':instance,'names':[boss],
                'variants':[{'raw':{'id':instance,'instance_group':instance,'fight_entity_id':[entity],'all_boss_id':profile}}]}
            catalogue['boss_profiles:'+str(profile)]={'family':'boss_profiles','id':profile,'names':[boss],
                'variants':[{'raw':{'boss_id':profile,'boss_no':entity}}]}
            replay_key='replay_encounters:'+str(replay)
            catalogue[replay_key]={'family':'replay_encounters','id':replay,'names':[boss],
                'variants':[{'raw':{'id':replay,'introduction_pic':d['info_pic']}}],
                'art':[{'owner':replay_key,'field':'introduction_pic','relation':'direct table field','resource':d['info_pic']}]}
        (root/'dungeon_library/dungeon_catalogue.json').write_text(json.dumps(catalogue),encoding='utf-8')
        for name,rows in [('trial_rank_ids.csv',ranks),('trial_dungeons.csv',dungeons)]:
            with (root/'dungeons'/name).open('w',encoding='utf-8',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

    def test_dungeon_family_keeps_difficulty_and_only_strips_explicit_metadata(self):
        self.assertEqual(dungeon_family('Rouge & Rite Challenge - Top 750 - Phase 2'),('rouge rite','challenge'))
        self.assertEqual(dungeon_family('Rouge & Rite Top 750 - Phase 1'),('rouge rite','normal'))
        self.assertEqual(dungeon_family('Dream Jinming Pool: Supreme Top 400'),('dream jinming pool supreme','normal'))
        self.assertNotEqual(dungeon_family('Drunken Verse 01 - Top 500')[0],dungeon_family('Drunken Verse')[0])
        self.assertNotEqual(dungeon_family('Formless Pass')[0],dungeon_family('Formless Mountain Pass')[0])

    def test_challenge_board_never_borrows_normal_mode_art(self):
        rank={'team':'hr','rank_name':'rank_team10_dungeon_99','leaderboard_title':'Test Challenge Top 500'}
        bindings={'rows':[{'name':'Test','raw':{'rank_id':'rank_team10_dungeon_1','game_list_image':'wulinlu_tongyou_pic_xj_09'}}]}
        self.assertEqual(resolve_dungeon_card(rank,bindings,{'assets':{},'references':{}})[0],[])

    def test_reported_wrong_group_has_one_distinct_dungeon_card_each(self):
        assets=DungeonAssets(BOT/'data/ranking_assets')
        expected={('hr',3):'11',('st',11):'15',('st',13):'17',('st',15):'17',('st',60004):'15'}
        for (team,identifier),suffix in expected.items():
            row=assets.get(team,identifier)
            self.assertEqual(row['art_status'],'verified_dungeon_card')
            self.assertEqual(len(row['images']),1)
            self.assertEqual(row['resource'],'wulinlu_tongyou_pic_xj_'+suffix)
            self.assertTrue(row['path'].is_file())

    def test_conflicting_explicit_card_bindings_are_rejected(self):
        rank={'team':'hr','rank_name':'rank_team10_dungeon_1','leaderboard_title':'Test Top 500'}
        rows=[{'raw':{'rank_id':rank['rank_name'],'game_list_image':'wulinlu_tongyou_pic_xj_'+n}} for n in ('09','11')]
        images,reason,_=resolve_dungeon_card(rank,{'rows':rows},{'assets':{},'references':{}})
        self.assertEqual(images,[]);self.assertIn('Conflicting',reason)

    def test_explicit_phase_replacement_uses_same_dungeon_card(self):
        resource='wulinlu_tongyou_pic_xj_15'
        rank={'team':'st','rank_name':'rank_team_dungeon_60004','leaderboard_title':'Abyssal Lament Top 500 - Phase 3'}
        bindings={'rows':[{'raw':{'rank_id':'rank_team_dungeon_11','rank_id_replace':[rank['rank_name']],'game_list_image':resource}}]}
        art={'references':{resource:['a']},'assets':{'a':{'status':'EXPORTED','png_sha256':'test'}}}
        images,_,join=resolve_dungeon_card(rank,bindings,art)
        self.assertEqual(len(images),1);self.assertEqual(images[0]['resource'],resource)
        self.assertIn('rank_id_replace',join)

    def upcoming_fixture(self):
        run=BOT.parent/'WWM-Toolkit/output/game_data/runs/20261007_074745_140408'
        catalogue=json.loads((run/'dungeon_library/dungeon_catalogue.json').read_text(encoding='utf-8'))
        reviews=json.loads((BOT/'maintenance/assets/dungeon_card_reviews.json').read_text())
        art=json.loads((BOT/'maintenance/research/output/dungeon_cards/art_manifest.json').read_text())
        matches=[{'row_id':'66','fuben_id':'145','mode':'normal','name':'Liminal Ascension'}]
        rank={'team':'hr','rank_name':'rank_team10_dungeon_34','leaderboard_title':'Liminal Ascension Top 500 - Phase 1'}
        return rank,matches,catalogue,reviews,art

    def test_all_upcoming_families_have_one_original_card(self):
        assets=DungeonAssets(BOT/'data/ranking_assets')
        expected={('hr',34):'25',('hr',37):'25',('st',35):'26',('st',38):'26',
                  ('st',41):'27',('st',44):'27',('hr',40):'28',('hr',43):'28',
                  ('st',47):'29',('st',50):'29',('hr',46):'30',('hr',49):'30'}
        for (team,identifier),suffix in expected.items():
            row=assets.get(team,identifier)
            self.assertEqual(len(row['images']),1)
            self.assertEqual(row['resource'],'wulinlu_tongyou_pic_xj_'+suffix)
            self.assertTrue(row['path'].is_file())
            self.assertIn(row['join'],('direct_guild_card','reviewed_boss_art_identity'))

    def test_changed_upcoming_art_requires_new_review(self):
        args=self.upcoming_fixture();rank,matches,cat,reviews,art=args
        review=next(r for r in reviews['reviews'] if r['trial_row_id']==66)
        review['png_sha256']='changed'
        self.assertEqual(resolve_reviewed_card(*args)[0],[])

    def test_upcoming_trial_identity_cannot_drift(self):
        args=self.upcoming_fixture();args[2]['trial_displays:66']['variants'][0]['raw']['fuben_id']=99999
        self.assertEqual(resolve_reviewed_card(*args)[0],[])

    def test_upcoming_challenge_requires_reciprocal_mode_link(self):
        rank,matches,cat,reviews,art=self.upcoming_fixture()
        rank['leaderboard_title']='Liminal Ascension Challenge Top 500 - Phase 1'
        matches=[{'row_id':'67','fuben_id':'149','mode':'challenge','name':'Liminal Ascension'}]
        self.assertEqual(len(resolve_reviewed_card(rank,matches,cat,reviews,art)[0]),1)
        cat['trial_displays:67']['variants'][0]['raw']['normal_dungeon_index']=12345
        self.assertEqual(resolve_reviewed_card(rank,matches,cat,reviews,art)[0],[])

    def test_upcoming_card_does_not_cross_dungeon_names(self):
        args=self.upcoming_fixture();args[0]['leaderboard_title']='Compass & Chime Top 750 - Phase 1'
        self.assertEqual(resolve_reviewed_card(*args)[0],[])

    def test_copied_banner_fields_cannot_supply_boss_art(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root,['wrong.png'])
            cat=json.loads((root/'dungeon_library/dungeon_catalogue.json').read_text())
            del cat['replay_encounters:3100']
            dungeon={'row_id':'0','fuben_id':'100','name':'Test'}
            images,reason=resolve_boss_art(cat,[dungeon],{'assets':{},'references':{}})
            self.assertEqual(images,[])
            self.assertIn('no original introduction',reason)

    def test_entity_profile_conflict_is_not_resolved_using_all_boss_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root,['wrong.png'])
            cat=json.loads((root/'dungeon_library/dungeon_catalogue.json').read_text())
            cat['boss_profiles:2100']['variants'][0]['raw']['boss_no']=999999
            images,reason=resolve_boss_art(cat,[{'row_id':'0','fuben_id':'100','name':'Test'}],{'assets':{},'references':{}})
            self.assertEqual(images,[])
            self.assertIn('fight entity',reason)

    def test_original_4k_variant_selected_without_guessing_unrelated_art(self):
        assets = {'a': {'status':'EXPORTED','n':'banner','size':[388,160],'png_sha256':'a'},
                  'b': {'status':'EXPORTED','n':'banner_4k','size':[772,316],'png_sha256':'b'}}
        art = {'references':{'banner.png':['a','b']},'assets':assets}
        self.assertEqual(choose_asset(art,'banner.png')['png_sha256'],'b')
        assets['b']['n'] = 'another_banner'
        self.assertIsNone(choose_asset(art,'banner.png'))

    def test_direct_art_requires_matching_instance_and_variant_consensus(self):
        entry = {'variants':[{'raw':{'fuben_id':24,'guild_image':'banner.png'}}],
                 'art':[{'owner':'trial_displays:1','field':'guild_image',
                         'relation':'direct table field','resource':'banner.png'}]}
        catalogue = {'trial_displays:1':entry}
        dungeon = {'row_id':'1','fuben_id':'24'}
        self.assertEqual(direct_resources(catalogue,dungeon,'guild_image'),{'banner.png'})
        dungeon['fuben_id'] = '25'
        self.assertEqual(direct_resources(catalogue,dungeon,'guild_image'),set())
        dungeon['fuben_id'] = '24'
        entry['variants'].append({'raw':{'fuben_id':24,'guild_image':'different.png'}})
        self.assertEqual(direct_resources(catalogue,dungeon,'guild_image'),set())

    def test_conflicting_dungeon_art_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/'source';run.mkdir();self.fixture(run,['a.png','b.png'])
            (run/'dungeon_library/art_manifest.json').write_text(json.dumps({'references':{},'assets':{}}))
            result=build_assets(run,root/'bot-assets',cards_root=root/'empty-cards')
            self.assertEqual(result['with_art'],0)

    def test_wrong_art_hash_does_not_publish_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/'source';run.mkdir();self.fixture(run,['a.png'])
            art=run/'dungeon_library';(art/'a.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            (art/'art_manifest.json').write_text(json.dumps({'references':{'a.png':['a']},'assets':{'a':{'status':'EXPORTED','png_sha256':'wrong','file':'a.png'}}}))
            with self.assertRaises(ValueError):copy_art(json.loads((art/'art_manifest.json').read_text()),'a.png',art,root/'bot-assets')
            self.assertFalse((root/'bot-assets/latest.json').exists())

    def test_legacy_callers_keep_generated_token_default(self):
        for name in ('guild_verification_cog.py','market_cog.py'):
            tree=ast.parse((BOT/'cogs'/name).read_text(encoding='utf-8'))
            for call in (node for node in ast.walk(tree) if isinstance(node,ast.Call)):
                self.assertFalse(any(k.arg=='token' and isinstance(k.value,ast.Name) and k.value.id=='WWM_TOKEN' for k in call.keywords))

    def test_time_rounding_carries_to_next_minute(self):
        self.assertEqual(ranking._format_rank_time(-59.9999),'1:00.000')
        self.assertEqual(ranking._format_rank_time(-450.11907958984375),'7:30.119')

    def test_invalid_time_and_points_do_not_crash(self):
        for value in (None,'bad',float('nan'),float('inf')):
            self.assertEqual(ranking._format_rank_time(value),'N/A')
            self.assertEqual(ranking._format_rank_points(value),'N/A')

    def test_affix_prefers_exact_id_reference(self):
        info={'min':10,'max':20,'name_min':1,'name_max':100}
        text=format_affix_range(info,15)
        self.assertIn('10.0–20.0',text);self.assertIn('+5.0',text)

    def test_above_reference_is_not_max(self):
        self.assertNotIn('MAX',format_affix_range({'min':7.5,'max':33.8},51.3))
        self.assertNotIn('to reference max',format_affix_range({'name_min':1,'name_max':100},50))

    def test_affix_mapper_rejects_fractional_boolean_and_nonfinite_ids(self):
        cache={1:{'name':'One'}}
        for value in (True,1.5,float('nan'),float('inf')):
            data={'base_affixes':[[value,42]],'tone_determin':value}
            mapped=_map_recursive(data,cache,'')
            self.assertNotIsInstance(mapped['base_affixes'][0][0],dict)
            self.assertNotIsInstance(mapped['tone_determin'],dict)

    def test_affix_mapper_leaves_unrelated_namespaces_untouched(self):
        data={'tokens':{1:100},'No':1,'base_affixes':[[1,42]]}
        mapped=_map_recursive(data,{1:{'name':'One'}},'')
        self.assertEqual(mapped['tokens'],{1:100});self.assertEqual(mapped['No'],1)
        self.assertEqual(mapped['base_affixes'][0][0]['name'],'One')

    def test_asset_namespaces_are_distinct(self):
        assets=DungeonAssets(BOT/'data/ranking_assets')
        self.assertNotEqual(assets.get('hr',1)['title'],assets.get('st',1)['title'])
        self.assertTrue(assets.get('st',1)['path'].is_file())
        self.assertIsNone(assets.get('pet',1));self.assertIsNone(assets.get('st',99999))

    def test_every_imported_banner_exists(self):
        root=BOT/'data/ranking_assets';run=root/json.loads((root/'latest.json').read_text())['run']
        data=json.loads((run/'manifest.json').read_text());assets=DungeonAssets(root)
        self.assertTrue(data['boards'])
        for key in data['boards']:
            team,identifier=key.split(':');row=assets.get(team,identifier)
            if data['boards'][key]['file']:self.assertTrue((run/data['boards'][key]['file']).is_file())
            if row['art_status'] in ('verified_dungeon_card','reviewed_dungeon_card'):self.assertTrue(row['path'].is_file())
            else:self.assertIsNone(row['path'])

    def test_asset_pointer_cannot_escape_bot_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'latest.json').write_text(json.dumps({'run':'../escape'}))
            self.assertIsNone(DungeonAssets(root).get('hr',1))


if __name__=='__main__':unittest.main()
