import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'maintenance/assets'))
from extract_martial import copy_export
from utility.emoji_startup import approval,sync,startup
from utility.emoji_upload import save
from utility.martial import read_registry


class FakeAPI:
    def __init__(self,app):self.app=app;self.items=[];self.posts=0;self.fail=False
    async def call(self,method,path,body=None):
        if path=='/oauth2/applications/@me':return {'id':self.app}
        if method=='GET':return {'items':self.items}
        self.posts+=1
        if self.fail:raise TimeoutError('fixture')
        value={'id':str(100000+self.posts),'name':body['name']};self.items.append(value);return value


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.directory=self.root/'release/martial'
        copy_export(ROOT/'assets/martial-review',self.directory)
        self.app=approval(self.directory,'test');self.api=FakeAPI(self.app)
    async def asyncTearDown(self):self.temp.cleanup()

    async def test_auto_create_then_reuse_keeps_checkout_clean(self):
        result=await sync(self.directory,'test','fixture',self.root,self.api)
        self.assertEqual(result['created'],11);self.assertTrue((self.root/'data/martial-emojis.json').exists())
        self.assertFalse((self.root/'assets/discord/martial-emojis.json').exists())
        result=await sync(self.directory,'test','fixture',self.root,self.api)
        self.assertEqual(result['created'],0);self.assertEqual(self.api.posts,11)
        text=(self.root/'data/martial-emojis.json').read_text(encoding='utf-8');self.assertNotIn('fixture',text)

    async def test_wrong_application_no_publication(self):
        self.api.app='999'
        with self.assertRaises(ValueError):await sync(self.directory,'production','fixture',self.root,self.api)
        self.assertEqual(self.api.posts,0)

    async def test_tampered_manifest_no_publication(self):
        path=self.directory/'upload-pack/upload-manifest.json';path.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):await sync(self.directory,'test','fixture',self.root,self.api)
        self.assertEqual(self.api.posts,0)

    async def test_uncertain_post_never_automatically_resent(self):
        self.api.fail=True
        with self.assertRaises(TimeoutError):await sync(self.directory,'test','fixture',self.root,self.api)
        self.api.fail=False
        with self.assertRaises(ValueError):await sync(self.directory,'test','fixture',self.root,self.api)
        self.assertEqual(self.api.posts,1)

    async def test_explicit_rejection_can_retry_next_start(self):
        from utility.emoji_upload import RejectedUpload
        original=self.api.call
        async def reject(method,path,body=None):
            if method=='POST':raise RejectedUpload('Rate limited')
            return await original(method,path,body)
        with patch.object(self.api,'call',side_effect=reject):
            with self.assertRaises(RejectedUpload):await sync(self.directory,'test','fixture',self.root,self.api)
        pending=json.loads((self.root/'data/martial-emoji-upload-pending.json').read_text(encoding='utf-8'))
        self.assertEqual(pending,{})
        result=await sync(self.directory,'test','fixture',self.root,self.api)
        self.assertEqual(result['created'],11)

    async def test_local_receipts_override_seed_per_application(self):
        public=self.root/'public.json';local=self.root/'local.json'
        save(public,dict(applications={'10':{'path_1':{'id':'1'}}},environments={'test':'10'}))
        save(local,dict(applications={'20':{'path_1':{'id':'2'}}},environments={'production':'20'}))
        registry=read_registry(public,local)
        self.assertEqual(registry['environments'],{'test':'10','production':'20'})
        self.assertEqual(registry['applications']['10']['path_1']['id'],'1')

    async def test_startup_timeout_is_nonfatal_and_recorded(self):
        from types import SimpleNamespace
        async def hang(*args):await asyncio.sleep(10)
        assets=SimpleNamespace(error=None,directory=self.directory.parent,section=lambda _:True)
        with patch('utility.emoji_startup.GameAssets',return_value=assets),patch('utility.emoji_startup.sync',side_effect=hang):
            result=await startup('test','fixture',self.root,timeout=.02)
        self.assertEqual(result['state'],'incomplete');self.assertEqual(result['error'],'TimeoutError')
        self.assertNotIn('fixture',(self.root/'data/martial-emoji-sync-status.json').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
