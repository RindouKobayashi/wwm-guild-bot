"""Offline test of native-derived SDK signing against one saved request.

Reads two JF_LOG_KEY assignments from the freshly extracted constants module.
Never prints keys, request bodies, signatures, tickets, or player identifiers.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
from sdk_client_signature import client_signature_candidate

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / 'output/token-update-20261016'
EXPECTED_CAPTURE = 'bde90d58aa63182ea79f1d7ec08a07feb8bf69bcd590f01e9fb0fb1b64d4e9a6'
EXPECTED_LUA = '90bb93e252b4d3b9d2d2faed1c7b5a362916a582d5c1f919f57635c257ff92ad'


def key_candidates():
    from lua54_activity_constants import inspect
    raw = (OUTPUT / 'sdk-key-current-lua/D31023E07374731B.luac').read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_LUA:
        raise ValueError('Constants module identity changed')
    tree = inspect(raw, instructions=True)
    proto = tree['children'][2]
    constants = proto['constants']
    if constants[13] != 'JF_LOG_KEY':
        raise ValueError('Expected property name changed')
    result = []
    # Current engine opcode 20 assigns an upvalue-table constant field. These
    # two sites correspond to the two region branches in recovered Lua.
    for pc, value_index in ((20, 14), (35, 24)):
        word = proto['instructions'][pc]
        if (word & 127, (word >> 16) & 255, (word >> 24) & 255, (word >> 15) & 1) != (20, 13, value_index, 1):
            raise ValueError('Expected constant assignment changed')
        value = constants[value_index]
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError('Unexpected key type/bounds')
        result.append((f'region_branch_pc_{pc}', value.encode()))
    return result


class Check:
    def running(self):
        from mitmproxy import ctx, io
        try:
            source = Path(os.environ['WWM_PAIR_CAPTURE'])
            before = source.stat()
            if before.st_size > 64 * 1024 * 1024 or hashlib.sha256(source.read_bytes()).hexdigest() != EXPECTED_CAPTURE:
                raise ValueError('Unexpected saved capture')
            keys = key_candidates()
            results = []
            with source.open('rb') as handle:
                for flow in io.FlowReader(handle).stream():
                    request = getattr(flow, 'request', None)
                    if request is None or request.pretty_host != 'mgbjp-h72na.unisdk.easebar.com' or not request.path.split('?')[0].endswith('/sdk/uni_sauth'):
                        continue
                    signature = request.headers.get('x-client-sign', '')
                    if request.method != 'POST' or not signature:
                        raise ValueError('Missing expected POST/signature')
                    url = 'https://' + request.pretty_host + request.path
                    for label, key in keys:
                        generated = client_signature_candidate(url, request.content, key)
                        results.append({'key_source': label,
                            'matches_recorded_header_exactly': hmac.compare_digest(generated, signature),
                            'matches_ignoring_hex_case': hmac.compare_digest(generated.upper(), signature.upper())})
            after = source.stat()
            if len(results) != 2 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Unexpected request count or capture changed')
            report = {'status': 'sdk_client_signature_checked', 'candidate_count': len(keys),
                'results': results, 'network_requests_made': False, 'credential_values_saved': False,
                'formula': 'lowercase_hex(HMAC-SHA256(JF_LOG_KEY, POST + request_target + exact_body))'}
            (OUTPUT / 'paired-state-135247/sdk-client-signature.redacted.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            ctx.master.shutdown()


addons = [Check()]
