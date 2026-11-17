"""Recover inline emote frames from installed Cocos atlases, not icon IDs."""
import hashlib
from pathlib import Path
import plistlib
import re
import struct


def rectangle(value):
    match = re.fullmatch(r'\{\{(-?\d+),(-?\d+)\},\{(\d+),(\d+)\}\}', value or '')
    if not match: raise ValueError('Unsupported atlas rectangle')
    return tuple(map(int, match.groups()))


def pair(value):
    match = re.fullmatch(r'\{(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\}', value or '')
    if not match: raise ValueError('Unsupported atlas pair')
    return tuple(map(float, match.groups()))


def crop_frame(atlas, definition):
    from PIL import Image
    # Current inline emote frames are all unrotated. Do not guess how a future
    # exporter represents a rotated rectangle; stop that frame for review.
    if definition.get('rotated') is not False:
        raise ValueError('Rotated inline frame needs an explicitly verified decoder')
    x, y, w, h = rectangle(definition.get('frame'))
    left, top, trim_w, trim_h = rectangle(definition.get('sourceColorRect'))
    sw, sh = pair(definition.get('sourceSize')); ox, oy = pair(definition.get('offset'))
    if (not sw.is_integer() or not sh.is_integer() or not 0 < sw <= 2048 or not 0 < sh <= 2048
            or min(x, y, left, top) < 0 or min(w, h) <= 0 or (w, h) != (trim_w, trim_h)
            or x + w > atlas.width or y + h > atlas.height or left + w > sw or top + h > sh
            or abs(left - ((sw - w) / 2 + ox)) > 0.001 or abs(top - ((sh - h) / 2 - oy)) > 0.001):
        raise ValueError('Invalid or inconsistent atlas frame geometry')
    result = Image.new('RGBA', (int(sw), int(sh)), (0, 0, 0, 0))
    result.paste(atlas.crop((x, y, x + w, y + h)).convert('RGBA'), (left, top))
    return result


def extract_frames(game, root, inline, output, D):
    from named_mtable import installed_binding
    from installed_texture_records import selected_records
    from wwm import messiah_fileid, read_physical_record, decode_ezst
    from dungeon_library import export_art
    from utility.game_assets import digest
    from PIL import Image
    binding = installed_binding(root, 'all_png_data')
    frames = dict(D.U.frames(root / binding['archive']))
    schema = D.decode_name_list(frames[binding['frame']])
    records = []
    for frame in range(binding['frame'] + 1, binding['frame'] + 1 + binding['pages']):
        records.extend(D.U.frame_records(frames[frame]))
    if schema != ['all'] or len(records) != 1 or records[0][0] != 1:
        raise ValueError('Cocos atlas-list schema/identity changed')
    loc = records[0][1]; a, f, o, size = struct.unpack('<4H', loc)
    raw = D.U.load_frames(root, [loc])[(a, f)][o * 8:(o + size) * 8]
    reader = D.U.Reader(raw); values = D.U.read_row(reader, schema)
    names = values.get(0)
    if any(raw[reader.pos:]) or not isinstance(names, list) or len(names) != len(set(names)):
        raise ValueError('Invalid Cocos atlas list')
    if any(not isinstance(n, str) or Path(n).name != n or '\\' in n or not n.endswith('.plist') for n in names):
        raise ValueError('Unsafe Cocos atlas path')
    binding['index_sha256'] = digest(root / binding['archive'])
    paths = ['UIScript/plist/' + name for name in names]
    records, layers = selected_records(game, {f'{messiah_fileid(path.encode()):016X}' for path in paths})
    wanted = {f'ui_emotion_{identifier}_000{i:02d}.png' for identifier, row in inline.items() for i in range(row['frame_count'])}
    found = {}; atlases = {}; missing_plists = []
    for path in paths:
        record = records.get(f'{messiah_fileid(path.encode()):016X}')
        if record is None: missing_plists.append(path); continue
        data = read_physical_record(game, record)
        if data.startswith(b'EZST'): data = decode_ezst(data)['decoded']
        document = plistlib.loads(data)
        matches = wanted & document.get('frames', {}).keys()
        if not matches: continue
        metadata = document.get('metadata', {})
        texture = metadata.get('textureFileName')
        if metadata.get('format') != 2 or not isinstance(texture, str) or Path(texture).name != texture or not texture.endswith('.png'):
            raise ValueError('Unreviewed Cocos atlas metadata')
        resource = texture[:-4]
        atlases[path] = dict(plist_sha256=hashlib.sha256(data).hexdigest(), texture_resource=resource,
                            declared_size=metadata.get('size'), definitions=document['frames'])
        for name in matches:
            if name in found: raise ValueError('Ambiguous inline frame in multiple installed atlases: ' + name)
            found[name] = path
    art = export_art(game, output / 'atlas-art', {'inline': {'art': [{'resource': value['texture_resource']} for value in atlases.values()]}})
    images = {}; receipts = {}; errors = {}
    for path, info in atlases.items():
        hits = [art['assets'][u] for u in art['references'].get(info['texture_resource'], [])
                if art['assets'][u].get('status') == 'EXPORTED' and art['assets'][u]['n'] == info['texture_resource']]
        if len(hits) != 1: raise ValueError('Missing or ambiguous original inline atlas texture')
        with Image.open(output / 'atlas-art' / hits[0]['file']) as image:
            atlas = image.convert('RGBA')
        if pair(info['declared_size']) != atlas.size: raise ValueError('Inline atlas dimensions differ from metadata')
        receipts[path] = {k:v for k,v in info.items() if k != 'definitions'}
        receipts[path]['texture'] = hits[0]
        for name, source in found.items():
            if source != path: continue
            try:
                target = output / 'atlas-frames' / name; target.parent.mkdir(parents=True, exist_ok=True)
                crop_frame(atlas, info['definitions'][name]).save(target)
                images[name] = target
            except ValueError as exc:
                errors[name] = str(exc)
    return images, dict(table=binding, layers=layers, atlases=receipts,
        registry_sha256=art['registry_sha256'], physical_lookup=art.get('physical_lookup'),
        required_frames=len(wanted), exported_frames=len(images), errors=errors,
        unavailable_plists=missing_plists, missing_frames=sorted(wanted - images.keys()),
        selection='Current all_png_data atlas list -> exact frame filename -> plist rectangle -> exact original texture; no standalone-ID mapping.')
