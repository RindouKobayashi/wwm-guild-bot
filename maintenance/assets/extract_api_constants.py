"""Extract verified API display mappings from installed MTable data, offline."""
import argparse
import json
from pathlib import Path
import re
import struct
import sys

HERE = Path(__file__).resolve().parent
BOT = HERE.parents[1]


def converted(sect_rows, status_rows, like_rows):
    """Keep sect statuses and server like-topic identities in their own namespaces."""
    names, ranks, votes, statuses, topics, groups = {}, {}, {}, {}, {}, {}
    for key, row in sect_rows.items():
        if str(row['no']) != key or not row.get('sect_name'):
            raise ValueError('Invalid sect identity/name')
        names[key] = row['sect_name']
    for key, row in status_rows.items():
        if key != f"{row['school']}_{row['identify']}" or not row.get('name_school_identity'):
            raise ValueError('Sect status key disagrees with school/identify')
        ranks[key] = row['name_school_identity']
        statuses[key] = {field: row[field] for field in ('id', 'school', 'identify', 'name_school_identity',
                         'name_school_identity_show', 'vote_time') if field in row}
        if 'vote_time' in row:
            count = row['vote_time']
            if type(count) is not int or count < 0:
                raise ValueError('Invalid sect vote allowance')
            votes[key] = count
        # The sectless name is supplied by its own status row, not invented.
        names.setdefault(str(row['school']), row['name_school_identity'] if row['school'] == 100 else '')
    if any(not name for name in names.values()):
        raise ValueError('Status refers to an unnamed sect')
    for key, row in like_rows.items():
        label, server_ids = row.get('upvote_text'), row.get('server_id')
        if not isinstance(label, str) or not label or not isinstance(server_ids, list) or not server_ids:
            raise ValueError('Missing likes label/server topic IDs')
        groups[key] = dict(row)
        for topic in server_ids:
            if type(topic) is not int or topic <= 0 or str(topic) in topics:
                raise ValueError('Duplicate or invalid server like-topic identity')
            topics[str(topic)] = label
    if not all((names, ranks, votes, topics)):
        raise ValueError('Empty API mapping extraction')
    return {'school_names': names, 'school_ranks': ranks, 'vote_counts': votes, 'likes': topics,
            'school_statuses': statuses, 'like_groups': groups}


def extract(toolkit, game):
    sys.path.insert(0, str(toolkit / 'app'))
    import update_trial_dungeons as D
    from table_discovery import catalogue, resolve
    from resolve_schema_fields import inline_name_records
    references = json.loads((HERE / 'api_constant_references.json').read_text(encoding='utf-8'))
    catalogue_data = catalogue(game, toolkit / 'cache/table_schema_catalogue.json')
    locale = D.all_english_strings(game)
    output, evidence = {}, {}
    for kind, reference in references.items():
        table = resolve(catalogue_data, reference)
        archive, frame = table['archive'], table['frame']
        frames = dict(D.frames(game / archive))
        schema = D.decode_name_list(frames[frame])
        if reference['key'] not in schema:
            raise ValueError('Missing primary key')
        entries = []
        if kind == 'statuses':
            for page in table['shard_frames']:
                strings = inline_name_records(frames[page])
                if strings is None:
                    raise ValueError('Status index is not a verified string-key table')
                entries.extend((name, locator) for locator, name in strings)
        else:
            entries = [(str(key), locator) for key, locator, *_ in D.U.shard_records(frames, frame)]
        data = D.U.load_frames(game, [locator for _, locator in entries])
        rows = {}
        for key, locator in entries:
            a, f, offset, size = struct.unpack('<HHHH', locator)
            payload = data[(a, f)][offset * 8:(offset + size) * 8]
            reader = D.U.Reader(payload)
            row = D.U.read_row(reader, schema)
            if not row or max(row) >= len(schema) or any(payload[reader.pos:]):
                raise ValueError('Malformed or unbounded API mapping row')
            decoded = {schema[i]: value for i, value in row.items()}
            if kind != 'statuses' and str(decoded.get(reference['key'])) != key:
                raise ValueError('API mapping primary key mismatch')
            if key in rows:
                raise ValueError('Duplicate API mapping key')
            if kind == 'statuses' and not re.fullmatch(r'\d+_\d+', key):
                raise ValueError('Invalid sect status key')
            for field in reference['localized']:
                if field in decoded:
                    value = decoded[field]
                    decoded[field] = locale.get(value) if type(value) is int else value
                    if not isinstance(decoded[field], str) or not decoded[field]:
                        raise ValueError('Unresolved English mapping: ' + field)
            rows[key] = decoded
        if len(rows) != table['records']:
            raise ValueError('API table coverage mismatch')
        output[kind] = rows
        evidence[kind] = {'archive': archive, 'frame': frame, 'fields': schema, 'records': len(rows),
                          'key': reference['key'], 'identity': reference['identity']}
    result = converted(output['sects'], output['statuses'], output['likes'])
    result['boss_names'] = {key: row['name'] for key, row in output['bosses'].items()}
    result['sources'] = evidence
    sys.path.insert(0, str(HERE))
    from extract_like_topics import extract as extract_topics
    result['like_topic_symbols'], result['sources']['like_topic_symbols'] = extract_topics(toolkit, game.parents[2])
    result['limitations'] = ['UI like groups do not include every server topic. Legacy Market/Kitty/Pet labels remain compatibility fallbacks.',
                             'Shipped sect definitions do not prove that a sect is currently available to players.',
                             'Vote allowance is configuration, not remaining votes or proof that an election is open.']
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolkit', type=Path, default=BOT.parent / 'WWM-Toolkit')
    parser.add_argument('--game-root', type=Path, required=True, help='Package/HD/oversea directory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = extract(args.toolkit.resolve(), args.game_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: len(result[key]) for key in ('school_names', 'school_ranks', 'vote_counts', 'likes', 'boss_names')}))
