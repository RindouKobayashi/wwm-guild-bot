"""Offline failure localization and TLS regressions; no account requests."""
import os
import ssl
import unittest
from unittest.mock import Mock, patch

from wwm_auth import renewal
from wwm_auth.diagnostics import stage
from wwm_auth.provider import SavedLoginExpired
from wwm_auth.service import failure_reason, safe_failure_reason
from wwm_auth.session import Session


class StageDiagnosticsTests(unittest.TestCase):
    def test_inner_stage_and_exception_type_survive(self):
        error = SavedLoginExpired('Saved login rejected or continuation unsupported')
        with self.assertRaises(SavedLoginExpired) as caught:
            with stage('steam_ticket'), stage('sdk_exchange'):
                raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(error.auth_stage, 'sdk_exchange')

    def test_private_error_text_and_untrusted_stage_are_not_reported(self):
        error = TimeoutError('secret-account-and-token')
        with self.assertRaises(TimeoutError):
            with stage('game_tcp_connect'):
                raise error
        result = failure_reason(error)
        self.assertIn('game_tcp_connect', result)
        self.assertNotIn('secret-account', result)
        self.assertTrue(safe_failure_reason(result))
        self.assertFalse(safe_failure_reason(result + ' secret'))
        error.auth_stage = 'secret-token'
        self.assertNotIn('secret', failure_reason(error))

    def test_tcp_timeout_has_connect_stage_and_closes_session(self):
        session = Session({'peer_host': 'offline.invalid', 'peer_port': 4000,
                           'public_key_pem': 'fake key'})
        with (patch('wwm_auth.session.load_pem_public_key', return_value=object()),
              patch('wwm_auth.session.socket.create_connection', side_effect=TimeoutError('private'))):
            with self.assertRaises(TimeoutError) as caught:
                session.__enter__()
        self.assertEqual(caught.exception.auth_stage, 'game_tcp_connect')
        self.assertIsNone(session.sock)

    def test_seed_timeout_is_distinct_from_connect_timeout(self):
        sock = Mock()
        sock.recv.side_effect = TimeoutError('private')
        session = Session({'peer_host': 'offline.invalid', 'peer_port': 4000,
                           'public_key_pem': 'fake key'})
        with (patch('wwm_auth.session.load_pem_public_key', return_value=object()),
              patch('wwm_auth.session.socket.create_connection', return_value=sock)):
            with self.assertRaises(TimeoutError) as caught:
                session.__enter__()
        self.assertEqual(caught.exception.auth_stage, 'game_seed')
        sock.close.assert_called_once()

    def test_sdk_timeout_is_distinct_from_steam_ticket_timeout(self):
        client = Mock()
        client.open.side_effect = TimeoutError('private-token')
        with self.assertRaises(TimeoutError) as caught:
            renewal.candidate_context({}, {'request': {}, 'metadata': {}}, b'fake-key', b'fake-ticket', client)
        self.assertEqual(caught.exception.auth_stage, 'sdk_exchange')
        self.assertNotIn('private-token', failure_reason(caught.exception))

    def test_sdk_tls_verifies_certificates_without_environment_key_logging(self):
        client = Mock()
        client.open.side_effect = TimeoutError('offline stop')
        handlers = []
        real_handler = renewal.urllib.request.HTTPSHandler
        def https_handler(*args, **kwargs):
            tls = kwargs['context']
            self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(tls.check_hostname)
            self.assertIsNone(tls.keylog_filename)
            handlers.append(tls)
            return real_handler(*args, **kwargs)
        with (patch.dict(os.environ, {'SSLKEYLOGFILE': 'Z:/unavailable/never-create.keylog'}),
              patch.object(renewal.ssl, 'create_default_context', side_effect=AssertionError('must not inherit key logging')),
              patch.object(renewal.urllib.request, 'HTTPSHandler', side_effect=https_handler),
              patch.object(renewal.urllib.request, 'build_opener', return_value=client)):
            with self.assertRaises(TimeoutError):
                renewal.candidate_context({}, {'request': {}, 'metadata': {}}, b'fake-key', b'fake-ticket')
        self.assertEqual(len(handlers), 1)


if __name__ == '__main__':
    unittest.main()
