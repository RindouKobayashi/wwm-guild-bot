"""Synthetic emote fixtures only: no real messages, Discord or game access."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / 'maintenance/assets')]
from utility.emotes import EmoteResolver, key, validate, inspect_image
from utility.game_assets import digest
from emote_animation import reuse_published_animation


def fixture(root):
    from PIL import Image
    (root / 'art').mkdir(parents=True, exist_ok=True)
    path = root / 'art/topic_101.png'
    Image.new('RGBA', (32, 32), (30, 80, 120, 128)).save(path)
    row = dict(id=101, source_table='chat_emotion_icon_lib', name='Wave', group_id=3,
               status='static', image='art/topic_101.png', image_sha256=digest(path),
               preview='art/topic_101.png', preview_sha256=digest(path))
    data = dict(format=1, entries={'101': row}, inline_entries={})
    (root / 'catalogue.json').write_text(json.dumps(data), encoding='utf-8')
    (root / 'contact-sheet.html').write_text('Synthetic fixture', encoding='utf-8')
    return data


class EmoteAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.data = fixture(self.root / 'emotes')
        self.assets = SimpleNamespace(directory=self.root, section=lambda *a: self.data)

    def tearDown(self): self.temp.cleanup()

    def resolver(self): return EmoteResolver(self.assets, self.root / 'manual')

    def test_animation_reuse_needs_matching_original_bytes(self):
        from PIL import Image

        published = self.root / 'emotes'
        output = self.root / 'new'
        (output / 'art').mkdir(parents=True)
        entry = dict(self.data['entries']['101'], status='animated', effect='repeat')
        gif = published / 'art/topic_101.gif'
        frames = [Image.new('RGB', (32, 32), color) for color in ('red', 'blue')]
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=40, loop=0)
        inputs = dict(lua={'sha256': 'script'}, csb='layout', csb_sha256='layout-hash', puppet='puppet',
                      puppet_uuid='identifier', puppet_sha256='skeleton', texture_sha256='image', atlas_transform={})
        entry.update(image='art/topic_101.gif', image_sha256=digest(gif),
                     animation={**inputs, 'registry_sha256': 'previous',
                                'capture': {'animation': 'vx_loop', 'fps': 30, 'max_size': 384}})
        job = {'effect': 'repeat', 'evidence': {**inputs, 'registry_sha256': 'current'}}
        changed = {'effect': 'repeat', 'evidence': {**inputs, 'texture_sha256': 'replacement'}}
        self.assertIsNone(reuse_published_animation(entry, published, changed, output))
        self.assertFalse((output / entry['image']).exists())
        reused = reuse_published_animation(entry, published, job, output)
        self.assertEqual(digest(output / entry['image']), entry['image_sha256'])
        self.assertEqual(reused['animation']['registry_sha256'], 'current')

    def test_numeric_ids_and_traversal(self):
        for value in (True, None, {}, [], '../101', '1/101', -1, '0', '9'*20): self.assertIsNone(key(value))
        self.assertEqual(key('00101'), '101'); self.assertEqual(key(101), '101')

    def test_namespaces_never_cross_resolve(self):
        resolver = self.resolver()
        self.assertEqual(resolver.resolve('101', '2').name, 'Wave')
        self.assertIsNone(resolver.resolve(101, '1'))
        self.assertIsNone(resolver.resolve(101, 3))
        self.data['inline_entries']['999'] = {**self.data['entries']['101'], 'id': 999, 'source_table': 'chat_emotion_frame_data'}
        self.assertIsNone(self.resolver().resolve(999))

    def test_game_then_manual_then_unknown(self):
        from PIL import Image
        manual = self.root / 'manual'; manual.mkdir()
        Image.new('RGB', (20, 20)).save(manual / '101.png')
        resolver = self.resolver()
        self.assertEqual(resolver.resolve(101).source, 'Game assets')
        (self.root / 'emotes/art/topic_101.png').unlink()
        self.assertEqual(resolver.resolve(101).source, 'Manual fallback')
        (manual / '101.png').unlink(); self.assertIsNone(resolver.resolve(101))

    def test_manual_approval_registration(self):
        from PIL import Image
        resolver = self.resolver(); resolver.manual_dir.mkdir()
        Image.new('RGB', (20, 20)).save(resolver.manual_dir / '102.png')
        self.assertIsNone(resolver.resolve(102)); resolver.register_manual('102')
        self.assertEqual(resolver.resolve(102).source, 'Manual fallback')

    def test_corrupt_manual_does_not_break_startup(self):
        manual = self.root / 'manual'; manual.mkdir(); (manual / '102.webp').write_bytes(b'not a webp')
        self.assertIsNone(self.resolver().resolve(102))

    def test_resolve_never_decodes_or_fetches(self):
        resolver = self.resolver()
        with patch('utility.emotes.inspect_image', side_effect=AssertionError('runtime decode')):
            self.assertIsNotNone(resolver.resolve(101).for_limit(1024 * 1024))

    def test_destination_limit(self):
        self.assertIsNone(self.resolver().resolve(101).for_limit(1))

    def test_unknown_description_is_safe(self):
        from utility.emotes import fallback_text
        self.assertEqual(fallback_text('999'), 'Unknown emote (999)')
        self.assertEqual(fallback_text(None, {}), 'Unknown emote (missing ID)')
        self.assertNotIn('@everyone', fallback_text(999, '**hello** @everyone'))

    def test_catalogue_rejects_mixed_namespace_and_unsafe_paths(self):
        validate(self.data)
        for field, value in [('id', 2), ('source_table', 'chat_emotion_frame_data'), ('image', '../token.png'), ('status', 'guessed'), ('image_sha256', 'bad')]:
            bad = copy.deepcopy(self.data); bad['entries']['101'][field] = value
            with self.assertRaises(ValueError): validate(bad)

    def test_bundle_checksums_and_copy_export(self):
        from test_game_asset_pipeline import fixture as bundle_fixture
        from extract_emotes import copy_export
        from build_game_bundle import finish
        from utility.game_assets import validate_release
        stage = self.root / 'release'; stage.mkdir(); tables = bundle_fixture(stage)
        copy_export(self.root / 'emotes', stage / 'emotes'); tables['emotes'] = self.data
        finish(stage, tables, {'mode': 'synthetic'}, {}); validate_release(stage)
        (stage / 'emotes/art/topic_101.png').write_bytes(b'changed')
        with self.assertRaises(ValueError): validate_release(stage)

    def test_copy_rejects_changed_image(self):
        from extract_emotes import copy_export
        (self.root / 'emotes/art/topic_101.png').write_bytes(b'changed')
        with self.assertRaises(ValueError): copy_export(self.root / 'emotes', self.root / 'copy')

    def test_gif_frame_order_timing_and_transparency(self):
        from PIL import Image
        from extract_emotes import make_gif
        paths = []
        for index, color in enumerate(((255, 0, 0, 255), (0, 255, 0, 255))):
            path = self.root / f'{index}.png'; image = Image.new('RGBA', (16, 16), color)
            image.putpixel((0, 0), (0, 0, 0, 0)); image.save(path); paths.append(path)
        gif = self.root / 'test.gif'; make_gif(paths, gif)
        self.assertTrue(inspect_image(gif))
        with Image.open(gif) as image:
            self.assertEqual(image.n_frames, 2); self.assertEqual(image.info['loop'], 0)
            for index in range(2):
                image.seek(index); self.assertEqual(image.info['duration'], 200)
                self.assertEqual(image.convert('RGBA').getpixel((0, 0))[3], 0)
                self.assertEqual(image.convert('RGBA').getpixel((1, 1))[index], 255)

    def test_field_level_review(self):
        from asset_review import review
        after = copy.deepcopy(self.data); after['entries']['101']['name'] = 'New name'
        report = review({'emotes': self.data}, {'emotes': after})
        self.assertEqual(report['summary']['emotes.entries']['changed'], 1)

    def test_searchable_contact_sheet_escapes_names(self):
        from extract_emotes import render_preview
        self.data['entries']['101']['name'] = '<script>secret()</script>'
        render_preview(self.data, self.root)
        page = (self.root / 'contact-sheet.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>secret()', page); self.assertIn('type=search', page)

    def test_animation_uses_static_preview_when_over_destination_limit(self):
        from dataclasses import replace
        entry = self.resolver().resolve(101)
        large = self.root / 'large.gif'; large.write_bytes(b'x' * 1000)
        entry = replace(entry, path=large, status='animated')
        self.assertEqual(entry.for_limit(500), entry.preview)

    def test_extension_and_dimension_validation(self):
        from PIL import Image
        path = self.root / 'wrong.webp'; Image.new('RGB', (10, 10)).save(path, format='PNG')
        with self.assertRaises(ValueError): inspect_image(path)
        path = self.root / 'large.png'; Image.new('RGB', (2049, 1)).save(path)
        with self.assertRaises(ValueError): inspect_image(path)

    def test_primary_index_count_discrepancy_is_reported_not_truncated(self):
        import struct
        from extract_emotes import named_rows
        rows = [(101, struct.pack('<4H', 1, 1, 0, 1), 0, 0), (102, struct.pack('<4H', 1, 1, 1, 1), 1, 0)]
        raw = struct.pack('<3Q', 3, 2, 0)
        def read(reader, schema):
            reader.pos = 1
            return {0: reader.data[0]}
        U = SimpleNamespace(frames=lambda p: [(10, b'schema'), (11, raw)], frame_records=lambda r: rows,
            load_frames=lambda *a: {(1, 1):bytes([101])+bytes(7)+bytes([102])+bytes(7)},
            Reader=lambda data: SimpleNamespace(data=data, pos=0), read_row=read)
        D = SimpleNamespace(U=U, decode_name_list=lambda r:['id'])
        binding = dict(archive='index1', frame=10, pages=1, records=1)
        with patch('named_mtable.installed_binding', return_value=binding), patch('extract_emotes.digest', return_value='a'*64):
            result, _, receipt = named_rows(self.root, 'chat_emotion_icon_lib', D)
            self.assertEqual(set(result), {101,102}); self.assertEqual(receipt['decoded_primary_records'], 2)
            rows[1] = rows[0]
            with self.assertRaisesRegex(ValueError, 'Duplicate'): named_rows(self.root, 'chat_emotion_icon_lib', D)

    def test_primary_index_rejects_row_id_mismatch(self):
        import struct
        from extract_emotes import named_rows
        U = SimpleNamespace(frames=lambda p: [(10,b'schema'), (11,struct.pack('<3Q',3,1,0))],
            frame_records=lambda r:[(101,struct.pack('<4H',1,1,0,1),0,0)],
            load_frames=lambda *a:{(1,1):bytes(8)}, Reader=lambda d:SimpleNamespace(pos=1),
            read_row=lambda *a:{0:999})
        D = SimpleNamespace(U=U, decode_name_list=lambda r:['id'])
        with patch('named_mtable.installed_binding', return_value=dict(archive='index1', frame=10, pages=1, records=1)):
            with self.assertRaisesRegex(ValueError, 'namespace'): named_rows(self.root, 'chat_emotion_icon_lib', D)

    def test_changed_routing_source_requires_review(self):
        from extract_emotes import lua_evidence
        with patch('original_lua.read_original', return_value=(b'changed', {'sha256':'unreviewed'})):
            with self.assertRaisesRegex(ValueError, 'source logic changed'): lua_evidence(self.root, self.root)

    def test_stationary_animation_uses_static_fallback(self):
        from extract_emotes import make_gif
        path = self.root / 'emotes/art/topic_101.png'
        with self.assertRaisesRegex(ValueError, 'no moving frames'):
            make_gif([path, path], self.root / 'stationary.gif')

    def test_atlas_crop_preserves_pixels_and_alpha(self):
        from PIL import Image
        from emote_atlas import crop_frame
        atlas = Image.new('RGBA', (8, 8), (0, 0, 0, 0))
        atlas.putpixel((3, 4), (100, 150, 200, 128))
        definition = dict(frame='{{2,3},{2,2}}', sourceColorRect='{{1,1},{2,2}}',
            sourceSize='{4,4}', offset='{0,0}', rotated=False)
        image = crop_frame(atlas, definition)
        self.assertEqual(image.size, (4,4))
        self.assertEqual(image.getpixel((2,2)), (100,150,200,128))
        self.assertEqual(image.getpixel((0,0))[3], 0)

    def test_atlas_rejects_unverified_rotation_and_invalid_geometry(self):
        from PIL import Image
        from emote_atlas import crop_frame
        atlas = Image.new('RGBA', (8,8))
        base = dict(frame='{{2,3},{2,2}}', sourceColorRect='{{1,1},{2,2}}',
            sourceSize='{4,4}', offset='{0,0}', rotated=False)
        for field,value in [('rotated',True),('frame','{{7,7},{2,2}}'),('sourceColorRect','{{3,3},{2,2}}'),('offset','{1,1}'),('sourceSize','{99999,99999}')]:
            with self.assertRaises(ValueError): crop_frame(atlas, {**base,field:value})

    def test_atlas_pair_parser_rejects_malformed_data(self):
        from emote_atlas import pair, rectangle
        self.assertEqual(pair('{0.5,-1.5}'), (0.5,-1.5))
        for value in ('nan', '{nan,1}', '[1,2]', None):
            with self.assertRaises(ValueError): pair(value)
        with self.assertRaises(ValueError): rectangle('{{1,2},{-3,4}}')

    def test_identical_source_frames_are_still_but_alpha_changes_are_not(self):
        from PIL import Image
        from extract_emotes import identical_frames
        first = self.root / 'first.png'; second = self.root / 'second.png'
        Image.new('RGBA',(8,8),(200,30,50,255)).save(first)
        self.assertTrue(identical_frames([first,first]))
        Image.new('RGBA',(8,8),(200,30,50,128)).save(second)
        self.assertFalse(identical_frames([first,second]))


class EmoteChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cogs.live_chat_cog import LiveChatCog
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        data = fixture(self.root / 'emotes')
        resolver = EmoteResolver(SimpleNamespace(directory=self.root, section=lambda *a: data), self.root / 'manual')
        self.cog = SimpleNamespace(emotes=resolver, seen_emotion_ids=set(),
            _get_rank_name=lambda x: 'Unknown', _get_discord_mention=lambda x: '', _avatar_path=lambda *a: None)
        self.cog._emotion_path = lambda eid: LiveChatCog._emotion_path(self.cog, eid)
        self.cog._should_offer_emotion_upload = lambda eid: LiveChatCog._should_offer_emotion_upload(self.cog, eid)
        self.post = lambda msg: LiveChatCog._post_v2_for_message(self.cog, self.channel, msg, None)
        self.channel = SimpleNamespace(send=AsyncMock(), guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024))

    async def asyncTearDown(self): self.temp.cleanup()

    def message(self, eid=101):
        return dict(ext=dict(msg_type='msg_emotion', emotion_id=eid, emotion_type='2'), nickname='Synthetic sender', ts=1700000000, level=42)

    async def test_known_emote_preserves_metadata_without_upload_button(self):
        await self.post(self.message())
        kwargs = self.channel.send.call_args.kwargs; content = str(kwargs['view'].to_components())
        self.assertIn('Synthetic sender', content); self.assertIn('1700000000', content)
        self.assertIn('attachment://emote_101.png', content); self.assertNotIn('Set Emote', content)
        for file in kwargs['files']: file.close()

    async def test_unknown_and_missing_preserve_message(self):
        for eid in (999, None, '../token'):
            await self.post(self.message(eid)); content = str(self.channel.send.call_args.kwargs['view'].to_components())
            self.assertIn('Unknown emote', content); self.assertIn('Synthetic sender', content)

    async def test_small_destination_uses_text(self):
        self.channel.guild.filesize_limit = 1
        await self.post(self.message()); self.assertEqual(self.channel.send.call_args.kwargs['files'], [])

    async def test_static_preview_label(self):
        from dataclasses import replace
        self.cog.emotes.entries['101'] = replace(self.cog.emotes.entries['101'], status='static_preview')
        await self.post(self.message()); kwargs = self.channel.send.call_args.kwargs
        self.assertIn('Static preview', str(kwargs['view'].to_components()))
        for file in kwargs['files']: file.close()

    async def test_definitive_rejection_uses_one_text_fallback(self):
        import discord
        error = discord.HTTPException(SimpleNamespace(status=413, reason='Too large'), {'code':40005, 'message':'Too large'})
        self.channel.send.side_effect = [error, Mock()]
        await self.post(self.message()); self.assertEqual(self.channel.send.await_count, 2)
        self.assertNotIn('files', self.channel.send.call_args.kwargs)

    async def test_uncertain_send_never_retries(self):
        self.channel.send.side_effect = TimeoutError('Unknown delivery')
        await self.post(self.message()); self.assertEqual(self.channel.send.await_count, 1)
        for file in self.channel.send.call_args.kwargs['files']: file.close()

    async def test_custom_id_cannot_display_topic_art(self):
        message = self.message(); message['ext']['emotion_type'] = '1'
        await self.post(message); kwargs = self.channel.send.call_args.kwargs
        self.assertEqual(kwargs['files'], []); self.assertNotIn('Set Emote', str(kwargs['view'].to_components()))

    async def test_manual_submission_approval_and_restart(self):
        from cogs import live_chat_cog as module
        from PIL import Image
        import io
        raw = io.BytesIO(); Image.new('RGBA', (20, 20), (5, 80, 20, 255)).save(raw, format='PNG')
        attachment = SimpleNamespace(filename='upload.png', read=AsyncMock(return_value=raw.getvalue()))
        self.cog._send_admin_emotion_approval = AsyncMock(); self.cog.save_config = Mock()
        manual = self.root / 'manual'; pending = manual / 'pending'
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(), user=SimpleNamespace(mention='Synthetic owner'))
        with patch.object(module, 'EMOTION_DIR', manual), patch.object(module, 'EMOTION_PENDING_DIR', pending), patch.object(module, 'is_admin_or_staff', AsyncMock(return_value=True)):
            ok, filename = await module.LiveChatCog._stage_emotion_upload(self.cog, attachment=attachment,
                emotion_id='102', suggested_by=Mock(), sender_nickname='Synthetic sender', sender_pid=None, source_message_jump_url=None)
            self.assertTrue(ok); self.assertTrue((pending / filename).exists())
            self.assertIsNone(self.cog.emotes.resolve(102))
            view = module.AdminEmotionConfirmView(cog=self.cog, emotion_id='102', pending_filename=filename,
                target_filename=filename, suggested_by_id=None, source_message_jump_url=None)
            self.assertTrue(view.is_persistent())
            await view._on_approve(interaction)
            self.assertEqual(self.cog.emotes.resolve(102).source, 'Manual fallback')
            self.assertFalse((pending / filename).exists())
            restored = EmoteResolver(SimpleNamespace(directory=None), manual)
            self.assertEqual(restored.resolve(102).source, 'Manual fallback')

    async def test_invalid_upload_and_unsafe_id_never_stage(self):
        from cogs import live_chat_cog as module
        manual = self.root / 'manual'; pending = manual / 'pending'
        self.cog._send_admin_emotion_approval = AsyncMock()
        attachment = SimpleNamespace(filename='bad.png', read=AsyncMock(return_value=b'not an image'))
        with patch.object(module, 'EMOTION_DIR', manual), patch.object(module, 'EMOTION_PENDING_DIR', pending):
            for identifier in ('../secret', '102'):
                ok, _ = await module.LiveChatCog._stage_emotion_upload(self.cog, attachment=attachment,
                    emotion_id=identifier, suggested_by=Mock(), sender_nickname='Synthetic', sender_pid=None, source_message_jump_url=None)
                self.assertFalse(ok)
            self.cog._send_admin_emotion_approval.assert_not_called()
            self.assertFalse((pending / '102.png').exists())


if __name__ == '__main__': unittest.main()
