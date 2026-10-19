"""Guarded fresh-test-capture API baseline; no Steam or game-process access."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from bootstrap_test_api_token import load_context, decoded_login, opener, TOKEN, msgpack
from probe_test_steam_sdk import ROOT, validate_account

SOURCE = ROOT.parent / 'token-capture/wwm-state-pair-20261018-204505-108.mitm'


def run():
    from mitmproxy import io
    _, expected_game_uid, _, old_check = load_context()
    profile = json.loads((ROOT / 'test-account-profile.private.json').read_text())
    status = json.loads(SOURCE.with_suffix('.status.json').read_text())
    if status.get('status') != 'stopped' or SOURCE.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Finalized bounded capture required')
    identities, samples, own_checks = [], [], []
    with SOURCE.open('rb') as handle:
        for flow in io.FlowReader(handle).stream():
            req = getattr(flow, 'request', None)
            if req is None:
                continue
            if req.pretty_host == 'mgbjp-h72na.unisdk.easebar.com' and req.path.endswith('/sdk/uni_sauth'):
                validate_account(profile, str(json.loads(req.content)['sdkuid']))
                login, _ = decoded_login(json.loads(flow.response.content))
                if hashlib.sha256(str(login['sdkuid']).encode()).hexdigest() != expected_game_uid:
                    raise ValueError('Fresh SDK game identity mismatch')
                identities.append(True)
            if req.pretty_host == 'h72naxx2gb-ms-prod.easebar.com':
                value = req.headers.get('h72-ms-token', '').encode('ascii')
                if TOKEN.fullmatch(value):
                    samples.append((req.timestamp_start, value, req.headers.get('h72-ms-uid', '')))
                if urlsplit(req.path).path == '/flk/chat_service/get_custom_chat_emotions':
                    body = msgpack.unpackb(req.content, raw=False, strict_map_key=False)
                    if isinstance(body, dict) and body.get('pid') == old_check[1]['pid']:
                        own_checks.append(body)
    if not identities or not own_checks:
        raise ValueError('Fresh test SDK and same-character HTTP evidence required')
    body = own_checks[-1]
    pid, hostnum = body['pid'], body['hostnum']
    samples = [row for row in samples if row[2].startswith(pid + '_')]
    if not samples or type(hostnum) is not int:
        raise ValueError('Fresh test token routing missing')
    captured_at, token, _ = max(samples, key=lambda row: row[0])
    report = {'status': 'fresh_test_capture_preflight_ok', 'steam_api_called': False,
        'game_process_access': False, 'main_account_context_used': False,
        'same_test_character_verified': True, 'capture_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'sample_age_seconds': round(time.time() - captured_at), 'cases': [], 'credentials_saved': False}
    report['token_timestamp_minus_capture_seconds'] = round(int(token.split(b':', 1)[0]) - captured_at)
    report['token_timestamp_minus_now_seconds'] = round(int(token.split(b':', 1)[0]) - time.time())
    if os.environ.get('WWM_FRESH_TEST_CAPTURE_SEAL') == '1':
        baseline = json.loads((ROOT / 'fresh-capture-api-baseline.redacted.json').read_text())
        cases = baseline.get('cases', [])
        if (baseline.get('capture_sha256') != report['capture_sha256'] or len(cases) != 3
                or cases[0].get('requested_test_player_returned') is not True
                or cases[1].get('http_status') != 403 or cases[2].get('http_status') != 403):
            raise ValueError('Protected baseline and negative controls required')
        from private_transport_state import _dpapi
        sealed = _dpapi(json.dumps({'token': token.decode('ascii'), 'pid': pid, 'hostnum': hostnum,
                                   'captured_at': captured_at}).encode())
        with (ROOT / 'fresh-api-token.user-dpapi.json').open('x') as handle:
            json.dump({'scope': 'current_windows_user', 'capture_sha256': report['capture_sha256'],
                'protected_payload_base64': base64.b64encode(sealed).decode()}, handle)
        report.update(status='verified_test_api_token_user_sealed', credentials_saved=True,
                      plaintext_credentials_saved=False)
        (ROOT / 'fresh-api-token-seal.redacted.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return
    execute = os.environ.get('WWM_FRESH_TEST_CAPTURE_EXECUTE') == '1'
    if execute:
        if time.time() - captured_at > 900:
            raise ValueError('Fresh baseline capture older than fifteen minutes')
        with (ROOT / 'fresh-capture-api-baseline-attempt.private.json').open('x') as handle:
            json.dump({'status': 'attempt_consumed'}, handle)
        invalid = token.split(b':', 1)[0] + b':' + b'0' * 32
        if invalid == token:
            raise ValueError('Invalid control equals token')
        for label, value in [('fresh_test_token', token), ('synthetic_invalid_token', invalid), ('no_token', None)]:
            row = {'case': label}
            callback = base64.b64encode(struct.pack('>I', int(time.time())) + secrets.token_bytes(8)).decode()
            payload = {'uid': callback, 'fields': ['base'], 'hostnum2pids': {hostnum: [pid]}}
            headers = {'h72-ms-uid': pid + '_' + callback, 'Content-Type': 'application/octet-stream', 'Accept-Encoding': 'identity'}
            if value is not None:
                headers['h72-ms-token'] = value.decode('ascii')
            req = urllib.request.Request('https://h72naxx2gb-ms-prod.easebar.com/flk/redis_player/get_players_info',
                msgpack.packb(payload, use_bin_type=True), headers, method='POST')
            try:
                try:
                    response = opener().open(req, timeout=10)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    row['http_status'] = response.code
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('Response budget exceeded')
                try:
                    obj = msgpack.unpackb(raw, raw=False, strict_map_key=False)
                    if isinstance(obj, dict):
                        code = obj.get('code')
                        row['result_code'] = code if type(code) is int else None
                        result = obj.get('result')
                        row['requested_test_player_returned'] = isinstance(result, dict) and isinstance(result.get(pid), dict)
                except (ValueError, UnicodeError):
                    row['messagepack_decoded'] = False
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row['error_type'] = type(exc).__name__
            report['cases'].append(row)
        report['status'] = 'fresh_test_capture_api_controls_completed'
    output = ROOT / ('fresh-capture-api-baseline.redacted.json' if execute else 'fresh-capture-api-preflight.redacted.json')
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


class Run:
    def running(self):
        from mitmproxy import ctx
        try:
            run()
        finally:
            ctx.master.shutdown()


addons = [Run()]
