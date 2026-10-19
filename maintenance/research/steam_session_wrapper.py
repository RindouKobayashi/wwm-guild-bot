"""Offline codec for the observed SDK Steam credential wrapper.

Does not request Steam tickets, authenticate, or transmit credentials. Decoded
tickets and metadata must remain in memory; callers export summaries only.
Native source: NtUniSdkSteam.dll 18001e9b0 and ticket helper 180092a30.
"""
import base64
import json
import re


def decode_steam_session(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 65536:
        raise ValueError('SDK wrapper bounds/type')
    parts = value.split('-')
    if len(parts) not in (3, 4) or parts[:2] != ['E', 'steam']:
        raise ValueError('Expected the observed Steam SDK wrapper')
    decoded = []
    for part in parts[2:]:
        try:
            raw = base64.b64decode(part, validate=True)
        except ValueError:
            raise ValueError('Invalid SDK wrapper Base64') from None
        if base64.b64encode(raw).decode() != part:
            raise ValueError('Noncanonical SDK wrapper Base64')
        decoded.append(raw)
    ticket_hex = decoded[0]
    if not ticket_hex or len(ticket_hex) % 2 or re.fullmatch(rb'[0-9A-F]+', ticket_hex) is None:
        raise ValueError('Expected the native uppercase hexadecimal ticket')
    ticket = bytes.fromhex(ticket_hex.decode('ascii'))
    if len(ticket) > 8192:
        raise ValueError('Steam ticket bounds/type')
    metadata = None
    if len(decoded) == 2:
        if len(decoded[1]) > 16384:
            raise ValueError('SDK metadata bounds')
        try:
            metadata = json.loads(decoded[1])
        except (ValueError, UnicodeError):
            raise ValueError('SDK wrapper metadata is not JSON') from None
        if not isinstance(metadata, dict):
            raise ValueError('SDK wrapper metadata is not an object')
    return ticket, metadata


def encode_steam_session(ticket, metadata=None):
    if not isinstance(ticket, bytes) or not 1 <= len(ticket) <= 8192:
        raise ValueError('Steam ticket bounds/type')
    value = 'E-steam-' + base64.b64encode(ticket.hex().upper().encode('ascii')).decode('ascii')
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError('SDK wrapper metadata must be an object')
        raw = json.dumps(metadata, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        if len(raw) > 16384:
            raise ValueError('SDK metadata bounds')
        value += '-' + base64.b64encode(raw).decode('ascii')
    return value


def summarize_steam_session(value):
    ticket, metadata = decode_steam_session(value)
    return {'format': 'E-steam-Base64(uppercase_hex_ticket)-Base64(metadata_json)',
            'ticket_bytes': len(ticket), 'metadata_present': metadata is not None,
            'metadata_field_types': {k: type(v).__name__ for k, v in (metadata or {}).items()
                if isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', k)},
            'reencoded_exactly': encode_steam_session(ticket, metadata) == value,
            'ticket_or_metadata_values_saved': False}
