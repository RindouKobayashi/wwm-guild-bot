"""Build and validate staged portable assets; never reads player data or uses APIs."""
import csv
import ast
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
import sys

BOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BOT))
from utility.game_assets import inside, digest, validate_release
from utility.xlsx_reader import iter_xlsx_rows
from asset_review import review, render


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def unique(records, key):
    result = {}
    for row in records:
        identifier = str(row[key])
        if not identifier.isdigit() or identifier in result:
            raise ValueError('Duplicate or invalid exported ID: ' + identifier)
        result[identifier] = row
    if not result:
        raise ValueError('Required source contains no rows')
    return result


def copy_image(source_root, relative, destination_root, expected=None):
    source = inside(Path(source_root), relative)
    raw = source.read_bytes()
    if not 0 < len(raw) <= 8 * 1024 * 1024 or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Artwork is not a supported original PNG: ' + relative)
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Artwork changed; re-review required: ' + relative)
    target = inside(Path(destination_root), relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def legacy_tables():
    """Bootstrap existing reviewed local exports, explicitly not a fresh game scan."""
    mappings = read(BOT / 'activity/player_mappings.json')
    item_index = {}
    for row in iter_xlsx_rows(BOT / 'data/All_Item_Names.xlsx'):
        if len(row) >= 3 and str(row[0]).isdigit() and row[2]:
            item_index.setdefault(str(row[0]), []).append([str(row[1]), str(row[2]), str(row[3]) if len(row) > 3 else ''])
    # Prefer the bot's current detailed affix source over an older Activity copy.
    mappings['affixes'] = {key: {'name': row.get('english_name'), 'attribute_id': row.get('attribute_id'),
        'format': row.get('value_show_format', ''), 'minimum': row.get('minimum'), 'maximum': row.get('maximum')}
        for key, row in unique(rows(BOT / 'data/all_affix_id_english_names.csv'), 'affix_id').items() if row.get('english_name')}
    mappings['sources'] = ['portable game asset bundle; source provenance is in manifest.json']
    return {'player_mappings': mappings, 'item_index': item_index,
            'unmigrated_policy': ['energy constants', 'PvP grade labels', 'quality labels',
                                  'sect names', 'martial role classification', 'UI slot labels']}


def apply_exports(tables, run, metadata, selected):
    mappings = tables['player_mappings']
    if 'affixes' in selected:
        source = unique(rows(run / 'affixes/all_affix_details.csv'), 'affix_id')
        mappings['affixes'] = {key: {'name': row['english_name'], 'attribute_id': row.get('attribute_id'),
            'format': row.get('value_show_format', ''), 'minimum': row.get('minimum'), 'maximum': row.get('maximum')}
            for key, row in source.items() if row.get('english_name')}
    if 'equipment' in selected:
        source = unique(rows(run / 'equipment/all_equipment_item_details.csv'), 'item_id')
        mappings['equipment'] = {key: row['english_name'] for key, row in source.items() if row.get('english_name')}
        extra = read(metadata)
        if extra.get('unresolved_metadata'):
            raise ValueError('Equipment metadata has unresolved IDs; previous bundle retained')
        for key in ('equipment_metadata', 'equipment_sets', 'equipment_slots', 'legacy_limits'):
            if not extra.get(key):
                raise ValueError('Empty equipment metadata section: ' + key)
            mappings[key] = extra[key]
    if 'items' in selected:
        index, scoped, seen = {}, {}, set()
        for row in rows(run / 'items/all_item_names.csv'):
            identifier, category = row['item_id'], row['category']
            if (identifier, category) in seen:
                raise ValueError('Duplicate category/item identity')
            seen.add((identifier, category))
            if not row.get('english_name'):
                continue
            index.setdefault(identifier, []).append([category, row['english_name'], row.get('name_status', '')])
            scoped.setdefault(identifier, {}).setdefault(category, []).append(row['english_name'])
        tables['item_index'], mappings['items'] = index, scoped


def add_rankings(stage, dungeon_source, abyss_source, tables):
    dungeon_source, abyss_source = Path(dungeon_source), Path(abyss_source)
    pointer = read(dungeon_source / 'latest.json')
    dungeon_run = inside(dungeon_source, pointer['run'].replace('\\', '/'))
    manifest = read(dungeon_run / 'manifest.json')
    manifest.pop('source_run', None)
    boards = []
    unresolved = []
    for key, row in manifest['boards'].items():
        team, identifier = key.split(':')
        prefix = 'rank_team10_dungeon_' if team == 'hr' else 'rank_team_dungeon_' if team == 'st' else ''
        if not prefix or not identifier.isdigit() or row.get('rank_name') != prefix + identifier:
            raise ValueError('Unverified leaderboard identity')
        images = row.get('images') or []
        if images and (len(images) != 1 or row.get('art_status') not in
                       ('explicit_leaderboard_art', 'verified_dungeon_card', 'reviewed_dungeon_card')):
            raise ValueError('Dungeon art must be a verified whole-dungeon card')
        entry = dict(key=key, family=team, title=row['title'], rank_name=row['rank_name'])
        if images:
            image = images[0]
            copy_image(dungeon_run, image['file'], stage / 'dungeons/cards', image['sha256'])
            filename = key.replace(':', '-') + '.png'
            target = stage / 'activity/art' / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(inside(dungeon_run, image['file']), target)
            entry['image'] = '/art/' + filename
        else:
            unresolved.append({'id': key, 'reason': row.get('art_reason', 'No verified artwork')})
        boards.append(entry)
    write(stage / 'dungeons/cards/manifest.json', manifest)
    write(stage / 'dungeons/latest.json', {'run': 'cards'})
    abyss = read(abyss_source / 'catalogue.json')
    identifiers = set()
    for row in abyss['trials']:
        if str(row['id']) in identifiers:
            raise ValueError('Duplicate Abyss identity')
        identifiers.add(str(row['id']))
        entry = {**row, 'key': 'abyss:' + str(row['id']), 'family': 'abyss', 'title': row['name']}
        if row.get('image'):
            copy_image(abyss_source, row['image'], stage / 'abyss', row['sha256'])
            filename = 'abyss-' + str(row['id']) + '.png'
            shutil.copy2(inside(abyss_source, row['image']), stage / 'activity/art' / filename)
            entry['image'] = '/art/' + filename
        else:
            unresolved.append({'id': entry['key'], 'reason': 'Original Abyss art not exported'})
        boards.append(entry)
    write(stage / 'abyss/catalogue.json', abyss)
    write(stage / 'activity/catalogue.json', {'boards': boards, 'names': abyss['names']})
    # This section contains only names actually supplied by the game extractor.
    tables['martial_names'] = abyss.get('names', {}).get('martial', {})
    return unresolved


def changes(old, new):
    result = {}
    for name in ('affixes', 'equipment', 'items', 'equipment_metadata'):
        before, after = old.get('player_mappings', {}).get(name, {}), new['player_mappings'].get(name, {})
        result[name] = {'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()),
                        'changed': sorted(key for key in after.keys() & before.keys() if after[key] != before[key])}
    return result


def finish(stage, tables, provenance, old, allow_removals=False, old_directory=None):
    fallback = {}
    if tables.get('api_constants'):
        selected = {'SCHOOL_NAMES': 'school_names', 'SCHOOL_RANKING': 'school_ranks',
                    'VOTE_COUNTS': 'vote_counts', 'LIKES': 'likes', 'BOSS_NAMES': 'boss_names'}
        for node in ast.parse((BOT / 'utility/api_constants.py').read_text(encoding='utf-8')).body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in selected:
                        fallback[selected[target.id]] = {str(key): value for key, value in ast.literal_eval(node.value).items()}
        # Keep unsupported topics explicitly attributed, rather than claiming
        # that the profile UI table covers every like/unlike server topic.
        api = tables['api_constants']
        extracted_topics = {str(topic) for group in api['like_groups'].values() for topic in group['server_id']}
        api['like_compatibility'] = {key: value for key, value in fallback['likes'].items() if key not in extracted_topics}
        api['likes'] = {**api['like_compatibility'], **api['likes']}
        provenance['api_mapping_sources'] = api.get('sources', {})
        provenance['api_mapping_limitations'] = api.get('limitations', [])
    delta = changes(old, tables)
    write(stage / 'tables.json', tables)
    report = {'provenance': provenance, 'counts': {key: len(value) for key, value in tables['player_mappings'].items() if isinstance(value, dict)},
              'changes': delta, 'unresolved_artwork': provenance.get('unresolved_artwork', []),
              'manual_policy_still_used': tables.get('unmigrated_policy', [])}
    review_old = old
    if old and tables.get('api_constants') and not old.get('api_constants'):
        # First migration compares against the literal defaults actually used by
        # the old runtime. Parse only public named dictionaries, never settings.
        review_old = {**old, 'api_constants': fallback}
        report['provenance']['api_mapping_baseline'] = 'Public literal defaults from utility/api_constants.py before first bundle migration'
    report['review'] = review(review_old, tables, old_directory, stage)
    write(stage / 'report.json', report)
    (stage / 'report.html').write_text(render(report), encoding='utf-8')
    if old and any(row['removed'] for row in report['review']['summary'].values()) and not allow_removals:
        raise ValueError('IDs disappeared from extraction; inspect release/report.html before using --allow-removals')
    files = {path.relative_to(stage).as_posix(): digest(path) for path in sorted(stage.rglob('*')) if path.is_file() and path != stage / 'manifest.json'}
    write(stage / 'manifest.json', {'format': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                                  'provenance': provenance, 'counts': report['counts'], 'files': files})
    validate_release(stage)
    return report
