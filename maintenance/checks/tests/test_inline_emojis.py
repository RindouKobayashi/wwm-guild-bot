"""Synthetic inline markup and mocked application uploads; never contacts Discord."""
import asyncio
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/'maintenance/assets')]
from utility.game_assets import digest
from utility.inline_emojis import InlineEmojis,validate_pack
from inline_emoji_pack import build


def fixture(root):
    from PIL import Image
    directory=root/'release/emotes';(directory/'art').mkdir(parents=True)
    first=Image.new('RGBA',(32,32),(255,0,0,255));second=Image.new('RGBA',(32,32),(0,255,0,255))
    first.save(directory/'art/inline_201.gif',save_all=True,append_images=[second],duration=[200,200],loop=0)
    first.save(directory/'art/inline_201.png');second.save(directory/'art/inline_261.png')
    data=dict(format=1,entries={'999':dict(id=999,source_table='chat_emotion_icon_lib',status='unresolved')},inline_entries={})
    for identifier,name,animated in [('201','smile',True),('261','heart',False)]:
        image=f'art/inline_{identifier}.'+('gif' if animated else 'png');preview=f'art/inline_{identifier}.png'
        data['inline_entries'][identifier]=dict(id=int(identifier),name=name,source_table='chat_emotion_frame_data',
            status='animated' if animated else 'static',image=image,image_sha256=digest(directory/image),
            preview=preview,preview_sha256=digest(directory/preview))
    (directory/'catalogue.json').write_text(json.dumps(data),encoding='utf-8')
    build(directory,data,dict(test='10',production='20'))
    assets=SimpleNamespace(directory=directory.parent,error=None,section=lambda *a:data)
    return directory,data,assets


def receipts(rows,application='10'):
    return dict(format=1,environments={'test':application},applications={application:{r['key']:dict(id=str(800+i),name=r['name'],sha256=r['sha256']) for i,r in enumerate(rows)}})


class InlineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.directory,self.data,self.assets=fixture(self.root)
        self.rows=validate_pack(self.directory)
        self.registry=receipts(self.rows)
        self.renderer=InlineEmojis(self.assets,self.root,self.registry)
    def tearDown(self):self.temp.cleanup()

    def test_pack_preserves_animation_and_has_size_limited_versioned_names(self):
        from PIL import Image
        self.assertEqual(len(self.rows),2)
        for row in self.rows:
            path=self.directory/'upload-pack'/row['file']
            self.assertLessEqual(path.stat().st_size,256*1024)
            self.assertIn(row['sha256'][:8],row['name'])
            with Image.open(path) as image:
                self.assertEqual(image.size,(128,128))
                self.assertEqual(image.n_frames>1,row['animated'])
                if row['animated']:
                    self.assertEqual(image.info['duration'],200);self.assertEqual(image.info['loop'],0)

    def test_position_mapping_not_id_guessing(self):
        result=self.renderer.render('hello [#4] [#201]',{'4':201},'10')
        self.assertIn('<a:',result);self.assertIn(':800>',result);self.assertIn('[#201]',result)
        self.assertEqual(self.renderer.render('[#4]',None,'10'),'[#4]')

    def test_received_ids_are_direct_and_position_decoder_remains_separate(self):
        expected=self.renderer.resolve('201','10')
        self.assertEqual(self.renderer.render_received('Hello [#201]','10'),'Hello '+expected)
        self.assertEqual(self.renderer.render_received('[#4]','10'),'[#4]')
        self.assertEqual(self.renderer.render('[#4]',{4:201},'10'),expected)

    def test_received_unknown_sticker_ids_and_malformed_markers_stay_literal(self):
        text='[#999][#0][#-1][#bad][#000201][#1.0]'
        self.assertEqual(self.renderer.render_received(text,'10'),text)

    def test_received_missing_receipts_and_other_application_use_names(self):
        self.assertEqual(self.renderer.render_received('[#201][#261]','20'),'[smile][heart]')
        self.assertEqual(self.renderer.render_received('[#201]',None),'[smile]')
        self.registry['applications']['10']['inline_201']['sha256']='changed'
        self.assertEqual(self.renderer.render_received('[#201]','10'),'[smile]')

    def test_received_repeats_static_animation_and_growth_bounds(self):
        self.assertEqual(self.renderer.render_received('[#201][#261][#201]','10'),
            self.renderer.resolve('201','10')+self.renderer.resolve('261','10')+self.renderer.resolve('201','10'))
        result=self.renderer.render_received('[#201]'*100,'10')
        self.assertEqual(result.count('<a:'),64);self.assertEqual(result.count('[#201]'),36)
        text='x'*3790+'[#201]'*3
        self.assertEqual(self.renderer.render_received(text,'10'),text)

    def test_translation_detection_removes_only_recognized_markers(self):
        for text in ('[#201][#261]','  !! [#201] 123','[#999]',''):
            self.assertFalse(self.renderer.has_translatable_text(text))
        for text in ('a[#201]','你好[#201]','hello [#999]'):
            self.assertTrue(self.renderer.has_translatable_text(text))

    def test_static_and_repeated_adjacent_markers(self):
        result=self.renderer.render('[#1][#2][#1]',{1:201,2:261},'10')
        self.assertEqual(result.count('<a:'),2);self.assertIn('<:wwm_i_261_',result)

    def test_actual_application_required_and_isolated(self):
        self.assertEqual(self.renderer.render('[#1]',{1:201},'20'),'[smile]')
        self.assertEqual(self.renderer.render('[#1]',{1:201},None),'[smile]')
        self.registry['applications']['10']['inline_201']['sha256']='changed'
        self.assertEqual(self.renderer.render('[#1]',{1:201},'10'),'[smile]')

    def test_unknown_id_readable_and_unbound_literal_unchanged(self):
        self.assertEqual(self.renderer.render('x [#1] y',{1:777},'10'),'x [Emote 777] y')
        self.assertEqual(self.renderer.render(':smile: [#201]',{},'10'),':smile: [#201]')

    def test_malformed_and_conflicting_metadata_stays_literal(self):
        for value in (None,[],True,{'1':None},{'1':{}},{'1':True},{'1':201,1:261}):
            self.assertEqual(self.renderer.render('[#1]',value,'10'),'[#1]')

    def test_receipt_markup_injection_rejected(self):
        row=self.registry['applications']['10']['inline_201'];row['name']='x><@everyone'
        self.assertEqual(self.renderer.render('[#1]',{1:201},'10'),'[smile]')

    def test_corrupt_receipt_falls_back_and_long_messages_do_not_expand(self):
        self.registry['applications']['10']['inline_201']='corrupt'
        self.assertEqual(self.renderer.render('[#1]',{1:201},'10'),'[smile]')
        text='x'*3790+'[#1]'*4
        self.assertEqual(self.renderer.render(text,{1:201},'10'),text)

    def test_large_marker_stream_has_bounded_expansion_without_losing_text(self):
        result=self.renderer.render('[#1]'*100,{1:201},'10')
        self.assertEqual(result.count('<a:'),64);self.assertEqual(result.count('[#1]'),36)

    def test_changed_pack_or_source_hash_is_rejected(self):
        manifest=self.directory/'upload-pack/upload-manifest.json';data=json.loads(manifest.read_text())
        data['icons'][0]['source_sha256']='changed';manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):validate_pack(self.directory)

    def test_resealed_wrong_source_binding_is_still_rejected(self):
        manifest=self.directory/'upload-pack/upload-manifest.json';data=json.loads(manifest.read_text())
        data['icons'][0]['source_id']=999;manifest.write_text(json.dumps(data))
        path=self.directory/'startup-approval.json';approval=json.loads(path.read_text());approval['upload_manifest_sha256']=digest(manifest);path.write_text(json.dumps(approval))
        with self.assertRaisesRegex(ValueError,'verified inline asset'):validate_pack(self.directory)

    def test_review_shows_upload_inventory_and_startup_approval(self):
        from asset_review import review
        report=review({'emotes':self.data},{'emotes':self.data},self.root/'empty',self.directory.parent)
        self.assertEqual(report['summary']['inline_emoji_uploads']['added'],2)
        self.assertEqual(report['summary']['inline_emoji_approval']['added'],1)

    def test_animated_verification_checks_beyond_first_frame(self):
        from PIL import Image
        from utility.emoji_upload import same_image
        raw=(self.directory/'upload-pack'/self.rows[0]['file']).read_bytes()
        first=Image.new('RGB',(128,128),(255,0,0));other=Image.new('RGB',(128,128),(0,0,255))
        out=io.BytesIO();first.save(out,format='GIF',save_all=True,append_images=[other],duration=200,loop=0)
        self.assertTrue(same_image(raw,raw,animated=True))
        self.assertFalse(same_image(raw,out.getvalue(),animated=True))


