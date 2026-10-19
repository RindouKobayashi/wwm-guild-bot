"""One-shot own-account Steam -> SDK login probe, without a game connection.

Run as an offline mitmdump addon to load the verified request template. Default
mode validates only. WWM_SDK_PROBE_EXECUTE=1 requests ONE fresh Steam ticket,
checks the signed-in Steam ID against the template, sends ONE HTTPS request to
the original SDK host, then discards credentials and cancels the Steam ticket.
"""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.request
from check_sdk_client_signature import key_candidates, EXPECTED_CAPTURE
from probe_steam_ticket import request_ticket, DEFAULT_DLL
from sdk_client_signature import client_signature_candidate
from steam_session_wrapper import decode_steam_session, encode_steam_session
from correlate_paired_login import containers

HERE = Path(__file__).resolve().parent
REPORT = HERE / 'output/token-update-20261016/paired-state-135247/fresh-sdk-login.redacted.json'
HOST = 'mgbjp-h72na.unisdk.easebar.com'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def response_summary(value):
    found = []
    access = refresh = False
    for path, obj in containers(value):
        if path.endswith(('.channel_raw_info', '.channel_raw_info.decoded_json')):
            token = obj.get('login_token')
            if isinstance(token, str) and token:
                found.append(token)
        if path.endswith('.oauth2'):
            access |= bool(obj.get('access_token'))
            refresh |= bool(obj.get('refresh_token'))
    return {'login_credential_present': len(set(found)) == 1,
            'oauth_access_present': access, 'oauth_refresh_present': refresh,
            'response_parser_version': 2}


def prepare_template(request):
    if request.method != 'POST' or request.pretty_host != HOST or '?' in request.path or not request.path.endswith('/sdk/uni_sauth'):
        raise ValueError('Unexpected SDK endpoint/method')
    url = 'https://' + HOST + request.path
    body = request.content
    original = json.loads(body)
    account = original.get('sdkuid')
    if not isinstance(account, str) or not re.fullmatch(r'[0-9]{17}', account):
        raise ValueError('Expected Steam account identifier in SDK template')
    _, metadata = decode_steam_session(original['sessionid'])
    if not isinstance(metadata, dict) or set(metadata) != {'app_id'}:
        raise ValueError('Unexpected Steam wrapper metadata')
    matching = [key for _, key in key_candidates()
                if hmac.compare_digest(client_signature_candidate(url, body, key), request.headers.get('x-client-sign', ''))]
    if len(matching) != 1:
        raise ValueError('Original request signature not reproduced uniquely')
    return url, original, metadata, matching[0], account


def exchange(ticket, url, original, metadata, key):
    body_obj = copy.deepcopy(original)
    body_obj['sessionid'] = encode_steam_session(ticket, metadata)
    body = json.dumps(body_obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'Accept-Encoding': 'identity', 'User-Agent': 'WWM-own-account-sdk-probe/1',
               'x-client-sign': client_signature_candidate(url, body, key)}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, body, headers, method='POST')
    result = {'sdk_requests_attempted': 1, 'fresh_ticket_submitted': True,
              'captured_context_fields_retained': True}
    try:
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            result['http_status'] = response.code
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            return dict(result, status='sdk_response_size_limit')
        try:
            decoded = json.loads(payload)
        except (ValueError, UnicodeError):
            return dict(result, status='sdk_response_not_json')
        summary = response_summary(decoded)
        result.update(summary)
        result['status'] = 'fresh_sdk_login_credential_received' if result['http_status'] == 200 and summary['login_credential_present'] else 'sdk_login_not_confirmed'
        # Numeric service codes help diagnosis without exporting messages/values.
        if isinstance(decoded, dict):
            result['numeric_result_codes'] = {name: decoded[name] for name in ('code', 'status', 'errcode', 'errno')
                                               if isinstance(decoded.get(name), int) and not isinstance(decoded[name], bool)}
        return result
    except (urllib.error.URLError, TimeoutError, OSError):
        return dict(result, status='sdk_transport_failed')


class Probe:
    def running(self):
        from mitmproxy import ctx, io
        try:
            source = Path(os.environ['WWM_PAIR_CAPTURE'])
            before = source.stat()
            if before.st_size > 64 * 1024 * 1024 or hashlib.sha256(source.read_bytes()).hexdigest() != EXPECTED_CAPTURE:
                raise ValueError('Unexpected saved capture')
            prepared = []
            with source.open('rb') as handle:
                for flow in io.FlowReader(handle).stream():
                    request = getattr(flow, 'request', None)
                    if request and request.pretty_host == HOST and request.path.split('?')[0].endswith('/sdk/uni_sauth'):
                        prepared.append(prepare_template(request))
                        response = getattr(flow, 'response', None)
                        if response is None or not response_summary(json.loads(response.get_text()))['login_credential_present']:
                            raise ValueError('Parser failed the known saved SDK response')
            after = source.stat()
            if len(prepared) != 1 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Template count or capture identity changed')
            url, original, metadata, key, account = prepared[0]
            report = {'status': 'fresh_sdk_login_preflight_ok', 'original_signature_reproduced': True,
                'saved_response_parser_verified': True, 'response_parser_version': 2,
                'account_guard_required': True, 'game_connection_started': False,
                'game_memory_access': False, 'credential_values_saved': False,
                'sdk_requests_attempted': 0}
            if os.environ.get('WWM_SDK_PROBE_EXECUTE') == '1':
                result = request_ticket(DEFAULT_DLL, expected_steam_id=account,
                    consume_ticket=lambda ticket: exchange(ticket, url, original, metadata, key))
                report['steam_probe'] = result
                if result.get('consumer_result'):
                    report.update(result['consumer_result'])
                else:
                    report['status'] = result['status']
            output = REPORT if os.environ.get('WWM_SDK_PROBE_EXECUTE') == '1' else REPORT.with_name('fresh-sdk-preflight.redacted.json')
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            ctx.master.shutdown()


addons = [Probe()]
