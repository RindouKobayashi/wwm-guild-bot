"""Recover only self-contained, unmodified single-Spine chat effects."""
import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import patch


def headless_browser(browser, url, profile):
    if not browser: raise ValueError('A local Chromium browser is required for offline animation capture')
    return subprocess.Popen([str(browser), '--headless=new', '--no-first-run',
        '--disable-background-networking', '--disable-extensions', '--no-proxy-server',
        '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--force-device-scale-factor=1',
        '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
        f'--user-data-dir={profile}', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def prepare(toolkit, game, entries, work):
    from original_lua import read_original
    from lua_symbols import inspect
    from scan_game_avatars import fresh_registry
    from installed_texture_records import selected_records
    from wwm import messiah_fileid, read_physical_record, decode_ezst, uuid_physical_path, texture2d_physical_fileid, parse_puppet_asset
    from avatar_spine_export import decode_texture_rgba, fit_atlas
    from cocos_binary import parse, walk
    from PIL import Image
    cache, registry_sha, _ = fresh_registry(game)
    effects = {row['effect'] for row in entries.values() if row.get('effect')}
    puppets = {}
    with cache.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('t') == 'Puppet': puppets.setdefault(row['n'], []).append(row)
    paths = {name: 'UIScript/' + name + '.csb' for name in effects}
    selected, layers = selected_records(game, {f'{messiah_fileid(p.encode()):016X}' for p in paths.values()})
    jobs = []
    failures = {}
    def constants(tree):
        yield from tree['constants']
        for child in tree['children']: yield from constants(child)
    for effect, path in sorted(paths.items()):
        try:
            lua, receipt = read_original(toolkit, game, 'hexm/client/ui/generated_view/' + effect + '_view.lua')
            if path not in set(c for c in constants(inspect(lua)) if isinstance(c, str)):
                raise ValueError('Generated view does not reference expected layout')
            physical = selected[f'{messiah_fileid(path.encode()):016X}']
            raw = read_physical_record(game, physical)
            if raw.startswith(b'EZST'): raw = decode_ezst(raw)['decoded']
            document = parse(raw); nodes = list(walk(document['node']))
            spine = [node for node in nodes if node['class'] == 'SpineNode']
            if len(spine) != 1 or document.get('timelines') or any(n['class'] not in ('Widget', 'SpineNode') for n in nodes):
                raise ValueError('Effect has additional composition or timelines')
            for node in nodes:
                widget = node['widget']
                scale = widget.get('scale', [1, 1])
                if not widget.get('visible', True) or widget.get('alpha', 255) != 255 or widget.get('color_argb', [255]*4) != [255]*4 or scale[0] != scale[1] or scale[0] <= 0 or any(widget.get('rotation', [0, 0])):
                    raise ValueError('Effect has unsupported visibility, tint or transform')
            puppet = spine[0]['puppet_name']; candidates = puppets.get(puppet, [])
            if len(candidates) != 1 or len(candidates[0].get('d', [])) != 1:
                raise ValueError('Effect puppet or atlas dependency is ambiguous')
            definition = candidates[0]; dep = definition['d'][0]
            dep = f'{dep[:8]}-{dep[8:12]}-{dep[12:16]}-{dep[16:20]}-{dep[20:]}'
            puppet_fid = f'{messiah_fileid(uuid_physical_path(definition["u"]).encode()):016X}'
            texture_fid = texture2d_physical_fileid(dep).upper()
            records, source_layers = selected_records(game, {puppet_fid, texture_fid})
            puppet_raw = read_physical_record(game, records[puppet_fid]); texture_raw = read_physical_record(game, records[texture_fid])
            decoded = decode_ezst(puppet_raw)['decoded'] if puppet_raw.startswith(b'EZST') else puppet_raw
            asset = parse_puppet_asset(decoded, include_payloads=True)
            if not asset:
                raise ValueError('Chat Spine asset is unavailable')
            rgba, physical_size = decode_texture_rgba(texture_raw)
            size = (int(asset['atlas_width']), int(asset['atlas_height']))
            fitted, transform = fit_atlas(rgba, physical_size, size)
            folder = work / 'reconstructed' / effect; folder.mkdir(parents=True, exist_ok=True)
            page = asset['atlas_page']
            if Path(page).name != page: raise ValueError('Unsafe atlas page')
            Image.frombytes('RGBA', size, fitted).save(folder / page)
            (folder / (effect + '.atlas')).write_bytes(asset['atlas_raw'])
            (folder / (effect + '.skel')).write_bytes(asset['skel'])
            jobs.append(dict(effect=effect, folder=folder, animation='vx_loop', evidence=dict(
                lua=receipt, csb=path, csb_sha256=hashlib.sha256(raw).hexdigest(),
                puppet=puppet, puppet_uuid=definition['u'], registry_sha256=registry_sha,
                puppet_sha256=hashlib.sha256(puppet_raw).hexdigest(),
                texture_sha256=hashlib.sha256(texture_raw).hexdigest(), atlas_transform=transform,
                layers=source_layers, layout_layers=layers)))
        except Exception as exc:
            failures[effect] = str(exc)
    return jobs, failures


def render(toolkit, game, entries, output):
    """Source-derived loop, sampled at 30fps; no component-only approximations."""
    from wwm import export_spine_asset
    from PIL import Image
    from utility.emotes import inspect_image
    from utility.game_assets import digest
    from extract_emotes import make_gif
    jobs, failures = prepare(toolkit, game, entries, output / 'effects')
    for job in jobs:
        effect = job['effect']
        print('  emote animation ' + effect, flush=True)
        try:
            fingerprint = hashlib.sha256(json.dumps(dict(evidence=job['evidence'], converter=1,
                fps=30, max_size=384, animation='vx_loop'), sort_keys=True).encode()).hexdigest()
            cache = Path(__file__).resolve().parents[2] / '.asset-builds/emote-animation-cache' / fingerprint
            cached = None
            try:
                cached = json.loads((cache / 'receipt.json').read_text(encoding='utf-8'))
                if digest(cache / 'animation.gif') != cached['gif_sha256'] or digest(cache / 'preview.png') != cached['preview_sha256']:
                    cached = None
                if cached:
                    if cached['animation']['capture'].get('animation') != 'vx_loop': raise ValueError('Unverified cached animation')
                    inspect_image(cache / 'animation.gif'); inspect_image(cache / 'preview.png')
            except (OSError, ValueError, KeyError):
                cached = None
            if cached:
                for identifier, row in entries.items():
                    if row.get('effect') != effect: continue
                    import shutil
                    gif = output / 'art' / f'topic_{identifier}.gif'; still = output / 'art' / f'topic_{identifier}.png'
                    shutil.copy2(cache / 'animation.gif', gif); shutil.copy2(cache / 'preview.png', still)
                    row.update(image='art/' + gif.name, image_sha256=digest(gif), preview='art/' + still.name,
                        preview_sha256=digest(still), status='animated', animation=cached['animation'])
                    row.pop('reason', None); row.pop('animation_failure', None)
                continue
            folder = output / 'effects' / 'captures'; folder.mkdir(parents=True, exist_ok=True)
            apng = folder / (effect + '.png')
            with patch('wwm.launch_browser', headless_browser):
                result = export_spine_asset(job['folder'], apng, 'custom', 'vx_loop',
                    None, None, 4, 1, False, 'apng', 30, 384)
            result.pop('output', None)
            if result.get('animation') != 'vx_loop':
                raise ValueError('Parsed Spine runtime did not select the exact chat vx_loop')
            with Image.open(apng) as image:
                paths = []
                for frame in range(image.n_frames):
                    image.seek(frame)
                    path = folder / f'{effect}_{frame:04d}.png'
                    image.convert('RGBA').save(path); paths.append(path)
            for identifier, row in entries.items():
                if row.get('effect') != effect: continue
                target = output / 'art' / f'topic_{identifier}.gif'
                # GIF delays are centiseconds. Distribute 30/40ms frames so
                # total loop duration matches the source sample times.
                durations = [(round((i + 1) * 100 / 30) - round(i * 100 / 30)) * 10 for i in range(len(paths))]
                try:
                    make_gif(paths, target, duration=durations)
                except ValueError:
                    make_gif(paths, target, duration=durations, max_size=256)
                still = output / 'art' / f'topic_{identifier}.png'
                shutil_source = paths[0]
                with Image.open(shutil_source) as image: image.save(still)
                inspect_image(target)
                row.update(image='art/' + target.name, image_sha256=digest(target),
                    preview='art/' + still.name, preview_sha256=digest(still), status='animated',
                    animation=dict(job['evidence'], capture=result,
                        note='Original vx_loop, sampled at 30fps; GIF binary alpha. Single unmodified Spine layout only.'))
                row.pop('reason', None)
                row.pop('animation_failure', None)
                import shutil
                cache.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, cache / 'animation.gif'); shutil.copy2(still, cache / 'preview.png')
                (cache / 'receipt.json').write_text(json.dumps(dict(gif_sha256=digest(target),
                    preview_sha256=digest(still), animation=row['animation']), indent=2), encoding='utf-8')
        except Exception as exc:
            failures[effect] = str(exc)
    for row in entries.values():
        if row.get('effect') in failures: row['animation_failure'] = failures[row['effect']]
