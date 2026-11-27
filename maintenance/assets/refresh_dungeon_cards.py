"""Refresh original dungeon activity-card bindings from installed game files."""
import argparse
import json
from pathlib import Path
import re
import sys

BOT = Path(__file__).resolve().parents[2]
GAME = Path(r'C:\Program Files (x86)\Steam\steamapps\common\Where Winds Meet')
RANK_NAME = re.compile(r'rank_team(?:10)?_dungeon_\d+')
TABLES = ('social_game_config', 'social_game_data', 'rank_social_game_data')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-root', type=Path, default=GAME)
    parser.add_argument('--toolkit', type=Path, default=BOT.parent / 'WWM-Toolkit')
    parser.add_argument('--output', type=Path, default=BOT / 'maintenance/research/output/dungeon_cards')
    args = parser.parse_args()
    sys.path.insert(0, str(args.toolkit.resolve() / 'app'))
    from dungeon_library import export_art
    from named_mtable import installed_table_roots, read_primary_rows
    from update_trial_dungeons import all_english_strings

    base, manifest_root = installed_table_roots(args.game_root)
    locale = all_english_strings(base)
    rows = []
    sources = {}
    for name in TABLES:
        proof, records = read_primary_rows(base, manifest_root, name, supplemental=True,
                                           append_linked_schema=True)
        sources[name] = proof
        for record in records:
            raw = record['fields']
            rank = raw.get('rank_id')
            resource = raw.get('game_list_image')
            if not isinstance(rank, str) or not RANK_NAME.fullmatch(rank):
                continue
            if not isinstance(resource, str) or not resource.startswith('wulinlu_tongyou_pic_xj_'):
                continue
            if name != 'rank_social_game_data' and record['key'] not in (
                raw.get('mate_info_id'), raw.get('team_game_id'), raw.get('guild_team_id')
            ):
                continue
            title = raw.get('game')
            if isinstance(title, int):
                title = locale.get(title)
            if not isinstance(title, str) or not title:
                continue
            source = record['source']
            page = source.get('index_page', source['schema_frame'])
            rows.append({'source': f"{source['index_layer']}:{source['archive']}:{page}",
                         'key': record['key'], 'locator': source['locator'], 'name': title, 'raw': raw})
    if not rows:
        raise RuntimeError('No validated dungeon activity-card bindings found')
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    reviews = json.loads((BOT / 'maintenance/assets/dungeon_card_reviews.json').read_text(encoding='utf-8'))['reviews']
    resources = {row['raw']['game_list_image'] for row in rows} | {row['resource'] for row in reviews}
    groups = {'cards': {'art': [{'resource': resource} for resource in sorted(resources)]}}
    manifest = export_art(args.game_root, out, groups)
    (out / 'art_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (out / 'bindings.json').write_text(json.dumps({'sources': sources, 'rows': rows},
                                                  ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Refreshed {len(rows)} dungeon activity-card records', flush=True)


if __name__ == '__main__':
    main()
