"""Offline fresh gate-handshake construction; no sockets or credential storage.

Models the observed mode-zero transport. Never reuse recorded session keys,
RC4 snapshots, client IDs, reconnect keys or old encrypted packets for login.
RSA OAEP-SHA1 follows the native RSA public-encrypt padding selector (4).
Server acceptance of this standalone implementation is not yet established.
"""
import hashlib
import secrets
import struct
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from transport_rc4_state import RC4State


def varint(value):
    if not isinstance(value, int) or not 0 <= value < (1 << 64):
        raise ValueError('Unsigned protobuf integer bounds')
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def byte_field(number, value):
    return varint(number * 8 + 2) + varint(len(value)) + value


def envelope(method, payload=b''):
    if not isinstance(method, int) or not 0 <= method <= 65535 or not isinstance(payload, bytes) or len(payload) > 8 * 1024 * 1024 - 2:
        raise ValueError('Envelope bounds/type')
    return struct.pack('<IH', len(payload) + 2, method) + payload


def validate_session_key_ok(frame):
    # Inbound gate method table: seed_reply=0, session_key_ok=1.
    # Frame ordinal zero in the decrypted stream is NOT message ID zero.
    if frame != envelope(1):
        raise ValueError('Expected encrypted SessionKeyOK')
    return True


def build_fresh_session(public_key, seed, crypto=0, enable_zstd=False):
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 2048:
        raise ValueError('Expected inspected RSA-2048 public key')
    if not isinstance(seed, int) or not -(1 << 63) <= seed < (1 << 63):
        raise ValueError('SessionSeed.seed int64 bounds')
    if crypto != 0:
        raise ValueError('Only inspected mode-zero transport supported')
    if not isinstance(enable_zstd, bool):
        raise ValueError('Expected Boolean negotiation flag')
    key = hashlib.sha1(secrets.token_bytes(32)).hexdigest().encode('ascii')
    header = secrets.token_bytes(2 + secrets.randbelow(8))
    tail = secrets.token_bytes(2 + secrets.randbelow(8))
    payload = byte_field(1, header) + byte_field(2, key)
    payload += varint(3 * 8) + varint(seed & ((1 << 64) - 1))
    payload += byte_field(4, tail)
    if enable_zstd:
        payload += varint(6 * 8) + b'\x01'
    encrypted = public_key.encrypt(payload, padding.OAEP(
        mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    # The seed request and wrapped-key envelope are plaintext. Separate cipher
    # instances start at the beginning of the fresh key for each direction.
    return {'session_key': key, 'seed_request': envelope(0),
            'session_key_request': envelope(1, byte_field(1, encrypted)),
            'send_cipher': RC4State.from_key(key), 'receive_cipher': RC4State.from_key(key)}
