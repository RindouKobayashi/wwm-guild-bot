"""Offline portability, account guards, token delivery and cache regression tests."""
import asyncio
import ast
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock, Mock
import typing
import aiohttp
import msgpack

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from wwm_auth.context import validate, write_private
from wwm_auth.protocol import (byte_field, varint, envelope, rpc_index, token_properties,
                               creation_token_values, avatar_reconnection, fields)
from wwm_auth.provider import retrieve_token
from wwm_auth.service import fingerprint, read_cache, get_token

TOKEN = '1792300000:0123456789abcdef0123456789abcdef'
PID = 'abcdefghijklmnop'


def fixture():
    return dict(format=1, account_kind='test', expected_sdkuid_sha256=hashlib.sha256(b'test-uid').hexdigest(),
                expected_steamid_sha256=hashlib.sha256(b'test-steam').hexdigest(),
                character_id=PID, character_hostnum=10401, logical_hostnum=10401,
                auth={'sdkuid': 'test-uid', 'sessionid': 'synthetic-test-credential', 'udid': 'one', 'deviceid': 'two'},
                device={'sdk_uid': 'test-uid', 'steam_account_id': 'test-steam', 'udid': 'one', 'unisdk_deviceid': 'two'},
                auth_info={'is_new': False, 'face_login': False}, peer_host='8.8.8.8', peer_port=4000,
                allowed_peers=['8.8.8.8'], public_key_pem='synthetic key not used by fake session')


def owner(kind, guid=123, snapshot=b''):
    return byte_field(1, kind) + byte_field(2, PID.encode()) + b'\x18' + varint(guid) + snapshot


def callback(label, args):
    message = byte_field(3, b'\x10' + varint(rpc_index(label) * 2))
    message += byte_field(12, msgpack.packb(args, use_bin_type=True))
    return b'\x08\x7b' + byte_field(3, byte_field(5, message))


