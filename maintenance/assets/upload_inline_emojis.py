"""Read-only inline emoji preview; upload/import requires explicit confirmation."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

BOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BOT))


async def run(args):
    import aiohttp
    from dotenv import dotenv_values
    from utility.game_assets import GameAssets, digest
    from utility.inline_emojis import validate_pack
    from utility.emoji_startup import approval, sync
    from utility.emoji_upload import API, load, save, preview
    from utility.martial import read_registry
    assets = GameAssets()
    if assets.directory is None: raise ValueError('No validated release installed')
    directory = assets.directory / 'emotes'; rows = validate_pack(directory)
    app = approval(directory, args.environment)
    env = {**dotenv_values(BOT / '.env'), **os.environ}
    token = env.get('DISCORD_API_TOKEN_DEV' if args.environment == 'test' else 'DISCORD_API_TOKEN')
    if not token: raise ValueError('Missing host credentials for selected environment')
    registry = read_registry(BOT / 'assets/discord/inline-emojis.json', BOT / 'data/inline-emojis.json')
    pending_file = BOT / 'data/inline-emoji-upload-pending.json'; pending = load(pending_file, {})
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
        api = API(session, token); identity = await api.call('GET', '/oauth2/applications/@me')
        if str(identity.get('id')) != app: raise ValueError('Authenticated application does not match the approved environment')
        print('Target:', args.environment, identity['name'], app)
        plan = await preview(api, app, rows, directory / 'upload-pack', registry, pending)
        for row in plan: print(row['state'].upper(), row['name'], row['file'])
        if not args.upload and not args.import_existing:
            print('Preview only; no uploads or local receipts written.'); return
        if args.import_existing and any(row['state']=='create' for row in plan):
            raise ValueError('Some files are missing or unverified; no import saved')
        expected = app if args.environment == 'production' else 'UPLOAD TEST'
        if input('Confirm by typing ' + expected + ': ').strip() != expected:
            print('Cancelled; no changes.'); return
        # sync repeats validation/reconciliation after confirmation, under the lock.
        result = await sync(directory, args.environment, token, BOT, api, kind='inline', allow_create=not args.import_existing)
        print('Inline emoji sync:', result['state'], 'created', result['created'], 'reused', result['reused'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--environment', required=True, choices=('test','production'))
    group = p.add_mutually_exclusive_group(); group.add_argument('--upload', action='store_true'); group.add_argument('--import-existing', action='store_true')
    args = p.parse_args(); handle = None
    try:
        if args.upload or args.import_existing:
            from control_panel.locks import FileLock
            handle = FileLock(BOT / 'data/inline-emoji-upload.lock')
        asyncio.run(run(args))
    except KeyboardInterrupt: raise SystemExit('Cancelled; reconcile pending uploads before retrying.') from None
    except Exception as exc: raise SystemExit(type(exc).__name__ + ': inline emoji maintenance failed; check the approved application, pack and pending receipts.') from None
    finally:
        if handle: handle.close()
