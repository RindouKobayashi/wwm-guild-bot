"""Offline chat emote export, with separate topic and inline namespaces."""
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import shutil
import struct
import sys

BOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BOT))
from utility.game_assets import digest
from utility.emotes import validate, inspect_image, key


def named_rows(root, name, D):
    from named_mtable import installed_binding
    binding = installed_binding(root, name)
    frames = dict(D.U.frames(root / binding['archive']))
    schema = D.decode_name_list(frames[binding['frame']])
    if not schema: raise ValueError('Missing emote schema: ' + name)
    records = []
    counts = []
    for frame in range(binding['frame'] + 1, binding['frame'] + 1 + binding['pages']):
        raw = frames[frame]
        entries = D.U.frame_records(raw)
        # Actual Swiss-table occupied count, checked independently of the
        # MANIFEST's advisory row count (which differs in installed content).
        occupied = struct.unpack_from('<Q', raw, 8)[0]
        if not entries or len(entries) != occupied: raise ValueError('Invalid emote primary index')
        records.extend(entries); counts.append(occupied)
    if len({r[0] for r in records}) != len(records): raise ValueError('Duplicate emote index key')
    chunks = D.U.load_frames(root, [r[1] for r in records]); rows = {}
    for identifier, locator, *_ in records:
        a, f, o, size = struct.unpack('<4H', locator)
        raw = chunks[(a, f)][o * 8:(o + size) * 8]
        if len(raw) != size * 8: raise ValueError('Truncated emote row')
        reader = D.U.Reader(raw)
        row = {schema[i]: v for i, v in D.U.read_row(reader, schema).items()}
        if any(raw[reader.pos:]): raise ValueError('Unconsumed emote row bytes')
        identity = 'id' if name == 'chat_emotion_icon_lib' else 'group_no' if name == 'chat_emotion_group' else 'desc' if name == 'chat_emotion_frame_desc' else 'no'
        value = row.get(identity)
        if name == 'chat_emotion_frame_desc' and type(value) is int: value &= (1 << 64) - 1
        if value != identifier: raise ValueError('Emote primary-key namespace mismatch')
        rows[identifier] = row
    binding.update(decoded_primary_records=len(rows), occupied_counts=counts,
                   index_sha256=digest(root / binding['archive']),
                   count_note='Primary occupied slots and every row identity verified; MANIFEST record count is advisory, not a truncation limit.')
    return rows, schema, binding


def lua_evidence(toolkit, game):
    from original_lua import read_original
    from lua_symbols import inspect
    approved = json.loads((Path(__file__).with_name('emote_sources.json')).read_text(encoding='utf-8'))['sources']
    requirements = {
        'hexm/common/consts/chat_consts.lua': {'MSG_EMOTION', 'msg_emotion'},
        'hexm/client/ui/windows/chat/chat_content/msg_style_config.lua': {'MSG_EMOTION', 'pic_style'},
        'hexm/client/ui/windows/chat/chat_window_events.lua': {'chat_emotion_type_custom', 'chat_emotion_type_topic', '1', '2'},
        'hexm/client/ui/windows/chat/chat_content/msg_components/pic.lua': {'emotion_id', 'emotion_type', 'chat_emotion_icon_lib', 'emoji_file_name', 'vx_file_name', 'vx_loop'},
        'hexm/client/ui/controllers/richtext_component/richtext_config.lua': {'ui_emotion_%s_000%s%s.png'},
        'hexm/client/ui/controllers/richtext_component/richtext_base_component.lua': {'chat_emotion_frame_data', 'frame_num', 'tick_count', 0.2},
        'hexm/client/ui/manager/cocos.lua': {'all_png_data', 'UIScript/plist/%s', 'addSpriteFramesWithFiles'},
        'hexm/client/ui/windows/chat/chat_rich_text_field.lua': {'[#%s]', 'emotion'},
        'hexm/common/misc/chat_misc.lua': {'emotion_pattern_str', '%[#(%d+)%]', 'chat_emotion_frame_data', 'contains', 'emotion_id'},
        'hexm/client/ui/windows/chat/chat_content/msg_components/normal_msg.lua': {'msg', 'set_richtext_by_msg', 'parse_raw_str_with_emotion'},
    }
    result = []
    def walk(t):
        yield t
        for c in t['children']: yield from walk(c)
    for source, required in requirements.items():
        raw, receipt = read_original(toolkit, game, source)
        if receipt['sha256'] != approved.get(source):
            raise ValueError('Chat source logic changed; reverify routing before approving its checksum: ' + source)
        tree = inspect(raw)
        constants = {c for t in walk(tree) for c in t['constants'] if isinstance(c, (str, int, float))}
        if tree['source'] != '@' + source or not required <= constants:
            raise ValueError('Chat renderer changed; reverify emote routing: ' + source)
        receipt['required_constants'] = sorted(map(str, required)); result.append(receipt)
    return result


