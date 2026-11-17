"""Shared application-emoji validation and reconciliation; no gateway session."""
import asyncio
import base64
import io
import json
from pathlib import Path
import time
from utility.game_assets import inside, digest

def load(path,default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8');temp.replace(path)


def icons(pack):
    from PIL import Image
    data=load(pack/'upload-manifest.json',{})
    if data.get('format')!=1 or not data.get('icons'):raise ValueError('Invalid upload manifest')
    seen=set()
    for row in data['icons']:
        import re
        if row['key'] in seen or not re.fullmatch(r'[a-z0-9_]{2,32}',row['name']):raise ValueError('Duplicate key or invalid emoji name')
        seen.add(row['key']);path=inside(pack,row['file'])
        animated=row.get('animated',False)
        if type(animated) is not bool or path.suffix!=('.gif' if animated else '.png') or path.stat().st_size>256*1024 or digest(path)!=row['sha256']:raise ValueError('Upload artwork changed')
        with Image.open(path) as image:
            image.load()
            if image.size!=(128,128) or image.format!=('GIF' if animated else 'PNG'):raise ValueError('Emoji must be a 128px PNG or reviewed animated GIF')
            count=getattr(image,'n_frames',1)
            if count>600 or (count>1)!=animated:raise ValueError('Emoji animation does not match manifest')
            for i in range(count):image.seek(i);image.load()
    return data['icons']


def same_image(first,second,animated=False):
    from PIL import Image
    with Image.open(io.BytesIO(first)) as a,Image.open(io.BytesIO(second)) as b:
        if not animated:return a.size==b.size and a.convert('RGBA').tobytes()==b.convert('RGBA').tobytes()
        def timeline(image):
            import hashlib
            if getattr(image,'n_frames',1)<2:return None
            result=[]
            for i in range(image.n_frames):
                image.seek(i);image.load();frame=image.convert('RGBA')
                frame.paste((0,0,0,0),mask=frame.getchannel('A').point(lambda a:255 if a==0 else 0))
                value=(frame.size,hashlib.sha256(frame.tobytes()).hexdigest())
                duration=int(image.info.get('duration',0))
                if duration<=0:return None
                if result and result[-1][0]==value:result[-1]=(value,result[-1][1]+duration)
                else:result.append((value,duration))
            return image.info.get('loop',0),result
        left,right=timeline(a),timeline(b)
        return left is not None and left==right


class RejectedUpload(ValueError):
    """Discord explicitly rejected the request before creating an emoji."""


class API:
    def __init__(self,session,token):self.session=session;self.token=token
    async def call(self,method,path,body=None):
        deadline=time.monotonic()+60
        while True:
            async with self.session.request(method,'https://discord.com/api/v10'+path,json=body,headers={'Authorization':'Bot '+self.token},allow_redirects=False) as response:
                if response.status==429:
                    value=await response.json();delay=float(value.get('retry_after',1))
                    if delay<0 or time.monotonic()+delay>deadline:raise RejectedUpload('Discord rate limit exceeded this run; retry later')
                    try:await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        # Cancellation while waiting after an explicit rejection
                        # is certain, unlike cancellation of an in-flight POST.
                        raise RejectedUpload('Rate-limit wait interrupted; retry at the next start') from None
                    continue
                if response.status in (400,401,403,404,405,413):raise RejectedUpload('Discord rejected request: HTTP '+str(response.status))
                if response.status>=300:raise ValueError('Discord request failed: HTTP '+str(response.status))
                return await response.json()
    async def image(self,identity,animated=False):
        suffix='.gif' if animated else '.png'
        async with self.session.get('https://cdn.discordapp.com/emojis/'+str(identity)+suffix+'?size=128&quality=lossless',allow_redirects=False) as response:
            if response.status!=200:raise ValueError('Cannot verify existing emoji artwork')
            raw=await response.content.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('Remote emoji too large')
            return raw


async def preview(api,app,rows,pack,registry,pending):
    listed=await api.call('GET',f'/applications/{app}/emojis');existing={str(e['id']):e for e in listed['items']};by_name={}
    for emoji in existing.values():by_name.setdefault(emoji['name'],[]).append(emoji)
    receipts=registry.get('applications',{}).get(str(app),{});plan=[]
    for row in rows:
        receipt=receipts.get(row['key'],{});match=existing.get(str(receipt.get('id')))
        animated=row.get('animated',False)
        if match and receipt.get('sha256')==row['sha256'] and match['name']==receipt.get('name') and bool(match.get('animated',False))==animated:
            plan.append(dict(row,state='reuse',id=match['id']));continue
        matches=[]
        for candidate in by_name.get(row['name'],[]):
            if bool(candidate.get('animated',False))!=animated:continue
            remote=await api.image(candidate['id'],animated=True) if animated else await api.image(candidate['id'])
            if same_image(inside(pack,row['file']).read_bytes(),remote,animated=animated):matches.append(candidate)
        if len(matches)==1:plan.append(dict(row,state='verified_existing',id=str(matches[0]['id'])));continue
        if by_name.get(row['name']):raise ValueError('Conflicting/ambiguous emoji name: '+row['name'])
        if str(app)+':'+row['key'] in pending:raise ValueError('Uncertain earlier upload for '+row['key']+'. Check Discord and rerun --import-existing; do not resend automatically.')
        plan.append(dict(row,state='create'))
    if len(existing)+sum(r['state']=='create' for r in plan)>2000:raise ValueError('Application emoji capacity exceeded; nothing uploaded')
    return plan
