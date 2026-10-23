"""Bounded test-only Steam -> SDK renewal. Credentials never enter logs."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import shutil
import ssl
import subprocess
import urllib.request
from .context import ROOT, context_path, validate

SDK_URL = 'https://mgbjp-h72na.unisdk.easebar.com/h72naxx2gb/sdk/uni_sauth'
HELPER = ROOT / 'wwm_auth/headless_steam/bin/Release/net8.0/HeadlessSteam.dll'


def private_json(path):
    if path.stat().st_size > 1024 * 1024 or (os.name == 'posix' and path.stat().st_mode & 0o077):
        raise ValueError('Private renewal configuration permissions or size invalid')
    return json.loads(path.read_text(encoding='utf-8'))


def configuration(context):
    validate(context)
    config = private_json(context_path().parent / 'sdk-renewal.json')
    if (config.get('format') != 1 or config.get('url') != SDK_URL
            or config.get('expected_steamid_sha256') != context['expected_steamid_sha256']
            or config.get('expected_sdkuid_sha256') != context['expected_sdkuid_sha256']):
        raise ValueError('Renewal configuration identity mismatch')
    original = config['request']
    if hashlib.sha256(str(original.get('sdkuid', '')).encode()).hexdigest() != context['expected_steamid_sha256']:
        raise ValueError('Renewal Steam account mismatch')
    if 'sessionid' in original or not isinstance(config.get('metadata'), dict):
        raise ValueError('Renewal template contains stale credential or invalid metadata')
    key = base64.b64decode(config['signing_key_base64'], validate=True)
    if not 1 <= len(key) <= 4096:
        raise ValueError('Renewal signing key invalid')
    return config, key


@contextmanager
def steam_ticket(context):
    if not HELPER.is_file() or not shutil.which('dotnet'):
        raise RuntimeError('Headless Steam helper needs building on this device')
    process = subprocess.Popen(['dotnet', str(HELPER), 'ticket', str(context_path().resolve())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', cwd=ROOT)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        line = pool.submit(process.stdout.readline, 32769).result(timeout=65)
        if len(line) > 32768:
            raise ValueError('Headless response too large')
        value = json.loads(line)
        if value.get('expected_steamid_sha256') != context['expected_steamid_sha256']:
            raise ValueError('Headless Steam identity mismatch')
        ticket = base64.b64decode(value['ticket'], validate=True)
        if not 1 <= len(ticket) <= 8192:
            raise ValueError('Headless ticket size invalid')
        yield ticket
    finally:
        try:
            process.stdin.write('release\n')
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        process.stdout.close()
        pool.shutdown(wait=True, cancel_futures=True)


def candidate_context(context, config, key, ticket, client=None):
    """One SDK request; preserve configured game/device/character identity."""
    request = copy.deepcopy(config['request'])
    request['sessionid'] = 'E-steam-' + base64.b64encode(ticket.hex().upper().encode()).decode()
    request['sessionid'] += '-' + base64.b64encode(json.dumps(config['metadata'], separators=(',', ':')).encode()).decode()
    request['client_login_sn'] = secrets.token_hex(16)
    body = json.dumps(request, separators=(',', ':'), ensure_ascii=False).encode()
    signature = hmac.new(key, b'POST/h72naxx2gb/sdk/uni_sauth' + body, hashlib.sha256).hexdigest()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    if client is None:
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(SDK_URL, body, {'Content-Type': 'application/json',
        'Accept-Encoding': 'identity', 'x-client-sign': signature}, method='POST')
    with client.open(req, timeout=15) as response:
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError('SDK response bounds invalid')
    obj = json.loads(raw)
    if type(obj.get('code')) is not int or obj['code'] != 200:
        raise ValueError('SDK renewal rejected')
    login = obj.get('unisdk_login_json')
    if isinstance(login, str):
        login = json.loads(base64.b64decode(login, validate=True))
    if not isinstance(login, dict) or not hmac.compare_digest(
            hashlib.sha256(str(login.get('sdkuid', '')).encode()).hexdigest(), context['expected_sdkuid_sha256']):
        raise ValueError('SDK renewal account mismatch')
    channel = login.get('extra', {}).get('channel_raw_info')
    if isinstance(channel, str):
        channel = json.loads(channel)
    credential = channel.get('login_token') if isinstance(channel, dict) else None
    if not isinstance(credential, str) or not 1 <= len(credential) <= 65536:
        raise ValueError('SDK game credential missing')
    value = copy.deepcopy(context)
    value['auth'].update(sdkuid=str(login['sdkuid']), sessionid=credential,
        client_login_sn=request['client_login_sn'], gas_token=login.get('gas_token') or login.get('sdk_token') or '')
    value['exported_at_utc'] = datetime.now(timezone.utc).isoformat()
    validate(value)
    return value


def renew(context):
    config, key = configuration(context)
    with steam_ticket(context) as ticket:
        return candidate_context(context, config, key, ticket)
