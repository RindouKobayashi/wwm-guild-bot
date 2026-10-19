"""One test-account token bootstrap, then disconnect. No gameplay RPCs.

Default is OFFLINE preflight. Live mode requires WWM_TEST_API_TOKEN_EXECUTE=1.
Uses a fresh guarded Steam ticket, fresh transport/Account IDs, and only the
captured TEST account's context. Persists the API token encrypted for this user.
The explicit fresh launcher also exports a private plaintext portable login
only after owned-character API validation succeeds, with an old-context backup.
"""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import struct
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from collections import Counter
from extract_native_protocol_schema import fields, varint as read_varint
from gate_account_codec import account_rpc, created_owner, incoming_calls, msgpack, reply_structure
from test_gate_session import TestGateSession
from probe_test_steam_sdk import ROOT, validate_profile, validate_account
from probe_fresh_sdk_login import prepare_template, NoRedirect
from probe_steam_ticket import request_ticket, DEFAULT_DLL
from steam_session_wrapper import encode_steam_session
from sdk_client_signature import client_signature_candidate
from private_transport_state import _dpapi

TOKEN = re.compile(rb'[0-9]{9,12}:[a-fA-F0-9]{32}')
AUTH_VALIDATION_VERIFIED = True  # Protected player baseline + rejected controls.


def decoded_login(obj):
    value = obj.get('unisdk_login_json') if isinstance(obj, dict) else None
    if isinstance(value, str):
        value = json.loads(base64.b64decode(value, validate=True))
    if not isinstance(value, dict) or not value.get('sdkuid'):
        raise ValueError('SDK login response missing account identity')
    raw = value.get('extra', {}).get('channel_raw_info')
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get('login_token'), str):
        raise ValueError('SDK game-login credential missing')
    return value, raw['login_token']


def token_properties(body):
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Property frame budget exceeded')
    rows, used = fields(body)
    if used != len(body):
        raise ValueError('Property frame trailing bytes')
    guid = next((v for n, w, v in rows if (n, w) == (1, 0)), None)
    visited = 0

    def property_values(prop, depth):
        nonlocal visited
        visited += 1
        if depth > 8 or visited > 20000:
            raise ValueError('Nested property budget exceeded')
        items, _ = fields(prop)
        for pn, pw, value in items:
            if (pn, pw) == (3, 2):
                values, _ = fields(value)
                for vn, vw, text in values:
                    if (vn, vw) == (4, 2) and TOKEN.fullmatch(text):
                        yield guid, text
            elif (pn, pw) == (4, 2):
                # PropItem.dict -> PropDict.item. Initial snapshots can group
                # property values here instead of using flat update items.
                dictionary, _ = fields(value)
                for dn, dw, child in dictionary:
                    if (dn, dw) == (2, 2):
                        yield from property_values(child, depth + 1)

    for n, w, prop in rows:
        if (n, w) == (3, 2):
            yield from property_values(prop, 0)


