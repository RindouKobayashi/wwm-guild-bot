"""Read verified TEST telemetry device context; no network or credentials saved."""
import hashlib
import json
from pathlib import Path
from audit_fresh_login_telemetry import walk

SOURCE_SHA256 = '4a2945b8a9fe405b0c1160186f8918ce8725bb90842801a8b2284cc8401cee71'
DEVICE_FIELDS = frozenset(('udid account_id pkg_lv is_android_harmony isp project aid '
    'first_deviceid unisdk_deviceid device_width engine_ver device_model app_ver '
    'imei client_platform source check_ssd first_udid sauth_login_mode tdid '
    'device_height type sdk_uid os_name network nation app_channel patch_ver '
    'ip ipv6 os_ver mac_addr oaid sdk_username is_low2 is_steam_deck is_ps5_pro').split())


def device_from_row(row, expected_uid, auth, hostnum, steam_id):
    if (not isinstance(row, dict)
            or hashlib.sha256(str(row.get('sdk_uid', '')).encode()).hexdigest() != expected_uid
            or row.get('udid') != auth['udid'] or row.get('unisdk_deviceid') != auth['deviceid']):
        return None
    device = {k: v for k, v in row.items() if k in DEVICE_FIELDS
              and type(v) in (str, int, bool) and (not isinstance(v, str) or len(v) <= 4096)}
    if not all(k in device for k in ('account_id', 'aid', 'engine_ver', 'patch_ver', 'app_ver')):
        return None
    device.update(server=hostnum, steam_account_id=steam_id)
    return device


def load_native_device(expected_uid, auth, hostnum, steam_id):
    from mitmproxy import io
    from bootstrap_test_api_token import msgpack
    from probe_fresh_test_capture_auth import SOURCE
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError('Native device test capture integrity mismatch')
    with SOURCE.open('rb') as handle:
        for flow in io.FlowReader(handle).stream():
            req = getattr(flow, 'request', None)
            if req is None or not req.content or len(req.content) > 1024 * 1024:
                continue
            try:
                obj = json.loads(req.content)
            except (ValueError, UnicodeError):
                try:
                    obj = msgpack.unpackb(req.content, raw=False, strict_map_key=False)
                except (ValueError, UnicodeError):
                    continue
            for row in walk(obj):
                device = device_from_row(row, expected_uid, auth, hostnum, steam_id)
                if device is not None:
                    return device
    raise ValueError('No matching native test device context')


def load_native_login_report(expected_uid, steam_id):
    from mitmproxy import io
    from bootstrap_test_api_token import msgpack
    from probe_fresh_test_capture_auth import SOURCE
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError('Native login report test capture integrity mismatch')
    reports = []
    with SOURCE.open('rb') as handle:
        for flow in io.FlowReader(handle).stream():
            req = getattr(flow, 'request', None)
            if req is None or not req.content or b'authjson_info' not in req.content or len(req.content) > 1048576:
                continue
            obj = json.loads(req.content)
            for row in walk(obj):
                if not isinstance(row.get('authjson_info'), dict) or not isinstance(row.get('device_info'), dict):
                    continue
                auth = json.loads(row['authjson_info']['authjson'])
                device = row['device_info']
                if (hashlib.sha256(str(auth.get('sdkuid', '')).encode()).hexdigest() != expected_uid
                        or hashlib.sha256(str(device.get('sdk_uid', '')).encode()).hexdigest() != expected_uid
                        or str(device.get('steam_account_id')) != steam_id
                        or device.get('udid') != auth.get('udid')
                        or device.get('unisdk_deviceid') != auth.get('deviceid')
                        or type(row.get('hostnum')) is not int):
                    raise ValueError('Native login report identity mismatch')
                if any(device.get(k) not in ('', 0, None) for k in ('avatar_id', 'role_id', 'role_name', 'role_level')):
                    raise ValueError('Native login report contains post-login character fields')
                reports.append((row['authjson_info'], auth, device, row['hostnum']))
    if len(reports) != 1:
        raise ValueError('Exactly one verified native login report required')
    return reports[0]
