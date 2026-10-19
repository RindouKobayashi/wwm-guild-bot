"""Look for native login-context schemas in verified test capture; offline only."""
import hashlib
import ast
import json
from urllib.parse import parse_qs
from bootstrap_test_api_token import load_context, msgpack
from probe_test_steam_sdk import ROOT
from probe_fresh_test_capture_auth import SOURCE


def walk(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for item in list(value.values())[:128]:
            yield from walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:128]:
            yield from walk(item, depth + 1)
    elif isinstance(value, str) and len(value) <= 65536 and (value.lstrip().startswith(('{', '[')) or 'authjson_info' in value):
        candidate = value
        if 'authjson_info' in value and '{' in value:
            candidate = value[value.find('{'):value.rfind('}') + 1]
        try:
            parsed = json.loads(candidate)
        except (ValueError, RecursionError):
            try:
                parsed = ast.literal_eval(candidate)
            except (ValueError, SyntaxError, RecursionError):
                return
        yield from walk(parsed, depth + 1)


class Run:
    def running(self):
        from mitmproxy import ctx, io
        try:
            load_context()
            baseline = json.loads((ROOT / 'fresh-capture-api-baseline.redacted.json').read_text())
            if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != baseline['capture_sha256']:
                raise ValueError('Test capture integrity mismatch')
            hits = []
            literal_mentions = {'authjson_info': 0, 'Account_sdk_login': 0, 'area_tag': 0, 'ps_social_flag': 0, 'support_pose_sender_script': 0}
            log_containers = []
            with SOURCE.open('rb') as handle:
                for index, flow in enumerate(io.FlowReader(handle).stream()):
                    req = getattr(flow, 'request', None)
                    if req is None or not req.content or len(req.content) > 1024 * 1024:
                        continue
                    for key in literal_mentions:
                        literal_mentions[key] += req.content.count(key.encode())
                    if b'authjson_info' in req.content:
                        text = req.content.decode('utf-8', errors='replace')
                        form = parse_qs(text, max_num_fields=512)
                        log_containers.append({'flow_ordinal': index, 'content_type': req.headers.get('content-type', ''),
                            'bytes': len(req.content), 'line_count': len(text.splitlines()),
                            'form_field_count': len(form), 'starts_json': text.lstrip().startswith(('{', '['))})
                    try:
                        obj = json.loads(req.content)
                    except (ValueError, UnicodeError):
                        try:
                            obj = msgpack.unpackb(req.content, raw=False, strict_map_key=False)
                        except (ValueError, UnicodeError):
                            obj = []
                            try:
                                text = req.content.decode('utf-8')
                                candidates = text.splitlines() + [v for values in parse_qs(text, max_num_fields=512).values() for v in values]
                                for candidate in candidates[:512]:
                                    try:
                                        obj.append(json.loads(candidate))
                                    except ValueError:
                                        pass
                            except (ValueError, UnicodeError):
                                continue
                    for value in walk(obj):
                        if any(key in value for key in ('authjson_info', 'fenbao_state', 'package_tag')) or ('sdk_uid' in value and 'unisdk_deviceid' in value):
                            hits.append({'flow_ordinal': index, 'field_names': sorted(k for k in value if isinstance(k, str)),
                                         'native_device_fields': sorted(value.get('device_info', {})) if isinstance(value.get('device_info'), dict) else None})
            report = {'status': 'offline_test_login_telemetry_audit', 'hits': hits, 'literal_mentions': literal_mentions, 'log_containers': log_containers,
                      'network_requests': 0, 'credential_values_exported': False}
            (ROOT / 'fresh-login-telemetry-schema.redacted.json').write_text(json.dumps(report, indent=2))
            print(json.dumps({'status': report['status'], 'matching_schemas': len(hits), 'literal_mentions': literal_mentions, 'log_containers': log_containers, 'network_requests': 0}))
        finally:
            ctx.master.shutdown()


addons = [Run()]
