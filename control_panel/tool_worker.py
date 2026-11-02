"""Disposable API worker: fixed registry, bounded I/O, sanitize before IPC."""
import asyncio
import ipaddress
import json
import os
import socket
import sys
import time
import uuid
from urllib.parse import urlsplit
import aiohttp
import msgpack
from .privacy import preview, REQUEST_FIELDS, FIELDS, endpoint, redact
from .tools import validate, load_profiles

class ToolFailure(Exception):
    def __init__(self,code,message):self.code=code;super().__init__(message)

def public_address(value):
    ip=ipaddress.ip_address(value)
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified and not (getattr(ip,'ipv4_mapped',None) and not ip.ipv4_mapped.is_global)

WWM_FIELDS=FIELDS|frozenset('attr gameplay_trail XIUWEI_KUNGFU XIUWEI_TRADE3 XIUWEI_TRADE4 XIUWEI_EXPLORE STR CON BAS CRI AGI rank rank_total_len start end player_info ud members level'.split())

class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        rows=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM,family=family)
        result=[]
        for af,_,_,_,address in rows:
            ip=ipaddress.ip_address(address[0])
            if not public_address(str(ip)):
                raise ToolFailure('destination_blocked','This destination resolves to a private or reserved address.')
            result.append(dict(hostname=host,host=str(ip),port=port,family=af,proto=0,flags=socket.AI_NUMERICHOST))
        if not result:raise ToolFailure('network','The approved host could not be resolved.')
        return result
    async def close(self):pass

async def bounded_body(response):
    chunks=[];size=0
    async for chunk in response.content.iter_chunked(32768):
        size+=len(chunk)
        if size>2*1024*1024:raise ToolFailure('too_large','Response exceeded the 2 MiB limit.')
        chunks.append(chunk)
    return b''.join(chunks)

