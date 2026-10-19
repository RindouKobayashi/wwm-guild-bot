"""Offline SDK HTTP -> encrypted game-login correlation; never replay credentials."""
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from steam_session_wrapper import summarize_steam_session

HERE = Path(__file__).resolve().parent
PAIR = HERE / 'output/token-update-20261016/paired-state-135247'
ALLOWED = {'gas_token', 'sessionid', 'sdkuid', 'gameid', 'login_channel'}


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def token_shape(value):
    if not isinstance(value, str) or not value:
        return 'empty_or_nonstring'
    parts = value.split('.')
    if len(parts) == 3:
        try:
            header = json.loads(base64.urlsafe_b64decode(parts[0] + '=' * (-len(parts[0]) % 4)))
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
            if isinstance(header, dict) and isinstance(claims, dict):
                return 'jwt_shaped_not_signature_verified'
        except (ValueError, UnicodeError):
            pass
    return 'opaque_string'


def jwt_metadata(value):
    if token_shape(value) != 'jwt_shaped_not_signature_verified':
        return None
    parts = value.split('.')
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    result = {'claim_names': sorted(k for k in claims if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', k)),
              'signature_verified': False}
    issuer = claims.get('iss')
    if isinstance(issuer, str):
        try:
            parsed = urlsplit(issuer)
            if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
                result['issuer_host'] = parsed.hostname
        except ValueError:
            pass
    issued, expires = claims.get('iat'), claims.get('exp')
    if isinstance(issued, (int, float)) and isinstance(expires, (int, float)):
        result['declared_lifetime_seconds'] = expires - issued
    return result


def build_targets():
    import msgpack
    from inspect_paired_outgoing import load_pair
    from compact_gate_messages import inner_message, entity_method_index, rpc_index
    _, evidence, _, _, frames, _ = load_pair()
    targets = None
    for outer, body, position, compressed in frames:
        if outer != 32768:
            continue
        inner, rows = inner_message(body)
        if inner != 9 or entity_method_index(rows) != rpc_index('sdk_login'):
            continue
        payload = next(v for n, w, v in rows if (n, w) == (12, 2))
        args = msgpack.unpackb(payload, raw=False, strict_map_key=False)
        if not isinstance(args, list) or len(args) != 3 or not isinstance(args[0], dict):
            raise ValueError('SDK login argument shape differs')
        auth = json.loads(args[0]['authjson'])
        if targets is not None:
            raise ValueError('Multiple SDK logins require separate correlation')
        targets = {k: fingerprint(v) for k, v in auth.items()
                   if k in ALLOWED and isinstance(v, (str, int)) and v not in ('', 0)}
    if not targets:
        raise ValueError('No login field fingerprints')
    output = PAIR / 'login-field-fingerprints.private.json'
    output.write_text(json.dumps({'capture_sha256': evidence['capture_sha256'], 'fingerprints': targets}, indent=2))
    print(json.dumps({'status': 'login_field_fingerprints_prepared', 'fields': sorted(targets), 'raw_credentials_saved': False}))


def containers(value, depth=0, path='$'):
    if depth > 6:
        return
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            label = key if isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', key) else '<key>'
            yield from containers(child, depth + 1, path + '.' + label)
    elif isinstance(value, list):
        for child in value[:1000]:
            yield from containers(child, depth + 1, path + '[]')
    elif isinstance(value, str) and len(value) <= 2 * 1024 * 1024:
        candidates = [value]
        try:
            candidates.append(base64.b64decode(value, validate=True).decode('utf-8'))
        except (ValueError, UnicodeError):
            pass
        for candidate in candidates:
            if candidate.lstrip().startswith(('{', '[')):
                try:
                    yield from containers(json.loads(candidate), depth + 1, path + '.decoded_json')
                except ValueError:
                    pass


class WwmLoginCorrelation:
    def running(self):
        from mitmproxy import ctx, io
        try:
            target = json.loads((PAIR / 'login-field-fingerprints.private.json').read_text())
            source = Path(os.environ['WWM_PAIR_CAPTURE'])
            before = source.stat()
            if before.st_size > 64 * 1024 * 1024:
                raise ValueError('Capture size budget')
            if hashlib.sha256(source.read_bytes()).hexdigest() != target['capture_sha256']:
                raise ValueError('Different capture')
            matches = []
            sauth_flows = 0
            credential_presence = {}
            scoped_credentials = []
            oauth_schemas = []
            channel_categories = set()
            sdk_request_session_metadata = []
            request_session_fingerprints = set()
            authentication_endpoints = {}
            bearer_fingerprints = []
            oauth_fingerprints = {'access_token': set(), 'refresh_token': set()}
            oauth_secrets_in_memory = {'access_token': set(), 'refresh_token': set()}
            with source.open('rb') as handle:
                for ordinal, flow in enumerate(io.FlowReader(handle).stream()):
                    req = getattr(flow, 'request', None)
                    if req is None:
                        continue
                    request_path = urlsplit(req.path).path
                    authorization = req.headers.get('authorization', '')
                    if authorization.lower().startswith('bearer '):
                        bearer_fingerprints.append((ordinal, req.pretty_host, fingerprint(authorization[7:].strip())))
                    if any(word in request_path.lower() for word in ('oauth', 'refresh', 'login', 'sauth')):
                        # Do not export arbitrary path components, which can
                        # contain identifiers or credentials.
                        allow = {'api', 'v1', 'v2', 'v3', 'v9', 'v10', 'sdk', 'uni_sauth', 'oauth', 'oauth2', 'authorize',
                                 'auth', 'login', 'refresh', 'token', 'access_token', 'refresh_token', '@me'}
                        safe_path = '/'.join(s if s in allow else '<segment>' for s in request_path.split('/') if s)
                        label = req.pretty_host + '/' + safe_path
                        authentication_endpoints[label] = authentication_endpoints.get(label, 0) + 1
                    if not request_path.endswith('/uni_sauth'):
                        continue
                    sauth_flows += 1
                    for direction, message in [('request', req), ('response', getattr(flow, 'response', None))]:
                        if message is None:
                            continue
                        text = message.get_text(strict=False)
                        if not text or len(text) > 2 * 1024 * 1024:
                            continue
                        objects = []
                        try:
                            objects.append(json.loads(text))
                        except ValueError:
                            objects.append({k: v[0] for k, v in parse_qs(text).items() if len(v) == 1})
                        found = set()
                        aliases = {}
                        for obj in objects:
                            for container_path, dictionary in containers(obj):
                                if direction == 'response' and container_path.endswith('.oauth2'):
                                    for key in oauth_fingerprints:
                                        value = dictionary.get(key)
                                        if isinstance(value, str) and value:
                                            oauth_fingerprints[key].add(fingerprint(value))
                                            if 16 <= len(value) <= 8192:
                                                oauth_secrets_in_memory[key].add(value.encode())
                                    oauth_schemas.append({'container': container_path,
                                        'field_types': {k: type(v).__name__ for k, v in dictionary.items()
                                            if isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', k)}})
                                if direction == 'request':
                                    channel = dictionary.get('login_channel')
                                    if channel in ('steam', 'steam_global', 'netease', 'netease_global', 'epic'):
                                        channel_categories.add(channel)
                                    session = dictionary.get('sessionid')
                                    if isinstance(session, str) and session:
                                        request_session_fingerprints.add(fingerprint(session))
                                        metadata = {'container': container_path,
                                            'characters': len(session),
                                            'shape': 'hex_string' if re.fullmatch(r'[a-fA-F0-9]+', session) and len(session) % 2 == 0 else token_shape(session),
                                            'equals_game_authjson_sessionid': fingerprint(session) == target['fingerprints'].get('sessionid')}
                                        metadata['delimiter_counts'] = {delimiter: session.count(delimiter)
                                            for delimiter in ('.', ':', '|', '@', '&', '=', '%', '{', '}') if delimiter in session}
                                        try:
                                            decoded_session = json.loads(session)
                                            if isinstance(decoded_session, dict):
                                                metadata['shape'] = 'json_object_string'
                                                metadata['field_types'] = {k: type(v).__name__ for k, v in decoded_session.items()
                                                    if isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', k)}
                                        except ValueError:
                                            pass
                                        sdk_request_session_metadata.append(metadata)
                                        if session.startswith('E-steam-'):
                                            metadata['shape'] = 'steam_sdk_wrapper'
                                            metadata['verified_steam_wrapper'] = summarize_steam_session(session)
                                else:
                                    for field in ('login_token', 'access_token', 'refresh_token', 'expires_in', 'refresh_expires_in'):
                                        if field in dictionary:
                                            entry = credential_presence.setdefault(field, {'present': True, 'nonempty_or_nonzero': False})
                                            entry['nonempty_or_nonzero'] |= dictionary[field] not in ('', None, 0, '0')
                                            scoped_credentials.append({'container': container_path, 'field': field,
                                                'nonempty_or_nonzero': dictionary[field] not in ('', None, 0, '0'),
                                                'shape': token_shape(dictionary[field]) if 'token' in field else 'not_classified'})
                                            if 'token' in field:
                                                metadata = jwt_metadata(dictionary[field])
                                                if metadata:
                                                    scoped_credentials[-1]['jwt_metadata'] = metadata
                                for key, value in dictionary.items():
                                    if key in target['fingerprints'] and isinstance(value, (str, int)) and fingerprint(value) == target['fingerprints'][key]:
                                        found.add(key)
                                    if isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', key) and isinstance(value, (str, int)):
                                        value_hash = fingerprint(value)
                                        for target_key, target_hash in target['fingerprints'].items():
                                            if value_hash == target_hash:
                                                aliases.setdefault(target_key, set()).add(key)
                        if found:
                            matches.append({'flow_ordinal': ordinal, 'direction': direction,
                                            'endpoint': 'SDK uni_sauth', 'matching_fields': sorted(found),
                                            'matching_value_field_names': {k: sorted(v) for k, v in aliases.items()},
                                            'timestamp': message.timestamp_start})
            oauth_usage = []
            # Second pass keeps raw credentials in memory only, so even an
            # opaque header/query/body transport can be recognized without
            # exporting its value or assuming a field name.
            with source.open('rb') as handle:
                for ordinal, flow in enumerate(io.FlowReader(handle).stream()):
                    req = getattr(flow, 'request', None)
                    if req is None:
                        continue
                    surfaces = {'headers': b'\n'.join(v for k, v in req.headers.fields),
                                'body': req.content or b'', 'path_or_query': req.path.encode()}
                    for key, secrets in oauth_secrets_in_memory.items():
                        found = [name for name, data in surfaces.items() if len(data) <= 2 * 1024 * 1024
                                 and any(secret in data for secret in secrets)]
                        if found:
                            oauth_usage.append({'flow_ordinal': ordinal, 'service_host': req.pretty_host,
                                                'matching_sdk_field': key, 'request_surfaces': found})
            archived_matches = []
            archived_capture_count = 0
            archived_bytes = 0
            seen_archives = {target['capture_sha256']}
            for archive in sorted(source.parent.glob('*.mitm'))[:32]:
                stat = archive.stat()
                if stat.st_size > 64 * 1024 * 1024 or archived_bytes + stat.st_size > 128 * 1024 * 1024:
                    raise ValueError('Archive scan budget')
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                if digest in seen_archives:
                    continue
                seen_archives.add(digest)
                archived_bytes += stat.st_size
                archived_capture_count += 1
                with archive.open('rb') as handle:
                    for ordinal, flow in enumerate(io.FlowReader(handle).stream()):
                        request = getattr(flow, 'request', None)
                        if request is None or not urlsplit(request.path).path.endswith('/uni_sauth'):
                            continue
                        for direction, message in [('request', request), ('response', getattr(flow, 'response', None))]:
                            if message is None:
                                continue
                            text = message.get_text(strict=False)
                            if not text or len(text) > 2 * 1024 * 1024:
                                continue
                            try:
                                value = json.loads(text)
                            except ValueError:
                                continue
                            for path, dictionary in containers(value):
                                for key, value in dictionary.items():
                                    if isinstance(value, str) and fingerprint(value) in request_session_fingerprints:
                                        if isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,48}', key):
                                            archived_matches.append({'capture': archive.name, 'flow_ordinal': ordinal,
                                                'direction': direction, 'container': path, 'matching_field': key})
                final = archive.stat()
                if (stat.st_size, stat.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                    raise ValueError('Archived capture changing')
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Capture changing')
            report = {'status': 'paired_login_http_correlation_completed', 'sauth_flows': sauth_flows,
                      'matches': matches, 'raw_credentials_saved': False, 'network_requests_made': False,
                      'known_login_channel_categories': sorted(channel_categories),
                      'sdk_request_sessionid_metadata': sdk_request_session_metadata,
                      'sdk_response_credential_presence': credential_presence,
                      'sdk_response_scoped_credential_presence': scoped_credentials,
                      'oauth2_schemas': oauth_schemas,
                      'authentication_endpoint_inventory': authentication_endpoints,
                      'sdk_oauth_bearer_correlations': [
                          {'flow_ordinal': ordinal, 'service_host': host, 'matching_sdk_field': key}
                          for ordinal, host, digest in bearer_fingerprints
                          for key, digests in oauth_fingerprints.items() if digest in digests],
                      'sdk_oauth_exact_request_usage': oauth_usage,
                      'archived_unique_captures_checked': archived_capture_count,
                      'current_sdk_request_sessionid_archive_matches': archived_matches,
                      'limitation': 'Matching SDK login fields does not establish HTTP API-token issuance or unattended SDK renewal.'}
            output = PAIR / 'login-http-correlation.redacted.json'
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
        finally:
            ctx.master.shutdown()


addons = [WwmLoginCorrelation()]

if __name__ == '__main__':
    build_targets()
