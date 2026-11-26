import asyncio
import copy
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'maintenance/assets'))
from utility.martial import equipped,identifier,lookup
from utility.martial_data import validate
from upload_martial_emojis import icons,preview,same_image
from extract_martial import pack


class MartialTests(unittest.TestCase):
    def test_supplemental_page_is_bound_to_named_primary_page(self):
        from maintenance.assets.named_mtable import binding

        header = struct.pack('<6I', 0x10001, 8, 1, 1, 0, 0)
        name = struct.pack('<H', 6) + b'kongfu'
        record = struct.pack('<8I', 1, 1, 2 | (30 << 16), 1001 | (20 << 16), 0, 0, 0, 1024)
        pages = struct.pack('<4H', 2, 31, 1001, 21)
        result = binding(header + name + record + pages, 'kongfu', page_links=True)
        self.assertEqual(result['supplemental_schema'], {'archive': 'index1001', 'frame': 20})
        self.assertEqual(result['supplemental_pages'], [{'archive': 'index1001', 'frame': 21}])
        with self.assertRaisesRegex(ValueError, 'does not match its primary schema'):
            binding(header + name + record + struct.pack('<4H', 2, 32, 1001, 21), 'kongfu', page_links=True)

    def test_bundle_roundtrip_and_integrity(self):
        from test_game_asset_pipeline import fixture
        from extract_martial import copy_export
        from build_game_bundle import finish
        from utility.game_assets import validate_release
        with tempfile.TemporaryDirectory() as d:
            stage=Path(d);tables=fixture(stage)
            copy_export(ROOT/'assets/martial-review',stage/'martial')
            tables['martial']=json.loads((stage/'martial/catalogue.json').read_text(encoding='utf-8'))
            finish(stage,tables,{'mode':'synthetic'},{});validate_release(stage)
            self.assertFalse((stage/'martial/upload-pack/contact-sheet.png').exists())
            (stage/'martial/art/path_1.png').write_bytes(b'changed')
            with self.assertRaises(ValueError):validate_release(stage)

    def test_decoder_duplicate_and_unsupported(self):
        from extract_martial import decode,read_tables
        class Reader:
            def __init__(self,raw):self.raw=raw;self.pos=0
            def unpack(self,fmt):
                value=struct.unpack_from(fmt,self.raw,self.pos)[0];self.pos+=struct.calcsize(fmt);return value
        self.assertEqual(decode(Reader(struct.pack('<HBB',1,0,48)),['is_chiji']),{'is_chiji':False})
        with self.assertRaises(ValueError):decode(Reader(struct.pack('<HBBBB',2,0,48,0,48)),['a','b'])
        with self.assertRaises(ValueError):decode(Reader(struct.pack('<HBB',1,0,252)),['a'])
        with self.assertRaises(ValueError):read_tables(Path('.'),{'tables':[]},{'kongfu_id'},'kongfu_id',None,None,{})

    def test_player_profile_kungfu_block(self):
        import textwrap
        from utility.api_constants import get_kongfu_ids_from_player,classify_kongfu_role
        source=(ROOT/'cogs/wwm_cog.py').read_text(encoding='utf-8')
        start=source.index('        kongfu_main = None');end=source.index('\n',source.index('logger.warning(f"Failed to parse kongfu data:',start))
        scope=dict(data={'kongfu':{'kongfu_main':'10101','kongfu_sub':999999}},get_kongfu_ids_from_player=get_kongfu_ids_from_player,classify_kongfu_role=classify_kongfu_role)
        from unittest.mock import Mock
        scope['logger']=Mock();exec(textwrap.dedent(source[start:end]),scope)
        self.assertIn('Strategic Sword',scope['kongfu_main']);self.assertIn('Unknown kungfu (999999)',scope['kongfu_sub'])
        self.assertEqual(scope['kongfu_role'],'Undetermined');scope['logger'].warning.assert_not_called()

    def test_real_export_portable_and_known_join(self):
        data=json.loads((ROOT/'assets/martial-review/catalogue.json').read_text(encoding='utf-8'));validate(data)
        self.assertEqual(data['kungfu']['10101']['path_id'],6)
        self.assertEqual(data['kungfu']['20902']['path_id'],14)
        from utility.game_assets import digest
        for row in data['paths'].values():self.assertEqual(digest(ROOT/'assets/martial-review'/row['file']),row['original_sha256'])

    def test_optional_and_unknown(self):
        self.assertEqual(equipped({'kongfu':{'kongfu_main':'10101','kongfu_sub':999999}}),[10101,999999])
        for value in (None,[],1,'bad'):
            self.assertEqual(equipped({'kongfu':value}),[])
        for value in (True,False,-1,1.5,{},'bad','0'):
            self.assertIsNone(identifier(value))
        self.assertEqual(lookup('999999',{})['name'],'Unknown kungfu (999999)')
        self.assertEqual(lookup('999999',{})['emoji'],'')

    def test_roles_and_display(self):
        from utility.api_constants import classify_kongfu_role,format_kongfu_display,get_kongfu
        self.assertEqual(classify_kongfu_role(['10301','20602']),'Healer')
        self.assertEqual(classify_kongfu_role([999999]),'Undetermined')
        self.assertEqual(classify_kongfu_role([10101,999999]),'Undetermined')
        self.assertIn('Unknown kungfu (999999)',format_kongfu_display([999999]))
        self.assertEqual(get_kongfu('10101')['name'],get_kongfu(10101)['name'])

    def test_environment_registry(self):
        rows={1:dict(name='Test',icon_key='path_1',icon_sha256='abc',emoji='')}
        registry={'applications':{'10':{'path_1':dict(id='100',name='wwm_path_1',sha256='abc')}}}
        with patch('utility.martial._registry',registry):
            self.assertIn(':100>',lookup(1,rows,'10')['emoji'])
            self.assertEqual(lookup(1,rows,'20')['emoji'],'')
            rows[1]['icon_sha256']='changed';self.assertEqual(lookup(1,rows,'10')['emoji'],'')

    def fixture(self,root):
        from PIL import Image
        (root/'art').mkdir();Image.new('RGBA',(80,120),(255,0,0,128)).save(root/'art/path_1.png')
        data=dict(format=1,kungfu={'10101':dict(name='Sword',path_id=1,icon_key='path_1',icon_sha256='x')},paths={'1':dict(name='Path',icon_key='path_1',file='art/path_1.png',icon_sha256='x')})
        pack(data,root);return data

    def test_pack_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=self.fixture(root);validate(data)
            rows=icons(root/'upload-pack');self.assertEqual(len(rows),1)
            self.assertTrue((root/'upload-pack/contact-sheet.html').exists())
            altered=copy.deepcopy(data);altered['kungfu']['10101']['path_id']=9
            with self.assertRaises(ValueError):validate(altered)
            path=root/'upload-pack'/rows[0]['file'];path.write_bytes(b'changed')
            with self.assertRaises(ValueError):icons(root/'upload-pack')

    def test_upload_reconciliation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);packdir=root/'upload-pack';rows=icons(packdir);image=(packdir/rows[0]['file']).read_bytes()
            class API:
                def __init__(self,items):self.items=items;self.calls=[]
                async def call(self,method,path):self.calls.append(method);return {'items':self.items}
                async def image(self,id):return image
            api=API([]);plan=asyncio.run(preview(api,'10',rows,packdir,{},{}));self.assertEqual(plan[0]['state'],'create');self.assertEqual(api.calls,['GET'])
            with self.assertRaises(ValueError):asyncio.run(preview(api,'10',rows,packdir,{}, {'10:path_1':{}}))
            api=API([dict(id='123',name=rows[0]['name'])]);plan=asyncio.run(preview(api,'10',rows,packdir,{}, {'10:path_1':{}}));self.assertEqual(plan[0]['state'],'verified_existing')
            api.items.append(dict(id='456',name=rows[0]['name']))
            with self.assertRaises(ValueError):asyncio.run(preview(api,'10',rows,packdir,{},{}))

    def test_cancel_and_partial_upload_receipts(self):
        import upload_martial_emojis as uploader
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);packdir=root/'upload-pack'
            manifest=json.loads((packdir/'upload-manifest.json').read_text(encoding='utf-8'))
            second=dict(manifest['icons'][0],key='path_2',name='wwm_path_2_fixture');manifest['icons'].append(second)
            (packdir/'upload-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            class Session:
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
            class FakeAPI:
                posts=0
                def __init__(self,*args):pass
                async def call(self,method,path,body=None):
                    if path=='/oauth2/applications/@me':return dict(id='10',name='Fixture')
                    if method=='GET':return {'items':[]}
                    FakeAPI.posts+=1
                    if FakeAPI.posts==2:raise TimeoutError('Fixture uncertain upload')
                    return {'id':'123'}
            args=SimpleNamespace(pack=packdir,environment='test',only=None,upload=True,import_existing=False)
            with patch.object(uploader,'BOT',root),patch.object(uploader,'REGISTRY',root/'registry.json'),patch.object(uploader,'API',FakeAPI),patch('aiohttp.ClientSession',return_value=Session()),patch('dotenv.dotenv_values',return_value={'DISCORD_API_TOKEN_DEV':'fixture-token'}):
                with patch('builtins.input',return_value='cancel'):
                    asyncio.run(uploader.run(args));self.assertEqual(FakeAPI.posts,0);self.assertFalse((root/'registry.json').exists())
                with patch('builtins.input',return_value='UPLOAD TEST'):
                    with self.assertRaises(TimeoutError):asyncio.run(uploader.run(args))
                saved=json.loads((root/'registry.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['applications']['10']['path_1']['id'],'123')
                pending=json.loads((root/'data/martial-emoji-upload-pending.json').read_text(encoding='utf-8'))
                self.assertNotIn('10:path_1',pending);self.assertIn('10:path_2',pending)
                self.assertNotIn('fixture-token',json.dumps(saved)+json.dumps(pending))


if __name__=='__main__':unittest.main()
