"""Validated, portable game assets shared by Discord and the Activity.

No settings, game installation, extractor, player database or network imports.
Processes pin one validated release; restart after publishing an update.
"""
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'assets/wwm'


def inside(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or '..' in name.split('/') or Path(name).is_absolute()):
        raise ValueError('Asset paths must be portable relative paths')
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Asset path escapes its release')
    return path


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def validate_release(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('Unsupported asset manifest')
    for name in ('tables.json', 'activity/catalogue.json', 'dungeons/latest.json', 'abyss/catalogue.json'):
        if name not in manifest['files']:
            raise ValueError('Required asset file missing from manifest: ' + name)
    for name, expected in manifest['files'].items():
        fixed = {'tables.json', 'report.json', 'report.html', 'activity/catalogue.json',
                 'dungeons/latest.json', 'dungeons/cards/manifest.json', 'abyss/catalogue.json'}
        artwork = name.endswith('.png') and name.startswith(('activity/art/', 'abyss/art/', 'dungeons/cards/artwork/'))
        if name not in fixed and not artwork:
            raise ValueError('Unexpected/private file in public bundle: ' + name)
        path = inside(directory, name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('Asset checksum mismatch: ' + name)
    tables = json.loads((directory / 'tables.json').read_text(encoding='utf-8'))
    mappings = tables.get('player_mappings', {})
    if 'api_constants' in tables:
        from utility.api_mapping_data import validate
        validate(tables['api_constants'])
    for name in ('affixes', 'equipment', 'items', 'constants'):
        if not isinstance(mappings.get(name), dict) or not mappings[name]:
            raise ValueError('Empty required mapping: ' + name)
    for row in mappings['affixes'].values():
        low, high = row.get('minimum'), row.get('maximum')
        if any(value not in ('', None) and not math.isfinite(float(value)) for value in (low, high)):
            raise ValueError('Nonfinite affix reference')
        if low not in ('', None) and high not in ('', None) and float(low) > float(high):
            raise ValueError('Affix minimum exceeds maximum')
    catalogue = json.loads((directory / 'activity/catalogue.json').read_text(encoding='utf-8'))
    keys = [row['key'] for row in catalogue['boards']]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError('Empty or duplicate ranking identities')
    for row in catalogue['boards']:
        if row.get('image'):
            relative = 'activity/' + row['image'].lstrip('/')
            if relative not in manifest['files']:
                raise ValueError('Ranking references untracked artwork')
    dungeon_pointer = json.loads((directory / 'dungeons/latest.json').read_text(encoding='utf-8'))
    dungeon_relative = 'dungeons/' + dungeon_pointer['run'] + '/manifest.json'
    if dungeon_relative not in manifest['files']:
        raise ValueError('Dungeon manifest is not tracked')
    dungeon_manifest = json.loads(inside(directory, dungeon_relative).read_text(encoding='utf-8'))
    for key, row in dungeon_manifest['boards'].items():
        team, identifier = key.split(':')
        prefix = 'rank_team10_dungeon_' if team == 'hr' else 'rank_team_dungeon_' if team == 'st' else ''
        if not prefix or not identifier.isdigit() or row.get('rank_name') != prefix + identifier:
            raise ValueError('Unverified dungeon identity')
        images = row.get('images') or []
        if images and (len(images) != 1 or row.get('art_status') not in
                       ('explicit_leaderboard_art', 'verified_dungeon_card', 'reviewed_dungeon_card')):
            raise ValueError('Invalid dungeon artwork ownership')
        for image in images:
            relative = 'dungeons/' + dungeon_pointer['run'] + '/' + image['file']
            if relative not in manifest['files'] or digest(inside(directory, relative)) != image['sha256']:
                raise ValueError('Dungeon image checksum mismatch')
    abyss = json.loads((directory / 'abyss/catalogue.json').read_text(encoding='utf-8'))
    identifiers = [str(row['id']) for row in abyss['trials']]
    if not identifiers or len(identifiers) != len(set(identifiers)):
        raise ValueError('Missing or duplicate Abyss identities')
    for row in abyss['trials']:
        if row.get('image'):
            relative = 'abyss/' + row['image']
            if relative not in manifest['files'] or digest(inside(directory, relative)) != row['sha256']:
                raise ValueError('Abyss image checksum mismatch')
    if not tables.get('item_index'):
        raise ValueError('Missing scoped item index')
    return manifest, tables


class GameAssets:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.directory = None
        self.tables = {}
        self.manifest = {}
        self.error = None
        pointer = self.root / 'current.json'
        if not pointer.exists():
            return
        try:
            value = json.loads(pointer.read_text(encoding='utf-8'))
            directory = inside(self.root, value['release'])
            if digest(directory / 'manifest.json') != value['manifest_sha256']:
                raise ValueError('Current manifest checksum mismatch')
            self.manifest, self.tables = validate_release(directory)
            self.directory = directory
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error = type(exc).__name__

    def section(self, name, default=None):
        return self.tables.get(name, default)


_shared = None


def current():
    global _shared
    if _shared is None:
        _shared = GameAssets()
        if _shared.error:
            raise RuntimeError('Published WWM assets failed validation; run Update WWM Assets.cmd --check')
    if _shared.error:
        raise RuntimeError('Published WWM assets failed validation')
    return _shared
