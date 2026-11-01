"""Small allowlists: unknown fields and endpoints never become stored payloads."""
import json
import re
from urllib.parse import urlsplit

LIMIT = 16 * 1024
FIELDS = frozenset('code status result data base stats attributes equipment equipment_list equip_id quality value min max affix scores hostnum number_id id fields force_search page limit skip rank_name rank_list score time timestamp count total level school damage heal members team_id club_id club_number_id hostnum2pids pid_list club_ids hostnums'.split())
REQUEST_FIELDS = FIELDS - {'result', 'data'}
SECRET_KEY = re.compile(r'token|secret|password|cookie|authorization|credential|session|ticket|login|message|chat|content|nickname|email|phone|signature|device|uid', re.I)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')

def endpoint(url):
    parts = urlsplit(url)
    return parts.path[:250] or '/'

def preview(value, allowed=FIELDS):
    def walk(item, depth=0, field=''):
        if depth > 7:
            return '[depth limited]'
        if isinstance(item, dict):
            result = {str(k): walk(v, depth + 1, str(k)) for k, v in list(item.items())[:100]
                      if str(k) in allowed and not SECRET_KEY.search(str(k))}
            # WWM uses IDs as dynamic result-map keys. Never persist those keys;
            # retain allowlisted numeric/stat fields under anonymous entry labels.
            if field in ('result', 'hostnum2pids'):
                for index, (key, child) in enumerate(list(item.items())[:40]):
                    if str(key) not in allowed and isinstance(child, (dict, list)) and re.fullmatch(r'[0-9a-fA-F_-]{5,64}', str(key)):
                        result['entry_' + str(index+1)] = walk(child, depth+1, 'entry')
            return result
        if isinstance(item, (list, tuple)):
            result = [walk(v, depth + 1, field) for v in item[:40]]
            if len(item) > 40:
                result.append({'omitted_items': len(item) - 40})
            return result
        if isinstance(item, bytes):
            return '[binary: %d bytes]' % len(item)
        if isinstance(item, str):
            if field in ('data', 'result'):
                return '[text omitted]'
            return redact(item)[:512]
        if item is None or isinstance(item, (int, float, bool)):
            return item
        return '[unsupported value]'
    try:
        result = walk(value)
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return {'omitted': 'Preview contains unsupported values'}
    if len(encoded.encode()) > LIMIT:
        return {'truncated': True, 'reason': 'Preview exceeds 16 KiB'}
    return result

def redact(value):
    """Defense in depth for legacy text; structured logs use fixed summaries."""
    import os
    text = ANSI.sub('', str(value))
    for key, secret in os.environ.items():
        if SECRET_KEY.search(key) and secret and len(secret) >= 6:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)(bearer\s+)[\w.\-/+=]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)([\w-]*(?:token|secret|password|cookie|credential|ticket|authorization)[\w-]*[\s\"\x27:=]+)[^\s,;\}\"\x27]+', r'\1[redacted]', text)
    text = re.sub(r'\b[\w-]{20,}\.[\w-]{6,}\.[\w-]{20,}\b', '[redacted]', text)
    text = re.sub(r'https?://[^\s]+', lambda m: urlsplit(m[0]).scheme + '://' + (urlsplit(m[0]).hostname or '') + endpoint(m[0]), text)
    return text[:8192]
