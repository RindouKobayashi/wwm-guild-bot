"""Offline v3 wire signing and failure localization; no real accounts or requests."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from wwm_auth.context import write_private
from wwm_auth.diagnostics import safe_details
from wwm_auth.provider import retrieve_token, validate_token
from wwm_auth.service import get_token
from wwm_auth.signing import sign_request
from maintenance.checks.tests.test_portable_wwm_auth import TOKEN, PID, fixture, owner, callback
from maintenance.checks.tests import test_portable_wwm_auth as portable_tests
from wwm_auth.protocol import byte_field


class SigningTests(unittest.TestCase):
    def test_original_v3_binary_body_vector_and_binding(self):
        wire = bytes.fromhex('81a3756964a178')
        signature = sign_request(TOKEN, '/flk/test?x=1&x=2', wire)
        self.assertEqual(signature, 'a81490755960da84662727ec6a2593e4:1792300000')
        self.assertNotEqual(signature, sign_request(TOKEN, '/flk/test?x=2&x=1', wire))
        self.assertNotEqual(signature, sign_request(TOKEN, '/flk/test?x=1&x=2', wire + b'\x00'))
        self.assertEqual(sign_request(TOKEN.encode(), '/flk/test?x=1&x=2', wire), signature)
        for credential in ('secret', signature, '\u2603'):
            with self.assertRaisesRegex(ValueError, '^Invalid native signing credential$'):
                sign_request(credential, '/flk/test', wire)

    def test_own_character_validation_signs_exact_sent_bytes(self):
        import msgpack
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = msgpack.packb({'code': 0, 'result': {PID: {'base': {}}}})
        client = Mock()
        client.open.return_value = response
        with patch('wwm_auth.provider.urllib.request.build_opener', return_value=client):
            accepted, detail = validate_token(TOKEN, fixture())
        request = client.open.call_args.args[0]
        self.assertTrue(accepted)
        self.assertTrue(detail['requested_character_returned'])
        self.assertEqual(request.get_header('H72-ms-token'),
                         sign_request(TOKEN, '/flk/redis_player/get_players_info', request.data))
        self.assertNotEqual(request.get_header('H72-ms-token'), TOKEN)
        self.assertTrue(request.get_header('H72-ts').isdigit())

    def test_managed_raw_payload_is_signed_without_repacking(self):
        scope, calls = portable_tests.PortableTests().api_scope([200])
        wire = bytes.fromhex('81a3756964a178')
        with patch('wwm_auth.service.request_identity', return_value=PID + '_callback'):
            result = asyncio.run(scope['_wwm_api_post']('https://example.invalid/flk/test?x=1&x=2',
                                                       {'uid': 'ignored'}, raw_payload=wire))
        self.assertEqual(result['code'], 0)
        self.assertEqual(calls[0]['wire_body'], wire)
        self.assertEqual(calls[0]['headers']['h72-ms-token'],
                         'a81490755960da84662727ec6a2593e4:1792300000')

    def test_rejected_candidate_is_not_misreported_as_token_absence(self):
        prop = byte_field(3, byte_field(4, TOKEN.encode()))
        snapshot = byte_field(6, b'\x08\x7b' + byte_field(3, prop))
        events = [(12, owner(b'Account')),
                  (10, callback('set_login_info', [{'uid': 'test-uid'}])),
                  (10, callback('show_player', [{'avatars': [{'id': PID, 'remove_ts': 0}]}])),
                  (12, owner(b'PlayerAvatar', snapshot=snapshot))]
        class FakeSession:
            def __init__(self, context):
                self.deadline = time.monotonic() + 10
                self.events = iter(events)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def send(self, packet):
                pass
            def receive(self):
                try:
                    return next(self.events)
                except StopIteration:
                    error = TimeoutError('Saved login session time limit reached')
                    error.auth_stage = self.stage
                    raise error
        with (patch('wwm_auth.provider.Session', FakeSession),
              patch('wwm_auth.provider.validate_token', return_value=(False, {'http_status': 403})),
              tempfile.TemporaryDirectory() as directory):
            path = Path(directory) / 'context.json'
            write_private(path, fixture())
            with (patch.dict('os.environ', {'WWM_AUTH_CONTEXT': str(path)}),
                  patch('wwm_auth.renewal.renew', side_effect=AssertionError('Timeout must not renew Steam'))):
                with self.assertRaisesRegex(RuntimeError, 'Received token was rejected by protected API') as caught:
                    get_token(force=True)
            progress = caught.exception.auth_diagnostic
            self.assertEqual(progress['last_http_status'], 403)
            self.assertTrue(progress['owned_avatar_created'])
            self.assertEqual(progress['token_candidates'], 1)
            failure = json.loads((path.parent / 'failure.json').read_text())
            self.assertEqual(failure['stage'], 'protected_api')
            self.assertEqual(failure['diagnostic'], progress)
            self.assertNotIn(TOKEN, json.dumps(failure))
            self.assertFalse((path.parent / 'provider.lock').exists())

    def test_progress_filter_never_exports_credentials_or_arbitrary_values(self):
        value = dict(token=TOKEN, validation={'secret': TOKEN}, token_candidates=1,
                     owned_avatar_created=True, last_http_status='403 secret', last_result_code=10**50)
        self.assertEqual(safe_details(value), {'owned_avatar_created': True, 'token_candidates': 1})


if __name__ == '__main__':
    unittest.main()