async def execute(tool,inputs,operation_id,environment):
    profiles=load_profiles() if tool.startswith('custom-') else {}
    inputs=validate(tool,inputs,profiles)
    events=[];all_attempts=[];started=time.time();secrets_used=[]
    def clean_result(value):
        encoded=json.dumps(value,allow_nan=False)
        for secret in secrets_used:
            if secret:encoded=encoded.replace(json.dumps(secret)[1:-1],'[redacted]')
        return json.loads(encoded)
    connector=aiohttp.TCPConnector(resolver=PublicResolver(),use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector,trust_env=False,timeout=aiohttp.ClientTimeout(total=10)) as http:
        async def request(url,method='POST',payload=None,headers=None,query=None,encoding='msgpack',allowed=None):
            parsed=urlsplit(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:raise ToolFailure('destination_blocked','Only approved HTTPS requests are supported.')
            # aiohttp bypasses its resolver for numeric hosts; validate those here.
            try:ip=ipaddress.ip_address(parsed.hostname)
            except ValueError:ip=None
            if ip and not public_address(str(ip)):raise ToolFailure('destination_blocked','Private destinations are blocked.')
            body=(msgpack.packb(payload) if encoding=='msgpack' else json.dumps(payload,allow_nan=False).encode()) if payload is not None else None
            if body and len(body)>65536:raise ToolFailure('too_large','Request body exceeded 64 KiB.')
            req_id=uuid.uuid4().hex;attempts=[];response_value=None;failure=None;status=None;begin=time.time();response_size=0
            try:
                for n in range(2):
                    tick=time.monotonic();record=dict(number=n+1,status=None,endpoint=endpoint(url),request_id=req_id)
                    try:
                        async with http.request(method,url,data=body,params=query,headers=headers,allow_redirects=False) as response:
                            status=record['status']=response.status
                            if status in (401,403):raise ToolFailure('auth_rejected','Authentication was rejected. Check host authentication readiness; expiry is unknown.')
                            if 300<=status<400:raise ToolFailure('redirect_blocked','The API returned a redirect, which is not followed.')
                            if status==429:
                                delay=response.headers.get('Retry-After','')
                                if n==0 and delay.isdigit() and int(delay)<=3:
                                    await asyncio.sleep(int(delay));continue
                                raise ToolFailure('rate_limited','The API is rate limited. Try again later.')
                            if status<200 or status>=300:raise ToolFailure('http_error','The API returned HTTP '+str(status)+'.')
                            raw=await bounded_body(response);response_size=len(raw)
                            try:
                                if encoding=='msgpack':response_value=msgpack.unpackb(raw,raw=False,strict_map_key=False)
                                elif encoding=='json':response_value=json.loads(raw)
                                else:response_value={'text':'[text omitted by privacy policy]','bytes':len(raw)}
                            except (ValueError,UnicodeError,msgpack.UnpackException):raise ToolFailure('decode_failure','The API response could not be decoded.') from None
                            if isinstance(response_value,dict) and response_value.get('code') not in (None,0):raise ToolFailure('application_error','The API reported an application-level failure.')
                            return response_value
                    except asyncio.CancelledError:
                        record['error']='TimeoutError';raise
                    except (asyncio.TimeoutError,aiohttp.ClientConnectionError):
                        record['error']='TimeoutError' if sys.exc_info()[0] is asyncio.TimeoutError else 'ConnectionError'
                        if n:raise ToolFailure('timeout','The API timed out or could not be reached.') from None
                        await asyncio.sleep(.5)
                    finally:
                        record['duration_ms']=round((time.monotonic()-tick)*1000,2);attempts.append(record);all_attempts.append(record)
            except asyncio.CancelledError:
                failure=ToolFailure('timeout','Lookup reached its time limit.');raise
            except aiohttp.ClientPayloadError:
                failure=ToolFailure('decode_failure','The API returned an incomplete response.');raise failure from None
            except ToolFailure as exc:
                failure=exc;raise
            finally:
                data=dict(id=req_id,parent_id=operation_id,tool_id=tool,endpoint=endpoint(url),label='API Tools · '+tool,method=method,
                    duration_ms=round((time.time()-begin)*1000,2),status=status,outcome='error' if failure else 'unknown' if allowed is None and isinstance(response_value,dict) and response_value.get('code') is None else 'success',
                    error=failure.code if failure else None,attempts=attempts,request_bytes=len(body or b''),response_bytes=response_size,
                    request=preview(payload,REQUEST_FIELDS),response=preview(response_value,allowed) if allowed is not None else preview(response_value,WWM_FIELDS))
                events.append(dict(kind='request',time=begin,source='api_tools_custom' if tool.startswith('custom-') else 'api_tools',environment=environment,data=data))
        try:
            if tool.startswith('custom-'):
                p=profiles[tool[7:]];headers=dict(inputs['header'])
                for name,env in p.get('credential_headers',{}).items():
                    value=os.environ.get(env)
                    if not value:raise ToolFailure('auth_missing','A credential required by this profile is missing on the host.')
                    headers[name]=value;secrets_used.append(value)
                if inputs['method']=='POST':headers['Content-Type']='application/json'
                result=await request(p['origin'].rstrip('/')+inputs['path'],inputs['method'],inputs['body'] if inputs['method']=='POST' else None,headers,inputs['query'],p.get('response_format','json'),set(p['response_fields']))
                safe=preview(result,set(p['response_fields']))
                # Redact values even when an approved response field echoes credentials.
                encoded=json.dumps(safe)
                for secret in secrets_used:encoded=encoded.replace(json.dumps(secret)[1:-1],'[redacted]')
                safe=json.loads(encoded)
                for event in events:event['data']['response']=safe
            else:
                # Reuse the public helpers, but replace only this disposable process's
                # transport. Never invoke provider login/renewal from API Tools.
                import utility.wwm as wwm
                import settings
                token=settings.WWM_TOKEN
                if settings.WWM_AUTH_MODE=='saved_login':
                    from wwm_auth.context import load,context_path
                    from wwm_auth.service import read_cache,fingerprint
                    context=load();token=read_cache(context_path().parent/'token-cache.json',fingerprint(context),int(os.environ.get('WWM_AUTH_CACHE_SECONDS','240')))
                    if not token:raise ToolFailure('auth_missing','No recent cached API credential is available. Use the existing host authentication setup; this tool will not log in automatically.')
                if not token:raise ToolFailure('auth_missing','The host API credential is not configured.')
                secrets_used.append(token)
                headers=await wwm.get_default_headers(token_override=token)
                async def transport(url,payload,*args,**kwargs):
                    value=await request(url,payload=payload,headers=headers)
                    if not isinstance(value,dict):raise ToolFailure('decode_failure','The WWM response was not an expected object.')
                    return value
                wwm._wwm_api_post=transport
                if tool=='player':result=await wwm.get_player_info(inputs['number_id'],fields=['base','attr']+(['gameplay_trail'] if inputs['fields']=='gameplay' else []))
                elif tool=='guild':result=await (wwm.get_club_by_number_id(int(inputs['value'])) if inputs['by']=='number' else wwm.get_club_by_name(inputs['value'],limit=5))
                elif tool=='rankings':result=await wwm.get_rank_list(inputs['rank'],int(inputs['page']))
                else:
                    player=await wwm.get_player_info(inputs['number_id'],fields=['base'])
                    pid=(player or {}).get('result',{}).get('id')
                    host=(player or {}).get('result',{}).get('hostnum') if inputs['hostnum']=='auto' else int(inputs['hostnum'])
                    if pid and not host:raise ToolFailure('server_unknown','The player lookup did not identify a server. Choose a server explicitly.')
                    result=await wwm.get_player_combat_plan(pid,int(host)) if pid else None
                safe=preview(result,WWM_FIELDS)
            empty=result is None or result==[] or result=={} or (isinstance(result,dict) and 'result' in result and result['result'] in (None,{},[]))
            return clean_result(dict(outcome='empty' if empty else 'success',message='No matching results.' if empty else 'Lookup complete. Only approved fields are shown.',response=safe,attempts=all_attempts,events=events,duration_ms=round((time.time()-started)*1000,2)))
        except asyncio.CancelledError:
            return clean_result(dict(outcome='error',code='timeout',message='Lookup reached its time limit. No automatic retry will run.',response=None,attempts=all_attempts,events=events,duration_ms=round((time.time()-started)*1000,2)))
        except ToolFailure as exc:
            return clean_result(dict(outcome='error',code=exc.code,message=str(exc),response=None,attempts=all_attempts,events=events,duration_ms=round((time.time()-started)*1000,2)))

async def main():
    # stdout is a private pipe, never a log. Suppress imports' legacy output.
    from contextlib import redirect_stdout,redirect_stderr
    original=sys.stdout
    body=json.loads(sys.stdin.buffer.read(70000))
    with open(os.devnull,'w') as null,redirect_stdout(null),redirect_stderr(null):
        try:result=await asyncio.wait_for(execute(body['tool'],body['inputs'],body['id'],body['environment']),29)
        except asyncio.TimeoutError:result=dict(outcome='error',code='timeout',message='Lookup exceeded its time limit.',events=[])
        except Exception:result=dict(outcome='error',code='setup',message='Lookup could not start. Check the tool profile and host authentication setup.',events=[])
    original.write(json.dumps(result,allow_nan=False));original.flush()

if __name__=='__main__':asyncio.run(main())
