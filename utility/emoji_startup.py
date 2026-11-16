"""Bounded startup sync of the exact icon manifest approved in the asset release."""
import asyncio
import base64
import json
from pathlib import Path
import time
from utility.game_assets import GameAssets, digest
from utility.emoji_upload import API, icons, load, save, preview, RejectedUpload

BOT=Path(__file__).resolve().parents[1]


def approval(directory, environment):
    from utility.martial_data import validate_approval
    value=validate_approval(directory)
    app=value.get('applications',{}).get(environment)
    if not isinstance(app,str) or not app.isdigit():raise ValueError('Application is not approved for this environment')
    return app


async def sync(directory, environment, token, root=BOT, api=None):
    from utility.martial import read_registry
    app=approval(directory,environment);pack=directory/'upload-pack';rows=icons(pack)
    local=root/'data/martial-emojis.json';pending_file=root/'data/martial-emoji-upload-pending.json'
    registry=read_registry(root/'assets/discord/martial-emojis.json',local);pending=load(pending_file,{})
    identity=await api.call('GET','/oauth2/applications/@me')
    if str(identity.get('id'))!=app:raise ValueError('Discord application does not match the approved environment')
    previous=registry['environments'].get(environment)
    if previous and previous!=app:raise ValueError('Local environment is bound to another application')
    plan=await preview(api,app,rows,pack,registry,pending)
    receipts=registry['applications'].setdefault(app,{})
    registry['environments'][environment]=app
    created=0
    for row in plan:
        path=pack/row['file']
        if digest(path)!=row['sha256']:raise ValueError('Icon changed during startup sync')
        key=app+':'+row['key']
        if row['state']=='create':
            pending[key]=dict(name=row['name'],sha256=row['sha256']);save(pending_file,pending)
            # No automatic retries after an uncertain POST. Reconcile next launch.
            try:
                result=await api.call('POST',f'/applications/{app}/emojis',dict(name=row['name'],image='data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()))
            except RejectedUpload:
                pending.pop(key,None);save(pending_file,pending)
                raise
            row['id']=str(result['id']);created+=1
        receipts[row['key']]={k:row[k] for k in ('id','name','sha256')}
        save(local,registry);pending.pop(key,None);save(pending_file,pending)
    return dict(state='complete',application_id=app,created=created,reused=len(plan)-created,icons=len(plan))


async def startup(environment,token,root=BOT,timeout=45):
    """Failure changes decoration, never whether the bot is allowed to start."""
    lock=None
    try:
        assets=GameAssets(root/'assets/wwm')
        if assets.error:raise ValueError('Asset release failed validation')
        if assets.directory is None or not assets.section('martial'):
            result=dict(state='not_configured',message='No reviewed martial release installed')
        else:
            from control_panel.locks import FileLock
            import aiohttp
            lock=FileLock(root/'data/martial-emoji-upload.lock')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),trust_env=False) as session:
                result=await asyncio.wait_for(sync(assets.directory/'martial',environment,token,root,API(session,token)),timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Do not persist HTTP bodies, URLs, tokens or arbitrary exception messages.
        result=dict(state='incomplete',message='Icon sync incomplete; names remain available. Check credentials, approved application and pending uploads.',error=type(exc).__name__)
    finally:
        if lock:lock.close()
    result.update(environment=environment,checked_at=time.time())
    try:save(root/'data/martial-emoji-sync-status.json',result)
    except OSError:pass
    from utility.martial import reload_registry
    reload_registry()
    return result
