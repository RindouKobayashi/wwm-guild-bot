import unittest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from extract_native_protocol_schema import fields
from gate_handshake_codec import build_fresh_session, envelope, varint, validate_session_key_ok


class GateHandshakeTests(unittest.TestCase):
    def test_key_ack_is_method_one_not_frame_ordinal_zero(self):
        self.assertTrue(validate_session_key_ok(envelope(1)))
        for invalid in (envelope(0), envelope(2), envelope(1, b'bad'), b''):
            with self.assertRaises(ValueError):
                validate_session_key_ok(invalid)

    @classmethod
    def setUpClass(cls):
        # Synthetic local key only: no game private key or recorded secret.
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def test_fresh_handshake_decrypts_to_required_schema(self):
        result = build_fresh_session(self.private.public_key(), -123, enable_zstd=True)
        self.assertEqual(result['seed_request'], b'\x02\x00\x00\x00\x00\x00')
        self.assertEqual(len(result['session_key_request']), 265)
        rows, used = fields(result['session_key_request'][6:])
        encrypted = rows[0][2]
        plain = self.private.decrypt(encrypted, padding.OAEP(
            mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
        values, used = fields(plain)
        self.assertEqual(used, len(plain))
        value = {n: v for n, w, v in values}
        self.assertEqual(value[2], result['session_key'])
        self.assertEqual(value[3], (1 << 64) - 123)
        self.assertEqual(value[6], 1)
        self.assertTrue(2 <= len(value[1]) <= 9 and 2 <= len(value[4]) <= 9)

    def test_keys_are_fresh_and_directions_independent(self):
        one = build_fresh_session(self.private.public_key(), 1)
        two = build_fresh_session(self.private.public_key(), 1)
        self.assertNotEqual(one['session_key'], two['session_key'])
        self.assertNotEqual(one['session_key_request'], two['session_key_request'])
        message = envelope(2, b'synthetic')
        ciphertext = one['send_cipher'].apply(message)
        self.assertEqual(one['receive_cipher'].apply(ciphertext), message)

    def test_unsupported_crypto_and_invalid_seed(self):
        for seed, crypto in [(0, 1), (1 << 63, 0), (-(1 << 63) - 1, 0)]:
            with self.assertRaises(ValueError):
                build_fresh_session(self.private.public_key(), seed, crypto)

    def test_protobuf_and_envelope_bounds(self):
        self.assertEqual(varint(300), b'\xac\x02')
        with self.assertRaises(ValueError):
            varint(-1)
        with self.assertRaises(ValueError):
            envelope(65536)


if __name__ == '__main__':
    unittest.main()