def creation_token_values(body):
    """Typed AoiProp values in an owned entity's initial snapshot only."""
    if len(body) > 8 * 1024 * 1024:
        raise ValueError('Creation snapshot budget')
    pos = 0
    while pos < len(body):
        tag, pos = read_varint(body, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            _, pos = read_varint(body, pos)
        elif wire == 2:
            size, pos = read_varint(body, pos)
            if pos + size > len(body):
                raise ValueError('Truncated creation snapshot')
            if number == 6:
                for _, token in token_properties(body[pos:pos + size]):
                    yield token
            pos += size
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
        else:
            raise ValueError('Invalid creation snapshot wire type')
        if not number or pos > len(body):
            raise ValueError('Creation snapshot bounds')


def only_existing_avatar(args):
    if not isinstance(args, list) or not args or not isinstance(args[0], dict):
        raise ValueError('Character-list shape not recognized')
    avatars = args[0].get('avatars')
    if not isinstance(avatars, list):
        raise ValueError('No returned character list')
    active = [a for a in avatars if isinstance(a, dict) and not a.get('remove_ts')]
    if len(active) != 1 or not isinstance(active[0].get('id'), str) or len(active[0]['id']) != 16:
        raise ValueError('Exactly one active returned test character required')
    return active[0]['id']


def login_failure_metadata(args):
    report = {'game_login_error_code': args[1] if isinstance(args, list) and len(args) > 1 and type(args[1]) is int else None}
    if not isinstance(args, list):
        return report
    markers = ('session', 'token', 'signature', 'platform', 'sdk', 'server', 'host',
               'expired', 'auth', 'device', 'package', 'steam', 'invalid', 'login')
    strings = [v[:65536] for v in args if isinstance(v, str)]
    report['failure_keyword_markers'] = [word for word in markers if any(word in v.lower() for v in strings)]
    report['failure_numeric_details'] = []
    for value in strings:
        try:
            obj = json.loads(value)
        except ValueError:
            continue
        if isinstance(obj, dict):
            report['failure_numeric_details'].append({k: obj[k] for k in ('code', 'subcode', 'errno', 'errcode') if type(obj.get(k)) is int})
    return report


def load_context():
    from mitmproxy import io
    profile = json.loads((ROOT / 'test-account-profile.private.json').read_text())
    source = Path(profile['capture_path'])
    validate_profile(profile, source.read_bytes())
    prepared = []
    events = []
    read_check = None
    with source.open('rb') as handle:
        for flow in io.FlowReader(handle).stream():
            req = getattr(flow, 'request', None)
            if req is None:
                continue
            if req.pretty_host == 'mgbjp-h72na.unisdk.easebar.com' and req.path.endswith('/sdk/uni_sauth'):
                template = prepare_template(req)
                saved, _ = decoded_login(json.loads(flow.response.content))
                prepared.append((template, hashlib.sha256(str(saved['sdkuid']).encode()).hexdigest()))
            if req.pretty_host == 'mgbjp-h72na.unisdk.easebar.com' and req.path.endswith('/sdk/event'):
                try:
                    obj = json.loads(req.content)
                    if isinstance(obj, dict) and 'device_model' in obj:
                        events.append(obj)
                except (ValueError, UnicodeError):
                    pass
            if urlsplit(req.path).path == '/flk/chat_service/get_custom_chat_emotions' and flow.response is not None:
                try:
                    reply = msgpack.unpackb(flow.response.content, raw=True, strict_map_key=False)
                    code = reply.get(b'code', reply.get('code')) if isinstance(reply, dict) else None
                    uid = req.headers.get('h72-ms-uid', '')
                    payload = msgpack.unpackb(req.content, raw=False, strict_map_key=False)
                    if (type(code) is int and code == 0 and isinstance(payload, dict)
                            and isinstance(payload.get('pid'), str) and isinstance(payload.get('uid'), str)
                            and payload['pid'] + '_' + payload['uid'] == uid
                            and len(req.content or b'') <= 65536):
                        read_check = (req.method, payload, req.headers.get('content-type', 'application/octet-stream'), code,
                                      urlsplit(req.path).query)
                except (ValueError, UnicodeError, AttributeError):
                    pass
    if len(prepared) != 1:
        raise ValueError('Exactly one test SDK template required')
    template, expected_game_uid = prepared[0]
    validate_account(profile, template[4])
    events = [e for e in events if str(e.get('sdkuid', '')) == template[4]
              or hashlib.sha256(str(e.get('sdkuid', '')).encode()).hexdigest() == expected_game_uid]
    if not events:
        raise ValueError('No same-test-account device context found')
    return template, expected_game_uid, events[-1], read_check


def opener():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=context))


