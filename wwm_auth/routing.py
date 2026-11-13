"""Explicit public production-route refresh; never authenticates an account."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import time
import urllib.request

from .context import context_path, load, validate, write_private

HOST = 'h72naxx2gb.update.easebar.com'
PRODUCTION_API = 'h72naxx2gb-ms-prod.easebar.com'
URLS = {slot: f'https://{HOST}/server_list_prod_{slot.lower()}.txt' for slot in ('A', 'B')}
MAX_BYTES = 2 * 1024 * 1024


def parse_public_list(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Public routing size invalid')
    rows = []
    seen = set()
    for line in raw.decode('utf-8-sig').splitlines():
        columns = line.split()
        if not columns:
            continue
        if len(columns) != 11:
            raise ValueError('Public routing schema changed')
        endpoint = columns[7].split(':')
        if (len(endpoint) != 4 or not ipaddress.ip_address(endpoint[0]).is_global
                or endpoint[1] != '4000' or not all(x.isdecimal() for x in endpoint[2:])
                or not columns[8].isdecimal() or not 1 <= int(columns[8]) <= 0xffffffff):
            raise ValueError('Public routing endpoint invalid')
        identity = (endpoint[0], int(columns[8]))
        if identity in seen:
            raise ValueError('Duplicate public routing entry')
        seen.add(identity)
        rows.append(dict(host=endpoint[0], hostnum=int(columns[8]), api=columns[9]))
    if not rows:
        raise ValueError('Public routing list empty')
    return rows


def candidate(context, snapshots):
    """Keep logical/home IDs and credentials; do not infer a numeric offset."""
    validate(context)
    choices = []
    for slot, raw in snapshots.items():
        if slot not in URLS:
            raise ValueError('Unsupported public routing slot')
        rows = parse_public_list(raw)
        # Rolling updates can move the previous slot to preonline with IDs +5000.
        # Never send account data to that environment or migrate those IDs.
        production = [row for row in rows if row['api'] == PRODUCTION_API]
        peers = {row['host'] for row in production if row['hostnum'] == context['logical_hostnum']}
        if peers:
            choices.append((slot, peers, {row['host'] for row in production}))
    if not choices:
        raise ValueError('Configured server absent from published production routes')
    if len({(frozenset(peers), frozenset(allowed)) for _, peers, allowed in choices}) != 1:
        raise ValueError('Production routing slots disagree; no context changed')
    slot, peers, allowed = choices[0]
    value = copy.deepcopy(context)
    value['peer_host'] = context['peer_host'] if context['peer_host'] in peers else sorted(peers)[0]
    value['peer_port'] = 4000
    value['allowed_peers'] = sorted(allowed)
    validate(value)
    return value, dict(slot=slot, configured_server_preserved=True,
        peer_changed=value['peer_host'] != context['peer_host'],
        allowlist_changed=value['allowed_peers'] != context['allowed_peers'],
        published_server_peers=len(peers), published_production_peers=len(allowed))


def fetch_public_lists():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_default_certs()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                         urllib.request.HTTPSHandler(context=tls))
    snapshots = {}
    for slot, url in URLS.items():
        request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'})
        with client.open(request, timeout=15) as response:
            raw = response.read(MAX_BYTES + 1)
            if response.status != 200:
                raise ValueError('Public routing request rejected')
        parse_public_list(raw)
        snapshots[slot] = raw
    return snapshots


def install(snapshots, path=None):
    """Atomic local routing-only update under the existing provider lock."""
    path = Path(path or context_path())
    lock = path.parent / 'provider.lock'
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError('Provider is busy; routing refresh did not change context') from None
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'pid': os.getpid(), 'started_at': time.time()}, handle)
        old = load(path)
        raw = path.read_bytes()
        if json.loads(raw) != old:
            raise ValueError('Context changed during routing refresh')
        value, report = candidate(old, snapshots)
        if value == old:
            return dict(report, context_changed=False, account_requests=0)
        if path.read_bytes() != raw:
            raise ValueError('Context changed during routing refresh')
        backup = path.parent / 'backups' / ('context-routing-' + str(time.time_ns()) + '-' + secrets.token_hex(3) + '.json')
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_fd, 'wb') as handle:
            handle.write(raw)
        write_private(path, value)
        # Leave token-cache/failure evidence intact. Its context fingerprint makes
        # the old cache/cooldown inapplicable to this newly selected route.
        return dict(report, context_changed=True, previous_context_backed_up=True, account_requests=0)
    finally:
        lock.unlink(missing_ok=True)
