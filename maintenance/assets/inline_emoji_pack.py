"""Build reviewed 128px application emojis from the inline atlas exports."""
import html
import json
from pathlib import Path


def build(directory, data, applications):
    from PIL import Image, ImageOps
    from utility.game_assets import digest
    from utility.emoji_upload import icons
    from utility.emotes import validate
    validate(data)
    pack = directory / 'upload-pack'; (pack / 'icons').mkdir(parents=True, exist_ok=True)
    rows = []
    for identifier, source in data.get('inline_entries', {}).items():
        if not source.get('image'): continue
        animated = source['status'] == 'animated'
        path = directory / source['image']; suffix = '.gif' if animated else '.png'
        target = pack / 'icons' / ('inline_' + identifier + suffix)
        with Image.open(path) as image:
            frames = []; durations = []; loop = image.info.get('loop', 0)
            for i in range(getattr(image, 'n_frames', 1)):
                image.seek(i); image.load()
                frame = ImageOps.pad(image.convert('RGBA'), (128,128), method=Image.Resampling.NEAREST, color=(0,0,0,0))
                if animated:
                    alpha = frame.getchannel('A')
                    frame = frame.convert('RGB').quantize(colors=255)
                    frame.paste(255, mask=alpha.point(lambda a: 255 if a < 128 else 0))
                    frame.info['transparency'] = 255
                    durations.append(int(image.info.get('duration', 200)))
                frames.append(frame)
        if animated:
            frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations,
                           loop=loop, transparency=255, disposal=2, optimize=False)
        else: frames[0].save(target)
        sha = digest(target)
        rows.append(dict(key='inline_' + identifier, source_id=int(identifier),
            source_sha256=source['image_sha256'], name=f'wwm_i_{identifier}_{sha[:8]}',
            file='icons/' + target.name, sha256=sha, animated=animated, label=source.get('name')))
    (pack / 'upload-manifest.json').write_text(json.dumps(dict(format=1, icons=rows), ensure_ascii=False, indent=2), encoding='utf-8')
    icons(pack)
    approval = dict(format=1, enabled=True, applications=applications,
        catalogue_sha256=digest(directory / 'catalogue.json'), upload_manifest_sha256=digest(pack / 'upload-manifest.json'))
    (directory / 'startup-approval.json').write_text(json.dumps(approval, indent=2), encoding='utf-8')
    cards = ''.join('<article><img width=64 height=64 src="' + html.escape(r['file'], quote=True) + '"><p>' +
        html.escape(str(r['source_id']) + ' · ' + str(r['label'])) + '</p><code>' + r['name'] + '</code><p>' +
        ('Animated GIF' if r['animated'] else 'Still PNG') + '</p></article>' for r in rows)
    (pack / 'contact-sheet.html').write_text('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Inline emoji uploads</title><style>body{background:#181821;color:white;font:16px system-ui;padding:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px}article{padding:18px;background:#292936;border-radius:12px}code{font-size:12px}</style><h1>Inline emoji upload pack</h1><p>' + str(len(rows)) + ' inline emotes, separate from standalone stickers. The bot uploads missing reviewed files at startup; no existing emojis are deleted.</p><main>' + cards + '</main>', encoding='utf-8')
    (pack / 'UPLOAD-GUIDE.md').write_text('''# Inline live-chat emoji uploads

These files are application emojis owned by the bot, not guild/server emojis.
Animated GIFs stay animated; the heart is a still PNG. Every upload is 128×128
and at most 256 KiB. The contact sheet shows the exact files and versioned names.

After reviewing and committing this pack, pull on the Pi and start normally.
Startup verifies the authenticated application against the environment ID below,
uploads missing files and saves local receipts under `data/inline-emojis.json`.
Test and production receipts remain separate. A bounded startup sync can resume
on the next start; successfully uploaded icons become usable immediately at login.
Missing uploads show readable names rather than breaking chat. No emoji is deleted.

The message marker `[#4]` means position 4, not emote ID 4. The renderer obtains
the real ID from `ext.emotion[4]` and uses only matching application receipts.
Unknown markers without metadata remain text. Standalone stickers are unchanged.

For a read-only preview run:
`python maintenance/assets/upload_inline_emojis.py --environment test`
Add `--upload` for an interactive confirmation, or upload these files manually
in the bot application's Developer Portal and run `--import-existing`.
Uncertain upload outcomes are reconciled before another create is attempted.

Approved applications:
''' + '\n'.join('- ' + env + ': `' + app + '`' for env,app in applications.items()) +
        '\n\nReference: https://docs.discord.com/developers/resources/emoji\n', encoding='utf-8')