def fresh_game_auth(ticket, template, expected_uid, report):
    url, original, metadata, key, steam_id = template
    body_obj = copy.deepcopy(original)
    body_obj['sessionid'] = encode_steam_session(ticket, metadata)
    body_obj['client_login_sn'] = secrets.token_hex(16)
    body = json.dumps(body_obj, separators=(',', ':'), ensure_ascii=False).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json',
        'Accept-Encoding': 'identity', 'User-Agent': 'WWM-test-token-bootstrap/1',
        'x-client-sign': client_signature_candidate(url, body, key)}, method='POST')
    report['sdk_requests_attempted'] += 1
    with opener().open(req, timeout=15) as res:
        raw = res.read(1024 * 1024 + 1)
        if res.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError('Fresh SDK response outside expected bounds')
    obj = json.loads(raw)
    report['sdk_result_code'] = obj.get('code') if type(obj.get('code')) is int else None
    if obj.get('code') != 200:
        raise ValueError('SDK authentication not accepted')
    login, credential = decoded_login(obj)
    if not hmac.compare_digest(hashlib.sha256(str(login['sdkuid']).encode()).hexdigest(), expected_uid):
        raise ValueError('Returned game account does not match test capture')
    body_obj['sdkuid'] = str(login['sdkuid'])
    body_obj['sessionid'] = credential
    body_obj['gas_token'] = login.get('gas_token') or login.get('sdk_token') or ''
    for key in ('app_channel', 'login_channel', 'platform'):
        if isinstance(login.get(key), str) and login[key]:
            body_obj[key] = login[key]
    return body_obj


def validate_emote_response(token, read_check, report, selected_id):
    if read_check is None:
        return False
    method, payload, content_type, expected_code, query = read_check
    # Current v3 wrapper: h72-ms-uid = avatar.id + '_' + callback_id.
    # The request body's uid is the callback ID, not the avatar identity.
    callback = base64.b64encode(struct.pack('>I', int(time.time())) + secrets.token_bytes(8)).decode('ascii')
    uid = selected_id + '_' + callback
    payload = dict(payload, uid=callback)
    if payload.get('pid') != selected_id:
        raise ValueError('Read-only payload references a different test character')
    for key in ('avatar_id', 'player_id'):
        if isinstance(payload.get(key), str) and payload[key] != selected_id:
            raise ValueError('Read-only payload references a different test character')
    body = msgpack.packb(payload, use_bin_type=True)
    if method not in ('GET', 'POST'):
        raise ValueError('Unexpected read-only check method')
    url = 'https://h72naxx2gb-ms-prod.easebar.com/flk/chat_service/get_custom_chat_emotions'
    if query:
        url += '?' + query
    req = urllib.request.Request(url,
        body if method == 'POST' else None, {'Content-Type': content_type,
        'h72-ms-token': token.decode('ascii'), 'h72-ms-uid': uid,
        'Accept-Encoding': 'identity'}, method=method)
    report['api_validation_requests'] += 1
    try:
        with opener().open(req, timeout=10) as res:
            report['last_api_http_status'] = res.status
            raw = res.read(1024 * 1024 + 1)
            if res.status != 200 or len(raw) > 1024 * 1024:
                return False
    except urllib.error.HTTPError as exc:
        report['last_api_http_status'] = exc.code
        if exc.code in (401, 403):
            return False
        raise
    obj = msgpack.unpackb(raw, raw=True, strict_map_key=False)
    code = obj.get(b'code', obj.get('code')) if isinstance(obj, dict) else None
    report['last_api_result_code'] = code if type(code) is int else None
    return type(code) is int and code == expected_code


def validate_api_token(token, read_check, report, selected_id):
    """Authentication proof requires returning the exact test character."""
    if read_check is None or not TOKEN.fullmatch(token):
        return False
    source = read_check[1]
    if source.get('pid') != selected_id or type(source.get('hostnum')) is not int:
        raise ValueError('Protected validation routing does not match test character')
    callback = base64.b64encode(struct.pack('>I', int(time.time())) + secrets.token_bytes(8)).decode('ascii')
    payload = {'uid': callback, 'fields': ['base'], 'hostnum2pids': {source['hostnum']: [selected_id]}}
    req = urllib.request.Request('https://h72naxx2gb-ms-prod.easebar.com/flk/redis_player/get_players_info',
        msgpack.packb(payload, use_bin_type=True), {'Content-Type': 'application/octet-stream',
        'h72-ms-token': token.decode('ascii'), 'h72-ms-uid': selected_id + '_' + callback,
        'Accept-Encoding': 'identity'}, method='POST')
    report['api_validation_requests'] += 1
    try:
        with opener().open(req, timeout=10) as response:
            report['last_api_http_status'] = response.status
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                return False
    except urllib.error.HTTPError as exc:
        report['last_api_http_status'] = exc.code
        if exc.code in (401, 403):
            return False
        raise
    obj = msgpack.unpackb(raw, raw=False, strict_map_key=False,
                         max_map_len=4096, max_array_len=4096, max_str_len=1048576, max_bin_len=1048576)
    code = obj.get('code') if isinstance(obj, dict) else None
    report['last_api_result_code'] = code if type(code) is int else None
    result = obj.get('result') if isinstance(obj, dict) else None
    matched = isinstance(result, dict) and isinstance(result.get(selected_id), dict)
    report['requested_test_player_returned'] = matched
    return type(code) is int and code == 0 and matched