class FakeAPI:
    def __init__(self):self.application='10';self.items=[];self.images={};self.created=0;self.uncertain=False
    async def call(self,method,path,body=None):
        if path=='/oauth2/applications/@me':return dict(id=self.application,name='Synthetic application')
        if method=='GET':return dict(items=copy.deepcopy(self.items))
        if method!='POST':raise AssertionError('Unexpected destructive method')
        import base64
        self.created+=1;identity=str(900+self.created)
        item=dict(id=identity,name=body['name'],animated=body['image'].startswith('data:image/gif;'))
        self.items.append(item);self.images[identity]=base64.b64decode(body['image'].split(',',1)[1])
        if self.uncertain:self.uncertain=False;raise TimeoutError('Uncertain synthetic response')
        return item
    async def image(self,identity,animated=False):return self.images[str(identity)]


class InlineUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.directory,self.data,self.assets=fixture(self.root);self.api=FakeAPI()
    async def asyncTearDown(self):self.temp.cleanup()

    async def test_sync_uploads_correct_formats_once_and_only_local_receipts(self):
        from utility.emoji_startup import sync
        result=await sync(self.directory,'test','secret-fixture',self.root,self.api,kind='inline')
        self.assertEqual(result['created'],2);self.assertTrue(self.api.items[0]['animated'])
        result=await sync(self.directory,'test','secret-fixture',self.root,self.api,kind='inline')
        self.assertEqual(result['created'],0);self.assertEqual(self.api.created,2)
        path=self.root/'data/inline-emojis.json'
        self.assertNotIn('secret-fixture',path.read_text());self.assertFalse((self.root/'data/martial-emojis.json').exists())
        renderer=InlineEmojis(self.assets,self.root)
        self.assertIn('<a:',renderer.render('[#1]',{1:201},'10'))

    async def test_wrong_application_never_uploads(self):
        from utility.emoji_startup import sync
        with self.assertRaises(ValueError):await sync(self.directory,'production','secret',self.root,self.api,kind='inline')
        self.assertEqual(self.api.created,0)

    async def test_uncertain_upload_reconciles_without_duplicate_creation(self):
        from utility.emoji_startup import sync
        self.api.uncertain=True
        with self.assertRaises(TimeoutError):await sync(self.directory,'test','secret',self.root,self.api,kind='inline')
        self.assertTrue(json.loads((self.root/'data/inline-emoji-upload-pending.json').read_text()))
        result=await sync(self.directory,'test','secret',self.root,self.api,kind='inline')
        self.assertEqual(result['created'],1);self.assertEqual(self.api.created,2)
        self.assertEqual(json.loads((self.root/'data/inline-emoji-upload-pending.json').read_text()),{})

    async def test_import_cannot_create_even_when_missing(self):
        from utility.emoji_startup import sync
        with self.assertRaises(ValueError):await sync(self.directory,'test','secret',self.root,self.api,kind='inline',allow_create=False)
        self.assertEqual(self.api.created,0)

    async def test_application_capacity_is_checked_before_upload(self):
        from utility.emoji_startup import sync
        self.api.items=[dict(id=str(i+1),name='existing_'+str(i),animated=False) for i in range(2000)]
        with self.assertRaisesRegex(ValueError,'capacity'):await sync(self.directory,'test','secret',self.root,self.api,kind='inline')
        self.assertEqual(self.api.created,0)

    async def test_uncertain_animation_cannot_reconcile_from_only_first_frame(self):
        from utility.emoji_startup import sync
        self.api.uncertain=True
        with self.assertRaises(TimeoutError):await sync(self.directory,'test','secret',self.root,self.api,kind='inline')
        self.api.images['901']=(self.directory/'upload-pack/icons/inline_261.png').read_bytes()
        with self.assertRaises(ValueError):await sync(self.directory,'test','secret',self.root,self.api,kind='inline')
        self.assertEqual(self.api.created,1)
        self.assertTrue(json.loads((self.root/'data/inline-emoji-upload-pending.json').read_text()))

    async def test_startup_timeout_is_nonfatal_and_separate(self):
        from utility.emoji_startup import startup
        async def hang(*args,**kwargs):await asyncio.sleep(10)
        with patch('utility.emoji_startup.sync',side_effect=hang):
            result=await startup('test','secret-fixture',self.root,timeout=.01,kind='inline',assets=self.assets)
        self.assertEqual(result['state'],'incomplete')
        path=self.root/'data/inline-emoji-sync-status.json'
        self.assertTrue(path.exists());self.assertNotIn('secret-fixture',path.read_text())

    async def test_formatter_replaces_after_translation_without_sending_emoji_ids(self):
        from cogs.live_chat_cog import LiveChatCog
        rows=validate_pack(self.directory);renderer=InlineEmojis(self.assets,self.root,receipts(rows))
        translator=AsyncMock(side_effect=lambda text,**kwargs:text)
        cog=SimpleNamespace(inline_emojis=renderer,bot=SimpleNamespace(application_id=10),ranks={},
            _get_discord_mention=lambda pid:'',translate_with_retry=translator)
        message=dict(msg='Hello [#201]',ts=1700000000,nickname='Synthetic sender',ext=dict(msg_type='msg_normal'))
        embed=await LiveChatCog.format_message_embed(cog,message)
        self.assertEqual(embed.description.count('<a:'),2)
        self.assertEqual(translator.call_args.args[0],'Hello [#201]')
        self.assertNotIn('800',translator.call_args.args[0])

    def received_renderer(self):
        rows=validate_pack(self.directory);registry=receipts(rows)
        renderer=InlineEmojis(self.assets,self.root,registry)
        # Synthetic receipts for the public game IDs in the reported sequences.
        for identifier,name in [('220','spit'),('234','dizzy'),('249','floating'),('263','lips'),('247','furious'),('260','rose')]:
            renderer.entries[identifier]={'id':int(identifier),'name':name}
            row=dict(rows[0],key='inline_'+identifier,source_id=int(identifier),name='wwm_i_'+identifier+'_fixture')
            renderer.rows[identifier]=row
            registry['applications']['10'][row['key']]=dict(id='900'+identifier,name=row['name'],sha256=row['sha256'])
        return renderer

    async def test_reported_mixed_sequence_preserves_text_repeated_ids_and_translation(self):
        from cogs.live_chat_cog import LiveChatCog
        renderer=self.received_renderer();text='a[#220][#234][#249][#249][#263][#261][#247]'
        translator=AsyncMock(return_value=text)
        cog=SimpleNamespace(inline_emojis=renderer,bot=SimpleNamespace(application_id=10),ranks={},
            _get_discord_mention=lambda pid:'',translate_with_retry=translator)
        embed=await LiveChatCog.format_message_embed(cog,dict(msg=text,ts=1700000000,nickname='Synthetic sender',ext={'msg_type':'msg_normal'}))
        expected='a'+''.join(renderer.resolve(i,'10') for i in ['220','234','249','249','263','261','247'])
        self.assertTrue(embed.description.startswith(expected+'\n\n[Translated] '+expected))
        self.assertEqual(embed.description.count(renderer.resolve('249','10')),4)
        self.assertEqual(translator.call_args.args[0],text)

    async def test_reported_rose_and_emoji_only_sequence_ignore_ext_mapping(self):
        from cogs.live_chat_cog import LiveChatCog
        renderer=self.received_renderer();translator=AsyncMock()
        cog=SimpleNamespace(inline_emojis=renderer,bot=SimpleNamespace(application_id=10),ranks={},
            _get_discord_mention=lambda pid:'',translate_with_retry=translator)
        for text in ('[#260]','[#220][#234][#249][#249][#263][#261][#247]'):
            for emotion in (None,{},[],{'260':201,'249':261}):
                embed=await LiveChatCog.format_message_embed(cog,dict(msg=text,ts=1700000000,nickname='Synthetic sender',ext={'msg_type':'msg_normal','emotion':emotion}))
                self.assertTrue(embed.description.startswith(renderer.render_received(text,'10')))
                self.assertNotIn('[Translated]',embed.description);self.assertNotIn('[#',embed.description)
        translator.assert_not_called()

    async def test_unicode_text_still_translates_before_replacement(self):
        from cogs.live_chat_cog import LiveChatCog
        renderer=self.received_renderer();translator=AsyncMock(return_value='Hello [#220]')
        cog=SimpleNamespace(inline_emojis=renderer,bot=SimpleNamespace(application_id=10),ranks={},
            _get_discord_mention=lambda pid:'',translate_with_retry=translator)
        embed=await LiveChatCog.format_message_embed(cog,dict(msg='你好 [#220]',ts=1700000000,nickname='Synthetic sender',ext={'msg_type':'msg_normal'}))
        self.assertEqual(translator.call_args.args[0],'你好 [#220]');self.assertEqual(translator.call_args.kwargs['src'],'zh-cn')
        self.assertEqual(embed.description.count(renderer.resolve('220','10')),2)


if __name__=='__main__':unittest.main()
