"""Offline failure/retry and identity checks; never authenticate real accounts."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wwm_auth import service, renewal
from wwm_auth.provider import SavedLoginExpired


class RenewalTests(unittest.TestCase):
    def test_sdk_wrong_account_rejected(self):
        context = {'expected_sdkuid_sha256': hashlib.sha256(b'test').hexdigest()}
        config = {'request': {}, 'metadata': {'app_id': 3564740}}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'code': 200, 'unisdk_login_json': {'sdkuid': 'wrong'}}).encode()
        class Client:
            def open(self, request, timeout): return Response()
        with self.assertRaisesRegex(ValueError, 'account mismatch'):
            renewal.candidate_context(context, config, b'fake-key', b'fake-ticket', Client())

    def run_provider(self, side_effect, renewed=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'context.json'
            original = {'version': 'old'}
            candidate = {'version': 'new'}
            path.write_text(json.dumps(original))
            for name in ('steam-headless.json', 'sdk-renewal.json'):
                (path.parent / name).write_text('{}')
            with (patch.object(service, 'context_path', return_value=path),
                  patch.object(service, 'load', side_effect=lambda *a: json.loads(path.read_text())),
                  patch('wwm_auth.provider.retrieve_token', side_effect=side_effect) as retrieve,
                  patch.object(renewal, 'renew', return_value=candidate) as renew):
                if renewed:
                    self.assertEqual(service.get_token(force=True), 'fake-token')
                    self.assertEqual(json.loads(path.read_text()), candidate)
                    self.assertEqual(len(list((path.parent / 'backups').glob('*.json'))), 1)
                    self.assertEqual(retrieve.call_count, 2)
                    renew.assert_called_once_with(original)
                else:
                    with self.assertRaises(RuntimeError): service.get_token(force=True)
                    self.assertEqual(json.loads(path.read_text()), original)
                    if isinstance(side_effect[0], SavedLoginExpired):
                        self.assertEqual(retrieve.call_count, 2)
                    else: renew.assert_not_called()
                self.assertFalse((path.parent / 'provider.lock').exists())

    def test_expired_login_renews_and_installs_only_validated_candidate(self):
        self.run_provider([SavedLoginExpired('rejected'), ('fake-token', {})])

    def test_network_failure_does_not_renew(self):
        self.run_provider([TimeoutError('network')], renewed=False)

    def test_failed_candidate_preserves_original_and_does_not_loop(self):
        self.run_provider([SavedLoginExpired('rejected'), SavedLoginExpired('still rejected')], renewed=False)


if __name__ == '__main__': unittest.main()
