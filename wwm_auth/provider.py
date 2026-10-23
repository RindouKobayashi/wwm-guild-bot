"""Retrieve and validate an API token for the explicitly configured test character."""
import base64
import copy
import hashlib
import hmac
import json
import secrets
import ssl
import struct
import time
import urllib.error
import urllib.request
import msgpack
from .protocol import (TOKEN, fields, created_owner, account_rpc, incoming_calls,
                       token_properties, creation_token_values, only_existing_avatar)
from .session import Session

API_URL = 'https://h72naxx2gb-ms-prod.easebar.com/flk/redis_player/get_players_info'


class SavedLoginExpired(ValueError):
    """Only the verified SDK-relay rejection permits upstream renewal."""


def validate_token(token, context):
    raw_token = token.encode('ascii') if isinstance(token, str) else token
    if not TOKEN.fullmatch(raw_token):
        raise ValueError('Invalid token format')
    callback = base64.b64encode(struct.pack('>I', int(time.time())) + secrets.token_bytes(8)).decode('ascii')
    pid = context['character_id']
    body = msgpack.packb({'uid': callback, 'fields': ['base'],
                         'hostnum2pids': {context['character_hostnum']: [pid]}}, use_bin_type=True)
    request = urllib.request.Request(API_URL, body, {
        'Content-Type': 'application/octet-stream', 'Accept-Encoding': 'identity',
        'h72-ms-token': raw_token.decode('ascii'), 'h72-ms-uid': pid + '_' + callback}, method='POST')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_default_certs()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=tls))
    try:
        with client.open(request, timeout=10) as response:
            status = response.status
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        return False, {'http_status': exc.code, 'requested_character_returned': False}
    if status != 200 or len(raw) > 1024 * 1024:
        return False, {'http_status': status, 'requested_character_returned': False}
    obj = msgpack.unpackb(raw, raw=False, strict_map_key=False,
                         max_map_len=4096, max_array_len=4096, max_str_len=1048576, max_bin_len=1048576)
    code = obj.get('code') if isinstance(obj, dict) else None
    result = obj.get('result') if isinstance(obj, dict) else None
    matched = isinstance(result, dict) and isinstance(result.get(pid), dict)
    valid = type(code) is int and code == 0 and matched
    return valid, dict(http_status=status, result_code=code if type(code) is int else None,
                       requested_character_returned=matched)


def retrieve_token(context):
    report = dict(steam_calls=0, sdk_requests=0, main_account_used=False, api_validation_requests=0)
    with Session(context) as session:
        owner = None
        for _ in range(16):
            method, body = session.receive()
            if method == 12:
                candidate = created_owner(body)
                if candidate and candidate['class'] == b'Account':
                    owner = candidate
                    break
        if owner is None:
            raise ValueError('Account entity not received')
        auth = copy.deepcopy(context['auth'])
        auth['hostid'] = context['logical_hostnum']
        info = copy.deepcopy(context['auth_info'])
        info['authjson'] = json.dumps(auth, separators=(',', ':'))
        device = copy.deepcopy(context['device'])
        device['server'] = context['logical_hostnum']
        session.send(account_rpc(owner, 'sdk_login', [info, device, context['logical_hostnum']]))
        verified = selected = confirmed = redirected = False
        owners, pending, checked = set(), {}, set()
        while time.monotonic() < session.deadline:
            method, body = session.receive()
            if method == 12:
                entity = created_owner(body)
                if entity and entity['class'] == b'PlayerAvatar':
                    if not selected or entity['id'] != context['character_id'].encode('ascii'):
                        raise ValueError('Avatar identity mismatch')
                    owners.add(entity['guid'])
                    pending.setdefault(entity['guid'], set()).update(creation_token_values(body))
            if method == 10:
                for guid, token in token_properties(body):
                    pending.setdefault(guid, set()).add(token)
            if method in (10, 11):
                for label, args in incoming_calls(method, body):
                    if (label == 'login_fail' and isinstance(args, list) and len(args) > 1
                            and type(args[1]) is int and args[1] == 40046):
                        raise SavedLoginExpired('Saved login rejected or continuation unsupported')
                    if label in ('login_fail', 'logout_by_server', 'logout_by_other', 'on_relay_to_account', 'need_queue_up'):
                        raise ValueError('Saved login rejected or continuation unsupported')
                    if label == 'set_login_info':
                        if not isinstance(args, list) or not args or not isinstance(args[0], dict):
                            raise ValueError('Invalid account identity callback')
                        actual = hashlib.sha256(str(args[0].get('uid', '')).encode()).hexdigest()
                        verified = hmac.compare_digest(actual, context['expected_sdkuid_sha256'])
                        if not verified:
                            raise ValueError('Authenticated account mismatch')
                    elif label == 'try_to_relay_other':
                        if not verified or confirmed:
                            raise ValueError('Unexpected session handover')
                        session.send(account_rpc(owner, 'confirm_relay_other', [1]))
                        confirmed = True
                    elif label == 'on_relay_to_avatar':
                        if not verified or not confirmed or redirected:
                            raise ValueError('Unexpected avatar relay')
                        session.reconnect_avatar(args)
                        selected = redirected = True
                        owners.clear()
                        pending.clear()
                        report['test_avatar_reconnected'] = True
                    elif label == 'show_player' and not selected:
                        if not verified or only_existing_avatar(args) != context['character_id']:
                            raise ValueError('Test character selection mismatch')
                        session.send(account_rpc(owner, 'player_login', [context['character_id']]))
                        selected = True
            for token in (value for guid in owners for value in pending.get(guid, ())):
                if token in checked:
                    continue
                checked.add(token)
                if len(checked) > 2:
                    raise ValueError('Token validation budget reached')
                accepted, detail = validate_token(token, context)
                report['api_validation_requests'] += 1
                report['validation'] = detail
                if accepted:
                    report['status'] = 'validated_test_token'
                    return token.decode('ascii'), report
    raise TimeoutError('No validated token within bounded session')
