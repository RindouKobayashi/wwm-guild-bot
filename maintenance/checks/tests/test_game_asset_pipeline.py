"""Offline asset publication and corruption regressions. No game or bot launch."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BOT))
sys.path.insert(0, str(BOT / 'maintenance/assets'))
from utility.game_assets import GameAssets, inside, validate_release
from build_game_bundle import finish, write
from update_game_assets import publish, prepare_review, apply_review
from utility.game_assets import digest
from asset_review import review, render
from extract_api_constants import converted
from utility.api_mapping_data import validate as validate_api_mappings
from extract_like_topics import assignments


def fixture(stage):
    tables = {'player_mappings': {'affixes': {'1': {'name': 'Attack', 'minimum': '1', 'maximum': '2'}},
              'equipment': {'2': 'Sword'}, 'items': {'3001': {'money': ['Coin'], 'title': ['Title']}},
              'constants': {'MAX_ENERGY': 600}},
              'item_index': {'3001': [['money', 'Coin', 'official'], ['title', 'Title', 'official']]}}
    write(stage / 'dungeons/latest.json', {'run': 'cards'})
    write(stage / 'dungeons/cards/manifest.json', {'boards': {'hr:1': {
        'title': 'Dungeon', 'rank_name': 'rank_team10_dungeon_1', 'images': [], 'art_status': 'unresolved'}}})
    write(stage / 'abyss/catalogue.json', {'trials': [{'id': 1, 'name': 'Boss'}], 'names': {}})
    write(stage / 'activity/catalogue.json', {'boards': [{'key': 'hr:1', 'title': 'Dungeon'}], 'names': {}})
    finish(stage, tables, {'mode': 'synthetic_test'}, {})
    return tables


class AssetPipelineTests(unittest.TestCase):
    def test_lua_topic_symbols_require_direct_constant_assignments(self):
        word = 24 | (1 << 15) | (1 << 24)
        tree = {'constants': ['TOPIC_WORLD_AREA', 102], 'instructions': [word]}
        self.assertEqual(assignments(tree), {'102': 'TOPIC_WORLD_AREA'})
        tree['instructions'] = [word & ~(1 << 15)]
        with self.assertRaises(ValueError):
            assignments(tree)

    def test_prepare_leaves_current_unchanged_and_apply_is_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'builds/run'
            fixture(work / 'release')
            prepare_review(work, None)
            self.assertFalse((root / 'published/current.json').exists())
            apply_review(work, root / 'published')
            self.assertIsNone(GameAssets(root / 'published').error)

    def test_apply_rejects_candidate_changed_after_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'builds/run'
            fixture(work / 'release')
            prepare_review(work, None)
            with (work / 'release/report.html').open('a') as handle:
                handle.write('changed')
            with self.assertRaises(ValueError):
                apply_review(work, root / 'published')
            self.assertFalse((root / 'published/current.json').exists())

    def test_apply_rejects_stale_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'builds/run'
            fixture(work / 'release')
            publish(work / 'release', root / 'published')
            prepare_review(work, digest(root / 'published/current.json'))
            tables = json.loads((work / 'release/tables.json').read_text())
            tables['player_mappings']['equipment']['2'] = 'New sword'
            finish(work / 'release', tables, {}, {})
            publish(work / 'release', root / 'published')
            before = (root / 'published/current.json').read_bytes()
            with self.assertRaises(ValueError):
                apply_review(work, root / 'published')
            self.assertEqual((root / 'published/current.json').read_bytes(), before)

    def test_publish_uses_stable_folder_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / 'stage')
            publish(root / 'stage', root / 'published')
            previous = (root / 'published/current/tables.json').read_bytes()
            publish(root / 'stage', root / 'published')
            self.assertEqual(json.loads((root / 'published/current.json').read_text())['release'], 'current')
            self.assertFalse((root / 'published/releases').exists())
            backups = list((root / '.asset-backups').glob('*-previous/tables.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), previous)

    def test_pointer_failure_restores_previous_current_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tables = fixture(root / 'stage')
            publish(root / 'stage', root / 'published')
            before = (root / 'published/current.json').read_bytes()
            tables['player_mappings']['equipment']['2'] = 'New sword'
            finish(root / 'stage', tables, {}, {})
            with patch.object(Path, 'replace', side_effect=OSError('simulated pointer failure')):
                with self.assertRaises(OSError):
                    publish(root / 'stage', root / 'published')
            self.assertEqual((root / 'published/current.json').read_bytes(), before)
            self.assertIsNone(GameAssets(root / 'published').error)
            self.assertEqual(GameAssets(root / 'published').tables['player_mappings']['equipment']['2'], 'Sword')

    def test_apply_requires_explicit_acceptance_of_removals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'builds/run'
            tables = fixture(work / 'release')
            old = json.loads(json.dumps(tables))
            old['player_mappings']['affixes']['99'] = {'name': 'Old', 'minimum': 1, 'maximum': 2}
            finish(work / 'release', tables, {}, old, True)
            prepare_review(work, None)
            with self.assertRaises(ValueError):
                apply_review(work, root / 'published')
            self.assertFalse((root / 'published/current.json').exists())
            apply_review(work, root / 'published', allow_removals=True)
            self.assertIsNone(GameAssets(root / 'published').error)

    def test_sect_and_like_namespaces_and_zero_votes(self):
        result = converted({'4': {'no': 4, 'sect_name': 'Silver Needle'}},
            {'4_1': {'id': 17, 'school': 4, 'identify': 1, 'name_school_identity': 'Keeper', 'vote_time': 0}},
            {'9': {'id': 9, 'upvote_text': 'Trial Likes', 'server_id': [103, 114]}})
        validate_api_mappings(result)
        self.assertEqual(result['vote_counts']['4_1'], 0)
        self.assertEqual(result['likes'], {'103': 'Trial Likes', '114': 'Trial Likes'})
        self.assertNotIn('9', result['likes'])
        result['school_statuses']['4_1']['identify'] = 2
        with self.assertRaises(ValueError):
            validate_api_mappings(result)

    def test_duplicate_like_topic_or_bad_status_join_rejected(self):
        sects = {'4': {'no': 4, 'sect_name': 'Silver Needle'}}
        statuses = {'4_1': {'id': 17, 'school': 4, 'identify': 1, 'name_school_identity': 'Keeper', 'vote_time': 1}}
        likes = {'1': {'id': 1, 'upvote_text': 'Likes', 'server_id': [103, 103]}}
        with self.assertRaises(ValueError):
            converted(sects, statuses, likes)
        statuses['4_1']['school'] = 3
        with self.assertRaises(ValueError):
            converted(sects, statuses, {'1': {'upvote_text': 'Likes', 'server_id': [103]}})

    def test_review_reports_field_values_and_all_mapping_sections(self):
        before = {'player_mappings': {'affixes': {'1': {'name': 'Attack', 'minimum': 1, 'maximum': 2}},
                  'equipment_sets': {'7': 'Old set'}}}
        after = {'player_mappings': {'affixes': {'1': {'name': 'Attack', 'minimum': 1, 'maximum': 3},
                   '2': {'name': '</script><script>unsafe()</script>'}}, 'equipment_sets': {}}}
        result = review(before, after)
        self.assertEqual(result['summary']['affixes']['changed'], 1)
        self.assertEqual(result['summary']['equipment_sets']['removed'], 1)
        row = next(row for row in result['records'] if row['id'] == '1')
        self.assertEqual(row['fields'][0]['field'], 'maximum')
        self.assertEqual((row['fields'][0]['before'], row['fields'][0]['after']), (2, 3))
        self.assertNotIn('</script><script>unsafe()', render({'review': result}))

    def test_review_detects_art_content_changes_with_same_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in [('old', b'old'), ('new', b'new')]:
                write(root / name / 'activity/catalogue.json', {'boards': [{'key': 'hr:1', 'image': '/art/1.png'}]})
                target = root / name / 'activity/art/1.png'
                target.parent.mkdir(parents=True)
                target.write_bytes(content)
            result = review({'player_mappings': {}}, {'player_mappings': {}}, root / 'old', root / 'new')
            self.assertEqual(result['summary']['ranking_boards']['changed'], 1)
            self.assertEqual(result['records'][0]['fields'][0]['field'], 'artwork_sha256')

    def test_publish_and_load_without_game_or_research_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / 'stage')
            publish(root / 'stage', root / 'published')
            bundle = GameAssets(root / 'published')
            self.assertIsNone(bundle.error)
            self.assertEqual(bundle.section('item_index')['3001'][0][0], 'money')
            self.assertEqual(len(bundle.section('item_index')['3001']), 2)

    def test_corrupt_stage_does_not_replace_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root / 'stage')
            publish(root / 'stage', root / 'published')
            before = (root / 'published/current.json').read_bytes()
            (root / 'stage/tables.json').write_text('{}')
            with self.assertRaises(ValueError):
                publish(root / 'stage', root / 'published')
            self.assertEqual((root / 'published/current.json').read_bytes(), before)

    def test_public_bundle_rejects_private_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            tables = fixture(stage)
            (stage / '.env').write_text('SYNTHETIC_SECRET=not-a-real-secret')
            # Rehashing must not legitimize a forbidden file.
            (stage / 'manifest.json').unlink()
            with self.assertRaises(ValueError):
                finish(stage, tables, {}, {})

    def test_cross_platform_path_escape_rejected(self):
        for path in ('../private', 'C:/private', '/tmp/private', 'bad\\path', 'a/../../private'):
            with self.assertRaises(ValueError):
                inside(Path('.'), path)

    def test_id_removal_is_blocked_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            tables = fixture(stage)
            old = json.loads(json.dumps(tables))
            old['player_mappings']['affixes']['99'] = {'name': 'Old', 'minimum': 1, 'maximum': 2}
            with self.assertRaises(ValueError):
                finish(stage, tables, {}, old)
            report = json.loads((stage / 'report.json').read_text())
            self.assertEqual(report['changes']['affixes']['removed'], ['99'])
            self.assertTrue((stage / 'report.html').is_file())

    def test_rehashed_wrong_rank_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            tables = fixture(stage)
            write(stage / 'dungeons/cards/manifest.json', {'boards': {'hr:1': {
                'title': 'Bad', 'rank_name': 'rank_team_dungeon_1', 'images': []}}})
            (stage / 'manifest.json').unlink()
            with self.assertRaises(ValueError):
                finish(stage, tables, {}, {})

    def test_schema_versions_are_explicit_not_fuzzy(self):
        path = BOT.parent / 'WWM-Toolkit/app/table_discovery.py'
        if not path.exists():
            self.skipTest('Optional extractor checkout is not installed')
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location('asset_table_discovery_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reference = {'archive': 'old', 'fields': ['id', 'name', 'old_field'],
                     'schema_versions': [{'fields': ['id', 'name', 'new_field']}]}
        tables = [{'archive': 'new', 'fields': fields, 'records': 1}
                  for fields in (['id', 'name'], ['id', 'name', 'new_field'])]
        self.assertEqual(module.resolve_all({'tables': tables}, reference), [tables[1]])


if __name__ == '__main__':
    unittest.main()