def run(authentication_provider=None, result_name='api-token-bootstrap-result.redacted.json'):
    template, expected_uid, event, read_check = load_context()
    report = {'status': 'token_bootstrap_preflight_ok', 'main_account_context_used': False,
              'sdk_requests_attempted': 0, 'api_validation_requests': 0,
              'game_account_login_sent': False, 'character_selection_sent': False,
              'game_memory_access': False, 'raw_credentials_saved': False,
              'token_obtained': False, 'device_context_from_test_capture': True,
              'authentication_validation_verified': AUTH_VALIDATION_VERIFIED,
              'read_only_validation_available': read_check is not None}
    if os.environ.get('WWM_TEST_API_TOKEN_EXECUTE') != '1':
        (ROOT / 'api-token-bootstrap-preflight.redacted.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return
    if read_check is None:
        raise ValueError('Same-test-account read-only validation request unavailable')
    # Negative controls proved chat-emote code 0 does not validate authentication.
    # Fail closed until an authentication-sensitive validation is integrated.
    if not AUTH_VALIDATION_VERIFIED:
        raise ValueError('Authentication-sensitive validation not yet integrated')
    suffix = ''
    if os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'after-account-switch':
        previous = json.loads((ROOT / 'api-token-bootstrap-account-mismatch.redacted.json').read_text())
        if (previous.get('steam_status') != 'steam_account_mismatch'
                or previous.get('sdk_requests_attempted') != 0
                or previous.get('game_account_login_sent') is not False):
            raise ValueError('Prior account-switch stop not verified')
        suffix = '-after-account-switch'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'native-caller':
        previous = json.loads((ROOT / 'api-token-bootstrap-before-caller-fix.redacted.json').read_text())
        if (previous.get('sdk_result_code') != 200
                or previous.get('stage') != 'account_sdk_login'
                or previous.get('character_selection_sent') is not False
                or previous.get('token_obtained') is not False):
            raise ValueError('Prior pre-character login stop not verified')
        suffix = '-native-caller'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'reply-structure':
        previous = json.loads((ROOT / 'api-token-bootstrap-native-caller.redacted.json').read_text())
        if (previous.get('stage') != 'account_sdk_login'
                or previous.get('character_selection_sent') is not False
                or previous.get('token_obtained') is not False):
            raise ValueError('Prior pre-character login stop not verified')
        suffix = '-reply-structure'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'ticket-lifetime':
        previous = json.loads((ROOT / 'api-token-bootstrap-before-ticket-lifetime.redacted.json').read_text())
        if (previous.get('sdk_result_code') != 200
                or previous.get('stage') != 'account_sdk_login'
                or previous.get('character_selection_sent') is not False
                or previous.get('token_obtained') is not False):
            raise ValueError('Prior pre-character login stop not verified')
        suffix = '-ticket-lifetime'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'native-device-context':
        baseline = json.loads((ROOT / 'fresh-capture-api-baseline.redacted.json').read_text())
        if (baseline.get('same_test_character_verified') is not True
                or baseline.get('cases', [{}])[0].get('requested_test_player_returned') is not True):
            raise ValueError('Protected test account baseline required')
        suffix = '-native-device-context'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'login-failure-code':
        previous = json.loads((ROOT / 'api-token-bootstrap-native-device.redacted.json').read_text())
        indexes = [p.get('method_index') for r in previous.get('reply_structures', []) for p in r.get('properties', [])]
        if 656485498 not in indexes or previous.get('character_selection_sent') is not False:
            raise ValueError('Prior login-failure callback evidence required')
        suffix = '-login-failure-code'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'native-auth-ip':
        previous = json.loads((ROOT / 'api-token-bootstrap-login-failure.redacted.json').read_text())
        if previous.get('game_login_error_code') != 40035 or previous.get('character_selection_sent') is not False:
            raise ValueError('Prior generic login rejection required')
        suffix = '-native-auth-ip'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'game-closed-control':
        previous = json.loads((ROOT / 'api-token-bootstrap-native-auth-ip.redacted.json').read_text())
        if previous.get('game_login_error_code') != 40035 or previous.get('auth_ip_from_fresh_connection_route') is not True:
            raise ValueError('Corrected auth-IP rejection baseline required')
        suffix = '-game-closed-control'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'native-package-tag':
        previous = json.loads((ROOT / 'api-token-bootstrap-game-closed.redacted.json').read_text())
        if previous.get('game_login_error_code') != 40035 or previous.get('character_selection_sent') is not False:
            raise ValueError('Closed-game rejection baseline required')
        suffix = '-native-package-tag'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'native-report-routing':
        previous = json.loads((ROOT / 'api-token-bootstrap-native-package.redacted.json').read_text())
        if previous.get('game_login_error_code') != 40035 or previous.get('native_package_tag_verified') is not True:
            raise ValueError('Verified-package rejection baseline required')
        suffix = '-native-report-routing'
    elif os.environ.get('WWM_TEST_API_TOKEN_RETRY') == 'native-provider':
        baseline = json.loads((ROOT / 'api-token-native-provider-baseline.redacted.json').read_text())
        if (baseline.get('status') != 'test_api_token_obtained_and_validated'
                or baseline.get('main_account_context_used') is not False
                or baseline.get('native_full_test_login_context_verified') is not True
                or baseline.get('requested_test_player_returned') is not True):
            raise ValueError('Successful native test provider baseline required')
        suffix = '-native-provider-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
    with (ROOT / ('api-token-bootstrap' + suffix + '-attempt.private.json')).open('x') as handle:
        json.dump({'status': 'attempt_consumed'}, handle)
    counts = Counter()
    calls_seen = []
    report['stage'] = 'fresh_connection'
    if suffix.startswith('-native-provider-'):
        report['invocation_id'] = os.environ.get('WWM_TEST_PROVIDER_INVOCATION', '')
    try:
        native_report = None
        if suffix == '-native-report-routing' or suffix.startswith('-native-provider-'):
            from test_native_login_context import load_native_login_report
            native_report = load_native_login_report(expected_uid, template[4])
        with TestGateSession(logical_hostnum=native_report[3] if native_report else None) as gate:
            owner = None
            for _ in range(16):
                method, body = gate.receive()
                counts[str(method)] += 1
                if method == 12:
                    candidate = created_owner(body)
                    if candidate and candidate['class'] == b'Account':
                        owner = candidate
                        break
            if owner is None:
                raise ValueError('Fresh Account entity not received')
            report['fresh_account_owner_received'] = True
            report['stage'] = 'fresh_sdk_auth'
            consumed = False
            def consume(ticket):
                nonlocal consumed
                consumed = True
                if authentication_provider is None:
                    auth = fresh_game_auth(ticket, template, expected_uid, report)
                    report['steam_ticket_held_during_game_login'] = True
                else:
                    auth = authentication_provider(template, expected_uid, report)
                    report['steam_ticket_held_during_game_login'] = False
                auth['hostid'] = gate.hostnum
                if suffix in ('-native-auth-ip', '-game-closed-control', '-native-package-tag'):
                    # Current imp_login prototype 38 obtains connect_data.ip
                    # for get_authjson_info; imp_base writes that into auth.ip.
                    # DeviceInfo.ip is a separate client-device field.
                    auth['ip'] = gate.peer_host
                    report['auth_ip_from_fresh_connection_route'] = True
                # These are optional client-context defaults. No anti-cheat, paid
                # privilege or known-device flags are forged by this helper.
                auth_info = {'authjson': json.dumps(auth, separators=(',', ':')),
                             'is_new': False, 'face_login': False, 'fenbao_state': 2,
                             'package_tag': '', 'netbar_privilege': ''}
                if suffix == '-native-package-tag':
                    from native_package_context import load_verified_tag
                    auth_info['package_tag'] = load_verified_tag()
                    report['native_package_tag_verified'] = True
                device = {'server': gate.hostnum, 'udid': auth['udid'],
                          'unisdk_deviceid': auth['deviceid'], 'sdk_uid': auth['sdkuid'],
                          'steam_account_id': template[4], 'app_channel': auth['app_channel']}
                for src, dst in [('device_model', 'device_model'), ('os_name', 'os_name'),
                                 ('os_version', 'os_ver'), ('ip', 'ip')]:
                    if isinstance(event.get(src), str):
                        device[dst] = event[src]
                if suffix in ('-native-device-context', '-login-failure-code', '-native-auth-ip', '-game-closed-control', '-native-package-tag'):
                    from test_native_login_context import load_native_device
                    device = load_native_device(expected_uid, auth, gate.hostnum, template[4])
                    report['native_device_context_verified'] = True
                    report['native_device_field_names'] = sorted(device)
                if native_report is not None:
                    saved_info, saved_auth, saved_device, saved_hostnum = native_report
                    if saved_info.get('is_new') is not False or saved_info.get('face_login') is not False or saved_hostnum != gate.hostnum:
                        raise ValueError('Native report is not a normal existing-account login')
                    new_auth = copy.deepcopy(saved_auth)
                    for key in ('sdkuid', 'sessionid', 'client_login_sn', 'gas_token'):
                        new_auth[key] = auth[key]
                    new_auth['hostid'] = gate.hostnum
                    auth_info = copy.deepcopy(saved_info)
                    auth_info['authjson'] = json.dumps(new_auth, separators=(',', ':'))
                    device = copy.deepcopy(saved_device)
                    device['server'] = gate.hostnum
                    device['sdk_uid'] = auth['sdkuid']
                    report.update(native_full_test_login_context_verified=True,
                                  native_logical_hostnum=gate.hostnum,
                                  transport_peer_hostnum=gate.peer_hostnum)
                report['stage'] = 'account_sdk_login'
                portable_export = None
                if (authentication_provider is None
                        and os.environ.get('WWM_EXPORT_FRESH_TEST_CONTEXT') == '1'):
                    from fresh_portable_context import prepare
                    portable_export = prepare(auth_info, device, expected_uid, template[4],
                                              read_check[1]['pid'], read_check[1]['hostnum'], gate.hostnum)
                gate.send(account_rpc(owner, 'sdk_login', [auth_info, device, gate.hostnum]))
                report['game_account_login_sent'] = True
                del auth_info, auth, device
                selected = False
                authenticated = False
                relay_confirmed = False
                avatar_redirected = False
                avatar_guids = set()
                pending_tokens = {}
                checked = set()
                while time.monotonic() < gate.deadline:
                    method, body = gate.receive()
                    counts[str(method)] += 1
                    if method == 12:
                        candidate = created_owner(body)
                        if candidate and candidate['class'] == b'PlayerAvatar':
                            if not selected or candidate['id'] != char_id.encode('ascii'):
                                raise ValueError('Created avatar differs from verified test character')
                            avatar_guids.add(candidate['guid'])
                            report['owned_avatar_snapshot_received'] = True
                            for value in creation_token_values(body):
                                pending_tokens.setdefault(candidate['guid'], set()).add(value)
                                report['snapshot_token_candidates'] = report.get('snapshot_token_candidates', 0) + 1
                    if method == 10:
                        structures = report.setdefault('reply_structures', [])
                        if len(structures) < 12:
                            structures.append(reply_structure(body))
                        for guid, token in token_properties(body):
                            pending_tokens.setdefault(guid, set()).add(token)
                            report['update_token_candidates'] = report.get('update_token_candidates', 0) + 1
                        for label, args in incoming_calls(method, body):
                            if label not in calls_seen:
                                calls_seen.append(label)
                            if label == 'login_fail':
                                report['stage'] = 'account_login_rejected'
                                report.update(login_failure_metadata(args))
                                raise ValueError('Game login rejected')
                            if label == 'set_login_info' and isinstance(args, list) and args and isinstance(args[0], dict):
                                uid = str(args[0].get('uid', ''))
                                authenticated = hmac.compare_digest(hashlib.sha256(uid.encode()).hexdigest(), expected_uid)
                                if not authenticated:
                                    raise ValueError('Game returned different account identity')
                            if label == 'try_to_relay_other':
                                if not authenticated or relay_confirmed or native_report is None:
                                    raise ValueError('Session requires unsupported login continuation')
                                # Verified native normal-login action. This may
                                # replace only this guarded test account's prior session.
                                gate.send(account_rpc(owner, 'confirm_relay_other', [1]))
                                relay_confirmed = True
                                report['test_session_handover_confirmed'] = True
                            if label == 'on_relay_to_avatar':
                                if not authenticated or not relay_confirmed or avatar_redirected:
                                    raise ValueError('Session requires unsupported login continuation')
                                char_id = read_check[1]['pid']
                                gate.reconnect_avatar(args, char_id)
                                selected = True
                                avatar_redirected = True
                                avatar_guids.clear()
                                pending_tokens.clear()
                                report.update(existing_test_avatar_reconnected=True, stage='await_api_token')
                                continue
                            if label == 'show_player' and not selected:
                                if not authenticated:
                                    raise ValueError('Character selection before verified account callback')
                                char_id = only_existing_avatar(args)
                                gate.send(account_rpc(owner, 'player_login', [char_id]))
                                selected = True
                                report['character_selection_sent'] = True
                                report['stage'] = 'await_api_token'
                            if label in ('logout_by_server', 'logout_by_other',
                                         'on_relay_to_account', 'need_queue_up'):
                                raise ValueError('Session requires unsupported login continuation')
                    for token in (value for guid in avatar_guids for value in pending_tokens.get(guid, ())):
                        if token in checked:
                            continue
                        checked.add(token)
                        if len(checked) > 2:
                            raise ValueError('API token validation budget reached')
                        if not validate_api_token(token, read_check, report, char_id):
                            continue
                        if portable_export is not None:
                            from fresh_portable_context import install
                            install(*portable_export)
                            report.update(portable_context_exported=True,
                                          portable_context_previous_backed_up=True,
                                          raw_credentials_saved=True)
                        sealed = _dpapi(token)
                        (ROOT / 'api-token.user-dpapi.json').write_text(json.dumps({
                            'protected_token_base64': base64.b64encode(sealed).decode(),
                            'scope': 'current_windows_user', 'token_sha256': hashlib.sha256(token).hexdigest()}, indent=2))
                        report.update(status='test_api_token_obtained_and_validated', token_obtained=True,
                                      token_user_sealed=True, token_property_owner='PlayerAvatar')
                        break
                    if report['token_obtained']:
                        break
                if not report['token_obtained']:
                    report['status'] = 'token_not_obtained_within_budget'
                return {'status': 'bounded_game_login_finished'}
            if authentication_provider is None:
                result = request_ticket(DEFAULT_DLL, expected_steam_id=template[4], consume_ticket=consume)
            else:
                result = consume(None)
            if not consumed:
                report['steam_status'] = result['status']
                raise ValueError('Guarded fresh Steam authentication unavailable')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report['status'] = 'token_bootstrap_incomplete'
        report['error_type'] = type(exc).__name__
        # Only fixed local messages; no provider response text or credential.
        safe = {'Game login rejected', 'Fresh Account entity not received', 'Guarded fresh Steam authentication unavailable',
                'Returned game account does not match test capture', 'Game returned different account identity',
                'Character selection before verified account callback',
                'Exactly one active returned test character required',
                'Session requires unsupported login continuation', 'Connection closed during token bootstrap'}
        if str(exc) in safe:
            report['error_reason'] = str(exc)
    report['inbound_method_counts'] = dict(counts)
    report['account_callbacks_seen'] = calls_seen
    (ROOT / result_name).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


class Run:
    def running(self):
        from mitmproxy import ctx
        try:
            run()
        finally:
            ctx.master.shutdown()


addons = [Run()]
