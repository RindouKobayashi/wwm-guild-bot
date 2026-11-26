"""Original v3 request signing using a supplied native credential, not its issuer."""
import hashlib
import re

_CREDENTIAL = re.compile(rb'[0-9]{9,12}:[a-fA-F0-9]{32}')


def sign_request(credential, path, body):
    """Sign the exact wire body: MD5(path|credential-secret|body):timestamp."""
    try:
        raw = credential.encode('ascii') if isinstance(credential, str) else credential
    except UnicodeError:
        raise ValueError('Invalid native signing credential') from None
    if not isinstance(raw, bytes) or not _CREDENTIAL.fullmatch(raw):
        raise ValueError('Invalid native signing credential')
    if (not isinstance(path, str) or not path.startswith('/') or
            any(c in path for c in ('\r', '\n', '#'))):
        raise ValueError('Invalid API request target')
    if not isinstance(body, bytes):
        raise ValueError('Signing requires the exact request bytes')
    timestamp, secret = raw.split(b':', 1)
    data = path.encode('utf-8') + b'|' + secret + b'|' + body
    return hashlib.md5(data, usedforsecurity=False).hexdigest() + ':' + timestamp.decode('ascii')
