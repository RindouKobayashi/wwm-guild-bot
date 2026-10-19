"""Install a validated fresh TEST login; no Steam/network calls in this module."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
from pathlib import Path
import sys

# mitmdump adds the addon directory, not necessarily the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wwm_auth.context import context_path, load, validate, write_private


def prepare(auth_info, device, expected_uid, steam_id, character_id, character_hostnum, logical_hostnum):
    previous = load()
    if (previous['expected_sdkuid_sha256'] != expected_uid
            or previous['expected_steamid_sha256'] != hashlib.sha256(steam_id.encode()).hexdigest()
            or previous['character_id'] != character_id
            or previous['character_hostnum'] != character_hostnum
            or previous['logical_hostnum'] != logical_hostnum):
        raise ValueError('Fresh export identity differs from configured test context')
    value = copy.deepcopy(previous)
    value['auth_info'] = copy.deepcopy(auth_info)
    value['auth'] = json.loads(value['auth_info'].pop('authjson'))
    value['device'] = copy.deepcopy(device)
    value['exported_at_utc'] = datetime.now(timezone.utc).isoformat()
    validate(value)
    return value, previous


def install(candidate, previous):
    """Caller must first validate the owned avatar's fresh API token."""
    validate(candidate)
    path = context_path()
    lock = path.parent / 'provider.lock'
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.close(descriptor)
        if load(path) != previous:
            raise ValueError('Portable context changed during fresh authentication')
        backup = path.parent / 'backups' / ('context-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4) + '.json')
        write_private(backup, previous)
        write_private(path, candidate)
    finally:
        lock.unlink(missing_ok=True)
