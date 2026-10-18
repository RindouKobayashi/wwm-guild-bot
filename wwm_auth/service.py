"""Shared private token cache with bounded refresh, locking and failure cooldown."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import threading
import secrets
import struct
import time
from .context import context_path, load, write_private

_mutex = threading.Lock()


def request_identity():
    """Use the configured token issuer for headers, not an old .env account ID."""
    context = load()
    callback = base64.b64encode(struct.pack('>I', int(time.time())) + secrets.token_bytes(8)).decode('ascii')
    return context['character_id'] + '_' + callback


def fingerprint(context):
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_cache(path, identity, ttl):
    try:
        if os.name == 'posix' and path.stat().st_mode & 0o077:
            return None
        if path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_text())
        from .protocol import TOKEN
        age = time.time() - value['obtained_at']
        token = value['token']
        if (value['context_sha256'] == identity and 0 <= age < ttl
                and isinstance(token, str) and TOKEN.fullmatch(token.encode('ascii'))):
            return token
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def get_token(force=False, rejected_token=None):
    with _mutex:
        path = context_path()
        context = load(path)
        identity = fingerprint(context)
        ttl = int(os.environ.get('WWM_AUTH_CACHE_SECONDS', '240'))
        if not 60 <= ttl <= 600:
            raise ValueError('WWM_AUTH_CACHE_SECONDS must be 60..600')
        cache, lock, failure = (path.parent / name for name in ('token-cache.json', 'provider.lock', 'failure.json'))
        token = read_cache(cache, identity, ttl)
        if token and (not force or (rejected_token is not None and rejected_token != token)):
            return token
        deadline = time.monotonic() + 65
        while True:
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as handle:
                    json.dump({'pid': os.getpid(), 'started_at': time.time()}, handle)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise RuntimeError('Saved-login provider locked; check the other process or stale lock')
                time.sleep(0.2)
        try:
            token = read_cache(cache, identity, ttl)
            if token and (not force or (rejected_token is not None and rejected_token != token)):
                return token
            if failure.exists():
                last = json.loads(failure.read_text())
                if last.get('context_sha256') == identity and time.time() < last.get('retry_after', 0):
                    raise RuntimeError('Saved-login provider is cooling down after a failed attempt')
            try:
                from .provider import retrieve_token
                token, report = retrieve_token(context)
            except Exception:
                write_private(failure, {'context_sha256': identity, 'retry_after': time.time() + 60})
                raise RuntimeError('Saved test login could not provide a validated token; credential may need replacement') from None
            write_private(cache, {'context_sha256': identity, 'obtained_at': time.time(), 'token': token})
            write_private(path.parent / 'last-result.json', report)
            failure.unlink(missing_ok=True)
            return token
        finally:
            lock.unlink(missing_ok=True)


async def get_token_async(force=False, rejected_token=None):
    return await asyncio.to_thread(get_token, force=force, rejected_token=rejected_token)
