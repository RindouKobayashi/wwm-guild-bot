import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'maintenance/assets'))
from utility.market_goods import validate,key
from utility.game_assets import digest


def fixture(root):
    from PIL import Image
    (root/'art').mkdir(parents=True);Image.new('RGBA',(128,128),(100,200,30,255)).save(root/'art/2122001.png')
    data=dict(format=1,goods={'2122001':dict(item_id=2122001,name='Chrysanthemum',image='art/2122001.png',icon_resource='2122001_0_0_ui',sha256=digest(root/'art/2122001.png'))})
    (root/'catalogue.json').write_text(json.dumps(data),encoding='utf-8');(root/'contact-sheet.html').write_text('Fixture',encoding='utf-8')
    return data


class MarketAssetTests(unittest.TestCase):
    def test_validation_unknown_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            data=fixture(Path(d));validate(data)
            for field,value in (('image','../../secret.png'),('item_id',1),('sha256','bad'),('name','')):
                bad=copy.deepcopy(data);bad['goods']['2122001'][field]=value
                with self.assertRaises(ValueError):validate(bad)
        for value in (None,{},True,-1,'../2122001','0'):self.assertIsNone(key(value))
        self.assertEqual(key('2122001'),'2122001')

    def test_bundle_images_are_verified(self):
        from test_game_asset_pipeline import fixture as bundle_fixture
        from extract_market_goods import copy_export
        from build_game_bundle import finish
        from utility.game_assets import validate_release
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';data=fixture(source);stage=root/'release';stage.mkdir();tables=bundle_fixture(stage)
            copy_export(source,stage/'market');tables['market_goods']=data;finish(stage,tables,{'mode':'fixture'},{});validate_release(stage)
            (stage/'market/art/2122001.png').write_bytes(b'changed')
            with self.assertRaises(ValueError):validate_release(stage)

    def test_runtime_missing_image_is_optional(self):
        from utility.market_goods import name,image
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=fixture(root/'market');assets=SimpleNamespace(directory=root,section=lambda *args:data)
            with patch('utility.game_assets.current',return_value=assets):
                self.assertEqual(name('2122001'),'Chrysanthemum');self.assertIsNone(name('999'))
                self.assertTrue(image('2122001').is_file());(root/'market/art/2122001.png').unlink();self.assertIsNone(image('2122001'))


class MarketViewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cogs import market_cog
        self.module=market_cog;self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.data=fixture(self.root)
        self.names=patch.object(market_cog,'market_name',side_effect=lambda k:'Chrysanthemum' if str(k)=='2122001' else None);self.names.start()
        self.images=patch.object(market_cog,'market_image',side_effect=lambda k:self.root/'art/2122001.png' if str(k)=='2122001' else None);self.images.start()
    async def asyncTearDown(self):
        self.images.stop();self.names.stop();self.temp.cleanup()

    async def test_game_name_precedence_without_database_rewrite(self):
        dbpath=self.root/'names.db'
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('CREATE TABLE good_names(good_id TEXT PRIMARY KEY,name TEXT)');db.executemany('INSERT INTO good_names VALUES(?,?)',[('2122001','Approved Alias'),('999','Approved Fallback')]);db.commit()
        cog=SimpleNamespace(db_path=dbpath)
        self.assertEqual(await self.module.MarketCog._get_good_name(cog,'2122001'),'Chrysanthemum')
        self.assertEqual(await self.module.MarketCog._get_good_name(cog,'999'),'Approved Fallback')
        self.assertIsNone(await self.module.MarketCog._get_good_name(cog,'998'))
        with patch.object(self.module,'market_catalogue',return_value={'2122001':{},'2122002':{}}):
            self.assertEqual(await self.module.MarketCog._get_known_goods(cog),{'2122001','2122002','999'})
        with closing(sqlite3.connect(dbpath)) as db:self.assertEqual(db.execute('SELECT name FROM good_names WHERE good_id=?',('2122001',)).fetchone()[0],'Approved Alias')

    async def test_item_details_have_image_or_clear_fallback(self):
        view,files=self.module.market_item_details('2122001','Approved Alias')
        self.assertIn('Chrysanthemum',str(view.to_components()));self.assertIn('attachment://market-good.png',str(view.to_components()));self.assertEqual(len(files),1)
        for f in files:f.close()
        view,files=self.module.market_item_details('999','Approved Fallback')
        self.assertIn('Approved Fallback',str(view.to_components()));self.assertEqual(files,[]);self.assertNotIn('attachment://',str(view.to_components()))

    async def test_good_switch_removes_previous_attachment(self):
        view=self.module.MarketGoodPaginatedView(Mock(),{'2122001':[],'9999999':[]},{},{},set(),1)
        self.assertIn('attachment://market-good.png',str(view.to_components()))
        interaction=SimpleNamespace(data={'values':['9999999']},response=SimpleNamespace(edit_message=AsyncMock()))
        await view._on_select_good(interaction)
        self.assertEqual(interaction.response.edit_message.call_args.kwargs['attachments'],[])
        self.assertNotIn('attachment://',str(view.to_components()))

    async def test_goods_list_picker_opens_private_details(self):
        view=self.module.GoodsPaginatedView([dict(good_id='2122001',name='Chrysanthemum')])
        interaction=SimpleNamespace(data={'values':['2122001']},response=SimpleNamespace(send_message=AsyncMock()))
        await view._on_item_details(interaction);sent=interaction.response.send_message.call_args.kwargs
        self.assertTrue(sent['ephemeral']);self.assertEqual(len(sent['files']),1)
        for f in sent['files']:f.close()

    async def test_unreadable_image_does_not_leave_dangling_media(self):
        with patch.object(self.module.discord,'File',side_effect=PermissionError('fixture')):
            view,files=self.module.market_item_details('2122001')
            self.assertEqual(files,[]);self.assertNotIn('attachment://',str(view.to_components()))
            ranks=self.module.MarketGoodPaginatedView(Mock(),{'2122001':[]},{},{},set(),1)
            self.assertEqual(ranks.attachments(),[])
            self.assertNotIn('attachment://',str(ranks.to_components()))

    async def test_main_report_uses_item_thumbnail(self):
        view=self.module.MarketReportView(Mock(),{'2122001':[]},0,1,2,
            known_goods={'2122001'},good_names_map={'2122001':'Chrysanthemum'})
        self.assertIn('Chrysanthemum',str(view.to_components()))
        self.assertIn('attachment://market-',str(view.to_components()))
        files=view.attachments();self.assertEqual(len(files),1)
        self.assertEqual(files[0].fp.read(),(self.root/'art/2122001.png').read_bytes())
        for f in files:f.close()

    async def test_new_week_cards_reuse_unchanged_attachments(self):
        view=self.module.NewWeekMarketView(Mock(),['2122001'],{'2122001':'Chrysanthemum'},{'2122001'},1)
        manifest=view.attachment_manifest();self.assertEqual(len(manifest),1)
        self.assertIn('attachment://market-',str(view.to_components()))
        existing=SimpleNamespace(id=5,filename=manifest[0]['filename'],size=manifest[0]['size_bytes'])
        from control_panel.action_bridge import resolve_attachments,visible_payload
        payload={'view':view,'attachments':view.attachments()};old=payload['attachments'][0]
        resolve_attachments(payload,[existing])
        self.assertTrue(old.fp.closed);self.assertIs(payload['attachments'][0],existing)
        public=visible_payload(payload);self.assertEqual(public['attachments'][0]['sha256'],manifest[0]['sha256'])
        self.assertNotIn(str(self.root),json.dumps(public))

    async def test_changed_or_removed_goods_replace_attachment_set(self):
        view=self.module.NewWeekMarketView(Mock(),['2122001'],{},set(),1)
        old=SimpleNamespace(id=5,filename='old-good.png',size=10)
        files=view.edit_attachments([old]);self.assertEqual(len(files),1);self.assertNotEqual(files[0].filename,old.filename)
        for f in files:f.close()
        empty=self.module.NewWeekMarketView(Mock(),['999'],{},set(),1)
        self.assertEqual(empty.edit_attachments([old]),[])
        self.assertNotIn('attachment://',str(empty.to_components()))

    async def test_thumbnail_budget_preserves_all_goods_text(self):
        ids=[str(2122001+i) for i in range(6)]
        with patch.object(self.module,'market_image',return_value=self.root/'art/2122001.png'):
            view=self.module.MarketReportView(Mock(),{key:[] for key in ids},0,1,2,known_goods=set(ids))
        self.assertLessEqual(view.total_children_count,40)
        self.assertGreater(len(view.attachment_manifest()),0)
        self.assertLess(len(view.attachment_manifest()),len(ids))
        for key in ids:self.assertIn(key,str(view.to_components()))

    async def test_online_and_guild_filters_include_images(self):
        row=('p','Player','123',100.,120.,20.,True,10103,'Guild',17,1)
        view=self.module.MarketReportView(Mock(),{'2122001':[row]},1,1,2,known_goods={'2122001'})
        interaction=SimpleNamespace(user=SimpleNamespace(id=7),response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await view._on_filter_online(interaction)
        sent=interaction.followup.send.call_args.kwargs;self.assertTrue(sent['ephemeral']);self.assertEqual(len(sent['files']),1)
        self.assertIn('attachment://market-',str(sent['view'].to_components()))
        for f in sent['files']:f.close()

        dbpath=self.root/'verified.db'
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('CREATE TABLE verified_members(user_id INTEGER, player_pid TEXT)');db.execute('INSERT INTO verified_members VALUES(7,"p")');db.commit()
        with patch.object(self.module,'VERIFICATION_DB_PATH',dbpath):await view._on_filter_guild(interaction)
        sent=interaction.followup.send.call_args.kwargs;self.assertEqual(len(sent['files']),1)
        self.assertIn('Player',str(sent['view'].to_components()))
        for f in sent['files']:f.close()

    async def test_scheduled_edit_reuses_images_and_keeps_returned_message(self):
        view=self.module.NewWeekMarketView(Mock(),['2122001'],{},set(),1)
        manifest=view.attachment_manifest()[0]
        attachment=SimpleNamespace(id=1,filename=manifest['filename'],size=manifest['size_bytes'])
        updated=SimpleNamespace(attachments=[attachment])
        old=SimpleNamespace(attachments=[attachment],edit=AsyncMock(return_value=updated))
        payload=dict(content=None,embeds=[],view=view,attachments=view.attachments())
        cog=SimpleNamespace(panel_prepare=AsyncMock(return_value=payload),last_report_message=old,
                            market_channel=SimpleNamespace(send=AsyncMock()),_save_config=AsyncMock())
        await self.module.MarketCog._build_and_send_report(cog)
        self.assertIs(cog.last_report_message,updated)
        self.assertIs(old.edit.call_args.kwargs['attachments'][0],attachment)
        cog.market_channel.send.assert_not_called()
        cog._save_config.assert_awaited_once()



if __name__=='__main__':unittest.main()
