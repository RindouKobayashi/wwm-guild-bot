"""Preview application emoji changes; creates only after explicit confirmation."""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time

BOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BOT))
from utility.game_assets import inside,digest

REGISTRY=BOT/'data/martial-emojis.json'


from utility.emoji_upload import load, save, icons, same_image, API, preview, RejectedUpload


async def run(args):
    import aiohttp
    from dotenv import dotenv_values
    rows=icons(args.pack)
    if args.only:
        rows=[r for r in rows if r['key']==args.only]
        if not rows:raise ValueError('Icon key is not in the manifest')
    env={**dotenv_values(BOT/'.env'),**os.environ}
    token=env.get('DISCORD_API_TOKEN_DEV' if args.environment=='test' else 'DISCORD_API_TOKEN')
    if not token:raise ValueError('Missing host Discord credentials for '+args.environment)
    from utility.martial import read_registry
    registry=read_registry(BOT/'assets/discord/martial-emojis.json', REGISTRY)
    intent=BOT/'data/martial-emoji-upload-pending.json';pending=load(intent,{})
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),trust_env=False) as session:
        api=API(session,token);identity=await api.call('GET','/oauth2/applications/@me');app=str(identity['id'])
        print(f"Target: {args.environment} · {identity['name']} · application {app}")
        previous=registry.get('environments',{}).get(args.environment)
        if previous and previous!=app:raise ValueError('Environment application identity differs from saved registry')
        plan=await preview(api,app,rows,args.pack,registry,pending)
        for row in plan:print(row['state'].upper(),row['name'],row['file'],row['sha256'])
        if not args.upload and not args.import_existing:print('Preview only. No uploads or registry writes.');return
        if args.import_existing and any(r['state']=='create' for r in plan):raise ValueError('Upload missing files manually before importing; no changes saved')
        expected=app if args.environment=='production' else 'UPLOAD TEST'
        if input('Confirm application emoji changes by typing '+expected+': ').strip()!=expected:print('Cancelled; no changes.');return
        # Revalidate after confirmation; files must not change while awaiting input.
        if [r for r in icons(args.pack) if not args.only or r['key']==args.only]!=rows:raise ValueError('Upload pack changed during confirmation')
        receipts=registry.setdefault('applications',{}).setdefault(app,{})
        registry.setdefault('environments',{})[args.environment]=app
        for row in plan:
            key=app+':'+row['key']
            if row['state']=='create':
                pending[key]=dict(name=row['name'],sha256=row['sha256']);save(intent,pending)
                try:
                    value=await api.call('POST',f'/applications/{app}/emojis',dict(name=row['name'],image='data:image/png;base64,'+base64.b64encode(inside(args.pack,row['file']).read_bytes()).decode()))
                except RejectedUpload:
                    pending.pop(key,None);save(intent,pending)
                    raise
                row['id']=str(value['id'])
            receipts[row['key']]={k:row[k] for k in ('id','name','sha256')};save(REGISTRY,registry)
            pending.pop(key,None);save(intent,pending)
        print('Saved public registry:',REGISTRY,'· restart consumers after deployment')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--environment',choices=('test','production'),required=True);parser.add_argument('--pack',type=Path,required=True);parser.add_argument('--only',help='Limit the reviewed operation to one manifest icon key')
    group=parser.add_mutually_exclusive_group();group.add_argument('--upload',action='store_true');group.add_argument('--import-existing',action='store_true');args=parser.parse_args()
    from control_panel.locks import FileLock
    lock=BOT/'data/martial-emoji-upload.lock';handle=None
    try:
        if args.upload or args.import_existing:
            handle=FileLock(lock)
        asyncio.run(run(args))
    except KeyboardInterrupt:raise SystemExit('Cancelled; inspect any pending upload receipt before retrying.') from None
    except Exception as exc:raise SystemExit(str(exc)) from None
    finally:
        if handle is not None:handle.close()
