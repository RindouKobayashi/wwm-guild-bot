"""Private portable context storage; no research or Windows imports."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parents[1]


def context_path():
    path = Path(os.environ.get('WWM_AUTH_CONTEXT', '.wwm-auth/context.json')).expanduser()
    return path if path.is_absolute() else ROOT / path


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate(value):
    if value.get('format') != 1 or value.get('account_kind') != 'test':
        raise ValueError('Verified test context version required')
    expected = value['expected_sdkuid_sha256']
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Invalid account guard')
    auth, device = value['auth'], value['device']
    for uid in (auth.get('sdkuid'), device.get('sdk_uid')):
        if not hmac.compare_digest(hashlib.sha256(str(uid).encode()).hexdigest(), expected):
            raise ValueError('Context identity mismatch')
    if not isinstance(auth.get('sessionid'), str) or not 1 <= len(auth['sessionid']) <= 65536:
        raise ValueError('Saved game login missing')
    if (not auth.get('udid') or not auth.get('deviceid') or
            device.get('udid') != auth['udid'] or device.get('unisdk_deviceid') != auth['deviceid']):
        raise ValueError('Context device mismatch')
    steam = str(device.get('steam_account_id', ''))
    if not hmac.compare_digest(hashlib.sha256(steam.encode()).hexdigest(), value['expected_steamid_sha256']):
        raise ValueError('Saved account association mismatch')
    if not isinstance(value['character_id'], str) or len(value['character_id'].encode('ascii')) != 16:
        raise ValueError('Invalid test character guard')
    for key in ('logical_hostnum', 'character_hostnum'):
        if type(value[key]) is not int or not 1 <= value[key] <= 0xffffffff:
            raise ValueError('Invalid server number')
    if not isinstance(value['allowed_peers'], list) or not 1 <= len(value['allowed_peers']) <= 4096:
        raise ValueError('Invalid relay allowlist')
    for host in value['allowed_peers']:
        if not isinstance(host, str) or not ipaddress.ip_address(host).is_global:
            raise ValueError('Nonpublic game peer')
    if value['peer_host'] not in value['allowed_peers'] or value['peer_port'] != 4000:
        raise ValueError('Invalid original game peer')
    info = value['auth_info']
    if info.get('is_new') is not False or info.get('face_login') is not False:
        raise ValueError('Only existing-account login is supported')
    if not isinstance(value['public_key_pem'], str) or len(value['public_key_pem']) > 4096:
        raise ValueError('Invalid game public key')
    return value


def load(path=None):
    path = Path(path or context_path())
    if os.name == 'posix' and path.stat().st_mode & 0o077:
        raise ValueError('Private context permissions must be 600 (chmod 600)')
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Context file too large')
    return validate(json.loads(path.read_text(encoding='utf-8')))
