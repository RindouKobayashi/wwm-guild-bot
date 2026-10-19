"""Recovered SDK request signing, not h72-ms-token generation.

Static evidence: SDK Base 1803be170 joins uppercase method, URL path and body;
1803c07a0 uses a SHA-256 digest descriptor and keyed digest, uppercase hex.
The caller sources its key from JF_LOG_KEY. Request routine lowercases hex.
The saved POST request reproduces exactly with the current overseas Lua key.
"""
import hashlib
import hmac
from urllib.parse import urlsplit


def client_signature_candidate(url, body, key):
    if not isinstance(body, bytes) or not isinstance(key, bytes) or not key:
        raise ValueError('Exact request body bytes and nonempty key bytes required')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('Expected HTTPS SDK URL')
    # Preserve the request target exactly: no JSON reserialization or URL decoding.
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    message = b'POST' + target.encode('utf-8') + body
    return hmac.new(key, message, hashlib.sha256).hexdigest()