class PortableTests(unittest.TestCase):
    def test_fresh_export_preserves_backup_and_guards_identity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('fresh_export_test', ROOT / 'maintenance/research/fresh_portable_context.py')
        export = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(export)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'context.json'
            old = fixture()
            write_private(path, old)
            info = dict(old['auth_info'], authjson=json.dumps(dict(old['auth'], sessionid='fresh-secret')))
            with patch.dict('os.environ', {'WWM_AUTH_CONTEXT': str(path)}):
                candidate, previous = export.prepare(info, old['device'], old['expected_sdkuid_sha256'],
                                                     'test-steam', PID, 10401, 10401)
                self.assertEqual(json.loads(path.read_text()), old)
                export.install(candidate, previous)
                self.assertEqual(json.loads(path.read_text())['auth']['sessionid'], 'fresh-secret')
                backups = list((path.parent / 'backups').glob('*.json'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(json.loads(backups[0].read_text()), old)
                self.assertNotIn('authjson', candidate['auth_info'])
                with self.assertRaises(ValueError):
                    export.prepare(info, old['device'], old['expected_sdkuid_sha256'],
                                   'main-steam', PID, 10401, 10401)
                with self.assertRaises(ValueError):
                    export.install(candidate, old)
                self.assertFalse((path.parent / 'provider.lock').exists())

    def test_context_rejects_other_account_and_device(self):
        validate(fixture())
        for location, key, value in [('auth', 'sdkuid', 'main'), ('device', 'sdk_uid', 'main'),
                                      ('device', 'steam_account_id', 'main'), ('device', 'udid', 'other')]:
            context = fixture()
            context[location][key] = value
            with self.assertRaises(ValueError):
                validate(context)
        for key, value in [('account_kind', 'main'), ('character_id', 'bad'), ('logical_hostnum', True),
                           ('allowed_peers', ['127.0.0.1']), ('peer_port', 8080)]:
            with self.assertRaises(ValueError):
                validate(dict(fixture(), **{key: value}))

    def test_nested_snapshot_ignores_name_only_and_bounds_depth(self):
        prop = byte_field(3, byte_field(4, TOKEN.encode()))
        nested = byte_field(4, byte_field(2, prop))
        aoi = b'\x08\x7b' + byte_field(3, nested)
        self.assertEqual(list(creation_token_values(byte_field(6, aoi))), [TOKEN.encode()])
        self.assertEqual(list(token_properties(b'\x08\x7b' + byte_field(3, byte_field(2, byte_field(4, TOKEN.encode()))))), [])
        for _ in range(10):
            prop = byte_field(4, byte_field(2, prop))
        with self.assertRaises(ValueError):
            list(token_properties(b'\x08\x7b' + byte_field(3, prop)))

    def test_native_reconnect_preserves_binary_key_and_omits_hostnum(self):
        key = b'\xff\x80opaque'
        rows, _ = fields(avatar_reconnection(PID, PID, key)[6:])
        self.assertEqual(next(v for n, w, v in rows if n == 5), key)
        self.assertNotIn(7, [n for n, _, _ in rows])

    def test_native_login_returns_only_validated_owned_token(self):
        prop = byte_field(4, byte_field(2, byte_field(3, byte_field(4, TOKEN.encode()))))
        snapshot = byte_field(6, b'\x08\x7b' + byte_field(3, prop))
        events = [(12, owner(b'Account')),
                  (10, callback('set_login_info', [{'uid': 'test-uid'}])),
                  (10, callback('show_player', [{'avatars': [{'id': PID, 'remove_ts': 0}]}])),
                  (12, owner(b'PlayerAvatar', snapshot=snapshot))]
        class FakeSession:
            def __init__(self, context):
                self.deadline = time.monotonic() + 10
                self.events = iter(events)
                self.sent = []
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def send(self, packet):
                self.sent.append(packet)
            def receive(self):
                return next(self.events)
        with patch('wwm_auth.provider.Session', FakeSession), patch('wwm_auth.provider.validate_token', return_value=(True, {'http_status': 200})) as check:
            token, report = retrieve_token(fixture())
        self.assertEqual(token, TOKEN)
        self.assertEqual(report['sdk_requests'], 0)
        self.assertEqual(report['steam_calls'], 0)
        check.assert_called_once_with(TOKEN.encode(), fixture())

    def test_cache_is_bound_to_context_and_cannot_reuse_rejected_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'context.json'
            context = fixture()
            write_private(path, context)
            cache = path.parent / 'token-cache.json'
            write_private(cache, {'token': TOKEN, 'obtained_at': time.time(), 'context_sha256': fingerprint(context)})
            self.assertIsNone(read_cache(cache, 'different-context', 240))
            with patch.dict('os.environ', {'WWM_AUTH_CONTEXT': str(path)}), patch('wwm_auth.provider.retrieve_token', return_value=(TOKEN, {'status': 'test'})) as fetch:
                self.assertEqual(get_token(), TOKEN)
                fetch.assert_not_called()
                self.assertEqual(get_token(force=True, rejected_token='different-old-token'), TOKEN)
                fetch.assert_not_called()
                self.assertEqual(get_token(force=True, rejected_token=TOKEN), TOKEN)
                fetch.assert_called_once()

    def test_provider_failure_cooldown_prevents_login_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'context.json'
            write_private(path, fixture())
            with patch.dict('os.environ', {'WWM_AUTH_CONTEXT': str(path)}), patch('wwm_auth.provider.retrieve_token', side_effect=TimeoutError) as fetch:
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        get_token()
                self.assertEqual(fetch.call_count, 1)
            self.assertFalse((path.parent / 'provider.lock').exists())

    def test_failure_diagnostics_preserve_safe_reason_and_hide_secrets(self):
        from wwm_auth.service import failure_reason
        self.assertEqual(failure_reason(ValueError('Game connection closed')), 'Game connection closed')
        self.assertNotIn('private-secret', failure_reason(ValueError('private-secret')))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'context.json'
            write_private(path, fixture())
            with patch.dict('os.environ', {'WWM_AUTH_CONTEXT': str(path)}), patch('wwm_auth.provider.retrieve_token', side_effect=ValueError('Game connection closed')) as fetch:
                with self.assertRaisesRegex(RuntimeError, 'Game connection closed'):
                    get_token()
                with self.assertRaisesRegex(RuntimeError, r'retry available in .*Game connection closed'):
                    get_token()
                self.assertEqual(fetch.call_count, 1)

    def test_runtime_imports_from_standalone_copy_without_research(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(ROOT / 'wwm_auth', Path(directory) / 'wwm_auth')
            result = subprocess.run([sys.executable, '-c',
                "import sys; import wwm_auth.provider, wwm_auth.service; assert not any('mitmproxy' in x or 'bootstrap_test' in x for x in sys.modules)"],
                cwd=directory, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_explicit_request_token_bypasses_managed_provider(self):
        # Compile just the header helper; do not import Discord/settings or .env.
        tree = ast.parse((ROOT / 'utility/wwm.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_default_headers')
        async def fail():
            raise AssertionError('Provider must not be called for an explicit token')
        scope = dict(WWM_HOST='example.invalid', WWM_UID='test', WWM_AUTH_MODE='saved_login', generate_token=fail)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<header-helper>', 'exec'), scope)
        headers = asyncio.run(scope['get_default_headers'](token_override=TOKEN))
        self.assertEqual(headers['h72-ms-token'], TOKEN)

    def api_scope(self, statuses):
        tree = ast.parse((ROOT / 'utility/wwm.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in ('get_default_headers', '_wwm_api_post')]
        class Reply:
            def __init__(self, status):
                self.status = status
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def read(self):
                return msgpack.packb({'code': 0, 'result': {}}, use_bin_type=True)
        calls = []
        sequence = iter(statuses)
        class Client:
            def post(self, url, **kwargs):
                calls.append({'headers': dict(kwargs['headers']), 'payload': msgpack.unpackb(kwargs['data'], raw=False)})
                return Reply(next(sequence))
        scope = dict(WWM_HOST='example.invalid', WWM_UID='legacy-account', WWM_AUTH_MODE='saved_login',
                     asyncio=asyncio, aiohttp=aiohttp, msgpack=msgpack, logger=Mock(),
                     Dict=typing.Dict, Any=typing.Any, Optional=typing.Optional,
                     generate_token=AsyncMock(return_value=TOKEN), get_session=AsyncMock(return_value=Client()))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<api-helper>', 'exec'), scope)
        return scope, calls

    def test_managed_api_refreshes_once_and_does_not_mutate_payload(self):
        scope, calls = self.api_scope([403, 200])
        original = {'uid': 'legacy-account', 'fields': ['base']}
        replacement = '1792300001:abcdef0123456789abcdef0123456789'
        with patch('wwm_auth.service.request_identity', return_value=PID + '_callback'), patch('wwm_auth.service.get_token_async', new_callable=AsyncMock, return_value=replacement) as refresh:
            result = asyncio.run(scope['_wwm_api_post']('https://example.invalid', original))
        self.assertEqual(result['code'], 0)
        self.assertEqual(original['uid'], 'legacy-account')
        self.assertEqual(calls[0]['payload']['uid'], 'callback')
        self.assertEqual(calls[0]['headers']['h72-ms-uid'], PID + '_callback')
        self.assertEqual(calls[1]['headers']['h72-ms-token'], replacement)
        refresh.assert_awaited_once_with(force=True, rejected_token=TOKEN)

    def test_explicit_api_token_is_not_replaced_after_rejection(self):
        scope, calls = self.api_scope([403])
        with patch('wwm_auth.service.get_token_async', new_callable=AsyncMock) as refresh:
            result = asyncio.run(scope['_wwm_api_post']('https://example.invalid', {'uid': 'custom'}, token=TOKEN))
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        refresh.assert_not_awaited()
        scope['generate_token'].assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
