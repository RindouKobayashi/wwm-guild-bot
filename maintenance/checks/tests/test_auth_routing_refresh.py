"""Offline rolling-update routing checks; no account/network requests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from wwm_auth import routing
from test_portable_wwm_auth import fixture


def line(host, hostnum, api=routing.PRODUCTION_API):
    return f'production 1001 10001 1 10 4 logical-server {host}:4000:8:10 {hostnum} {api} 1\n'.encode()


class RoutingRefreshTests(unittest.TestCase):
    def snapshots(self):
        return {'A': line('8.8.8.8', 15401, 'h72naxx2gb-ms-preonline.easebar.com'),
                'B': line('1.1.1.1', 10401) + line('9.9.9.9', 12401)}

    def test_production_slot_switch_preserves_credentials_and_host_numbers(self):
        old = fixture()
        new, report = routing.candidate(old, self.snapshots())
        self.assertEqual(report['slot'], 'B')
        self.assertEqual(new['peer_host'], '1.1.1.1')
        self.assertEqual(new['allowed_peers'], ['1.1.1.1', '9.9.9.9'])
        for key in old.keys() - {'peer_host', 'peer_port', 'allowed_peers'}:
            self.assertEqual(new[key], old[key])
        self.assertEqual(old, fixture())

    def test_preonline_only_does_not_migrate_hostnum(self):
        with self.assertRaisesRegex(ValueError, 'absent'):
            routing.candidate(fixture(), {'A': self.snapshots()['A']})

    def test_ambiguous_production_slots_stop(self):
        snapshots = {'A': line('8.8.8.8', 10401), 'B': line('1.1.1.1', 10401)}
        with self.assertRaisesRegex(ValueError, 'disagree'):
            routing.candidate(fixture(), snapshots)

    def test_invalid_private_endpoints_and_wrong_schema_stop(self):
        for data in (line('127.0.0.1', 10401), b'wrong schema', line('1.1.1.1', 10401)*2):
            with self.assertRaises(ValueError):
                routing.parse_public_list(data)

    def test_install_preserves_exact_original_and_existing_token_cache(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'context.json'
            original = json.dumps(fixture(), indent=2).encode()
            path.write_bytes(original)
            path.chmod(0o600)
            cache = path.parent / 'token-cache.json'
            cache.write_text('old private cache preserved')
            report = routing.install(self.snapshots(), path)
            self.assertTrue(report['context_changed'])
            backup, = list((path.parent / 'backups').glob('*.json'))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(cache.read_text(), 'old private cache preserved')
            self.assertEqual(json.loads(path.read_text())['peer_host'], '1.1.1.1')
            self.assertFalse((path.parent / 'provider.lock').exists())

    def test_existing_provider_lock_preserved(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'context.json'
            path.write_text(json.dumps(fixture()))
            lock = path.parent / 'provider.lock'
            lock.write_text('another provider')
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                routing.install(self.snapshots(), path)
            self.assertEqual(lock.read_text(), 'another provider')
            self.assertEqual(json.loads(path.read_text()), fixture())


if __name__ == '__main__':
    unittest.main()
