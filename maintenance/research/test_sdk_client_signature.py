import unittest
from sdk_client_signature import client_signature_candidate


class SdkSignatureTests(unittest.TestCase):
    def test_independent_openssl_vector(self):
        # Frozen result from OpenSSL dgst -sha256 -hmac synthetic-test-key.
        self.assertEqual(client_signature_candidate(
            'https://sdk.example.test/sdk/uni_sauth', b'{"synthetic":true}',
            b'synthetic-test-key'),
            '8fc195e3b0d6985211a45744099d33ab480e32efde26cf80f06e53079662ad82')

    def test_body_bytes_are_not_reserialized(self):
        url = 'https://sdk.example.test/sdk/uni_sauth'
        self.assertNotEqual(client_signature_candidate(url, b'{"a":1}', b'test'),
                            client_signature_candidate(url, b'{"a": 1}', b'test'))

    def test_host_is_not_part_of_signed_message(self):
        self.assertEqual(client_signature_candidate('https://one.example/path', b'{}', b'test'),
                         client_signature_candidate('https://two.example/path', b'{}', b'test'))

    def test_invalid_key_body_and_url(self):
        for url, body, key in [('http://sdk.example/path', b'{}', b'test'),
                               ('https://user:pass@sdk.example/path', b'{}', b'test'),
                               ('https://sdk.example/path', '{}', b'test'),
                               ('https://sdk.example/path', b'{}', b'')]:
            with self.assertRaises(ValueError):
                client_signature_candidate(url, body, key)


if __name__ == '__main__':
    unittest.main()