def identical_frames(paths):
    from PIL import Image
    signatures = set()
    for path in paths:
        with Image.open(path) as image:
            signatures.add((image.size, hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest()))
    return len(signatures) == 1


def make_gif(paths, destination, duration=200, max_size=None):
    """Only for the proven inline frame player (0.2 seconds/frame)."""
    from PIL import Image
    frames = []
    for path in paths:
        with Image.open(path) as image:
            rgba = image.convert('RGBA')
        if max_size:
            rgba.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        # Reserve palette index 255 for transparent pixels. GIF cannot retain
        # partial alpha; static PNG retains the source's original transparency.
        frame = rgba.convert('RGB').quantize(colors=255)
        frame.paste(255, mask=rgba.getchannel('A').point(lambda a: 255 if a < 128 else 0))
        frame.info['transparency'] = 255
        frames.append(frame)
    if len({f.size for f in frames}) != 1: raise ValueError('Inconsistent emote frame dimensions')
    frames[0].save(destination, save_all=True, append_images=frames[1:], duration=duration,
                   loop=0, transparency=255, disposal=2, optimize=False)
    if not inspect_image(destination):
        raise ValueError('Animation has no moving frames; retain a static preview')


def extract(toolkit, game, out):
    sys.path.insert(0, str(toolkit / 'app'))
    import update_trial_dungeons as D
    from dungeon_library import export_art
    root = game / 'Package/HD/oversea'
    evidence = lua_evidence(toolkit, game)
    topics, fields, topic_source = named_rows(root, 'chat_emotion_icon_lib', D)
    inline, inline_fields, inline_source = named_rows(root, 'chat_emotion_frame_data', D)
    descriptions, _, description_source = named_rows(root, 'chat_emotion_frame_desc', D)
    if len(descriptions) != len(inline) or any(inline.get(row['no']) != row for row in descriptions.values()):
        raise ValueError('Inline description secondary index differs from primary definitions')
    groups, group_fields, group_source = named_rows(root, 'chat_emotion_group', D)
    # An additional schema with the same identity fields can represent an
    # overseas override. Refuse it until its merge semantics are reviewed.
    from table_discovery import catalogue
    schemas = catalogue(root, toolkit / 'cache/table_schema_catalogue.json')['tables']
    known = {(s['archive'], s['frame']) for s in (topic_source, inline_source, group_source, description_source)}
    signatures = ({'emoji_name', 'emoji_file_name', 'id'}, {'frame_num', 'show_frame', 'no'}, {'group_no', 'emoji_file_name', 'name'})
    for schema in schemas:
        if any(signature <= set(schema['fields']) for signature in signatures) and (schema['archive'], schema['frame']) not in known:
            raise ValueError('Unreviewed additional emote schema/override')
    if not {'id', 'emoji_file_name', 'emoji_group', 'emoji_name', 'vx_file_name'} <= set(fields):
        raise ValueError('Chat icon library schema changed')
    if not {'no', 'frame_num', 'show_frame', 'desc'} <= set(inline_fields):
        raise ValueError('Chat inline frame schema changed')
    locale = D.all_english_strings(root)
    def localized(value):
        return locale.get(value) if type(value) is int else value if isinstance(value, str) else None
    entries = {}
    inline_entries = {}
    for identifier, row in sorted(topics.items()):
        if not key(identifier): raise ValueError('Invalid topic emote ID')
        resource = row.get('emoji_file_name')
        if not isinstance(resource, str) or not resource: raise ValueError('Missing emote resource reference')
        entries[str(identifier)] = dict(id=identifier, source_table='chat_emotion_icon_lib',
            name=localized(row.get('emoji_name')), group_id=row.get('emoji_group'),
            group_name=localized(groups.get(row.get('emoji_group'), {}).get('name')),
            resource=resource + '_0_0_ui', effect=row.get('vx_file_name'), status='unresolved',
            reason='Artwork not exported', declared_type=row.get('emoji_type'))
    for identifier, row in sorted(inline.items()):
        count = row.get('frame_num', 1); preview = row.get('show_frame', 0)
        if not key(identifier) or type(count) is not int or not 1 <= count <= 120 or type(preview) is not int or not 0 <= preview < count:
            raise ValueError('Invalid inline emote frames')
        inline_entries[str(identifier)] = dict(id=identifier, source_table='chat_emotion_frame_data',
            name=localized(row.get('desc')), group_id=row.get('group'), status='unresolved',
            frame_count=count, show_frame=preview, frame_duration_ms=200,
            resources=[f'ui_emotion_{identifier}_000{i:02d}.png' for i in range(count)],
            reason='Inline-only namespace; not used for standalone msg_emotion')
    resources = {r['resource'] for r in entries.values()}
    out.mkdir(parents=True, exist_ok=True)
    art = export_art(game, out / 'source-art', {'emotes': {'art': [{'resource': s} for s in sorted(resources)]}})
    (out / 'source-art-manifest.json').write_text(json.dumps(art, indent=2), encoding='utf-8')
    def original(resource):
        hits = [art['assets'][u] for u in art['references'].get(resource, [])
                if art['assets'][u].get('status') == 'EXPORTED' and art['assets'][u]['n'] == resource]
        if len(hits) > 1: raise ValueError('Ambiguous emote artwork: ' + resource)
        return out / 'source-art' / hits[0]['file'] if hits else None
    (out / 'art').mkdir(exist_ok=True)
    from emote_atlas import extract_frames
    inline_images, atlas_source = extract_frames(game, root, inline_entries, out, D)
    for namespace, rows in (('topic', entries), ('inline', inline_entries)):
        for identifier, row in rows.items():
            paths = ([original(row['resource'])] if namespace == 'topic' else
                     [inline_images.get(f'ui_emotion_{identifier}_000{i:02d}.png') for i in range(row['frame_count'])])
            if namespace == 'inline':
                row['art_source'] = 'Cocos sprite atlas'
            preview = paths[0] if namespace == 'topic' else paths[row['show_frame']]
            if preview:
                target = out / 'art' / f'{namespace}_{identifier}.png'
                shutil.copy2(preview, target); inspect_image(target)
                row.update(image='art/' + target.name, image_sha256=digest(target),
                           preview='art/' + target.name, preview_sha256=digest(target), status='static')
                if namespace == 'topic' and row.get('effect'):
                    row.update(status='static_preview', reason='Original UI-effect animation requires verified layout/effect rendering; showing its own emoji_file_name artwork.')
                else: row.pop('reason', None)
            if namespace == 'inline' and len(paths) > 1 and all(paths):
                gif = out / 'art' / f'inline_{identifier}.gif'
                try:
                    if identical_frames(paths):
                        row.update(status='static', reason='All source frames are identical; original still image preserved.')
                        continue
                    make_gif(paths, gif)
                    row.update(image='art/' + gif.name, image_sha256=digest(gif), status='animated',
                               reason='Inline-only animation; not used for standalone msg_emotion. GIF uses binary alpha; PNG preview preserves source alpha.')
                except (ValueError, OSError) as exc:
                    row.update(status='static_preview' if preview else 'unresolved', reason=str(exc))
            elif namespace == 'inline' and not all(paths):
                row['missing_frames'] = [i for i, p in enumerate(paths) if p is None]
                row['reason'] = 'Missing inline animation frames; no incomplete animation assembled.'
    from emote_animation import render as render_effects
    render_effects(toolkit, game, entries, out)
    manual = []
    for path in sorted((BOT / 'data/emotion').glob('*')):
        if not path.is_file() or not key(path.stem): continue
        entry = entries.get(key(path.stem))
        manual.append(dict(id=key(path.stem), filename=path.name, sha256=digest(path),
                           extracted_status=entry['status'] if entry else 'unknown',
                           match=bool(entry and entry.get('preview_sha256') == digest(path))))
    data = dict(format=1, entries=entries, inline_entries=inline_entries, sources=dict(
        topics=topic_source, inline=inline_source, description_index=description_source, groups=group_source, lua=evidence,
        registry_sha256=art['registry_sha256'], physical_lookup=art.get('physical_lookup'), inline_atlas=atlas_source,
        routing='msg_emotion -> pic_style -> emotion_type topic (2) -> chat_emotion_icon_lib. Received inline [#N] -> chat_emotion_frame_data[N] directly. Outgoing input [#position] + ext.emotion[position] is a separate format; never cross-resolve namespaces.',
        game_version=None, version_note='Content identified by source checksums; no authoritative semantic game version supplied.'),
        local_comparison=manual)
    validate(data)
    (out / 'catalogue.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    render_preview(data, out)
    return data


def render_preview(data, out):
    esc = lambda s: html.escape(str(s or ''), quote=True)
    cards = []
    for title, rows in (('Standalone live-chat emotes', data['entries']), ('Inline text — Discord application emojis', data.get('inline_entries', {}))):
        cards.append('<h2>' + esc(title) + '</h2><main>')
        previous_group = object()
        for identifier, r in sorted(rows.items(), key=lambda pair: (pair[1].get('group_id') or 0, int(pair[0]))):
            if r.get('group_id') != previous_group:
                previous_group = r.get('group_id')
                cards.append('<h3 style="grid-column:1/-1">' + esc(r.get('group_name') or 'Collection ' + str(previous_group)) + '</h3>')
            image = '<img loading="lazy" src="' + esc(r['image']) + '" alt="' + esc(r.get('name') or identifier) + '">' if r.get('image') else '<p>Artwork unavailable</p>'
            still = '<a href="' + esc(r['preview']) + '">Static PNG</a>' if r.get('preview') else ''
            cards.append('<article><h3>' + esc(r.get('name') or 'Unnamed emote') + '</h3>' + image + '<p>ID ' + identifier + ' · ' + esc(r.get('group_name') or r.get('group_id')) + '</p><strong>' + esc(r['status']) + '</strong><p>' + esc(r.get('reason')) + '</p><small>' + esc(r.get('resource') or r.get('resources')) + '</small><p>' + still + '</p></article>')
        cards.append('</main>')
    counts = Counter(r['status'] for r in data['entries'].values())
    inline_counts = Counter(r['status'] for r in data.get('inline_entries', {}).values())
    page = '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>WWM emote review</title><style>body{background:#15151d;color:#eee;font:16px system-ui;padding:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}article{background:#252532;padding:18px;border-radius:14px;overflow-wrap:anywhere}img{width:160px;height:160px;object-fit:contain;background:repeating-conic-gradient(#34343d 0% 25%,#292932 0% 50%) 0/20px 20px}a{color:#8ee8ee}input{padding:12px;width:min(90%,550px)}small{font-size:12px}</style><h1>Game-derived emotes</h1><p><a href="upload-pack/contact-sheet.html">Discord inline emoji upload pack</a></p><p>' + 'Standalone: ' + esc(dict(counts)) + '</p><p>Inline: ' + esc(dict(inline_counts)) + '</p><p>Static previews do not claim animation fidelity. Inline IDs never resolve standalone messages.</p><label>Find by ID, name, collection or status <input type=search id=q></label>' + ''.join(cards) + '<h2>Existing manual files</h2><pre>' + esc(json.dumps(data.get('local_comparison', []), indent=2)) + '</pre><script>document.getElementById("q").addEventListener("input",e=>document.querySelectorAll("article").forEach(a=>a.hidden=!a.textContent.toLowerCase().includes(e.target.value.toLowerCase())))</script>'
    (out / 'contact-sheet.html').write_text(page, encoding='utf-8')


def copy_export(source, target):
    from utility.game_assets import inside
    data = json.loads((source / 'catalogue.json').read_text(encoding='utf-8')); validate(data)
    (target / 'art').mkdir(parents=True, exist_ok=True)
    for namespace in ('entries', 'inline_entries'):
        for row in data.get(namespace, {}).values():
            for field in ('image', 'preview'):
                if not row.get(field): continue
                path = inside(source, row[field])
                if digest(path) != row[field + '_sha256']: raise ValueError('Emote image changed during export')
                inspect_image(path); shutil.copy2(path, inside(target, row[field]))
    for name in ('catalogue.json', 'contact-sheet.html'):
        shutil.copy2(source / name, target / name)
    if any(row.get('image') for row in data.get('inline_entries', {}).values()):
        from inline_emoji_pack import build
        applications=json.loads((BOT / 'assets/discord/inline-applications.json').read_text(encoding='utf-8'))
        build(target, data, applications)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--toolkit', type=Path, default=BOT.parent / 'WWM-Toolkit')
    p.add_argument('--game-root', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = extract(args.toolkit, args.game_root, args.output)
    print('Standalone emotes:', dict(Counter(r['status'] for r in result['entries'].values())), flush=True)
