import base64
import json
import unittest
from steam_session_wrapper import decode_steam_session, encode_steam_session, summarize_steam_session

class SteamWrapperTests(unittest.TestCase):
    def test_native_uppercase_hex(self):
        self.assertEqual(encode_steam_session(bytes.fromhex('00ffab')), 'E-steam-MDBGRkFC')

    def test_roundtrip_with_metadata(self):
        ticket = bytes(range(240))
        value = encode_steam_session(ticket, {'app_id': 'synthetic-app'})
        self.assertEqual(decode_steam_session(value), (ticket, {'app_id': 'synthetic-app'}))
        self.assertTrue(summarize_steam_session(value)['reencoded_exactly'])

    def test_minimum_ticket_and_decode_bounds(self):
        self.assertEqual(decode_steam_session(encode_steam_session(b'\x00')), (b'\x00', None))
        oversized = 'E-steam-' + base64.b64encode(b'AA' * 8193).decode()
        with self.assertRaises(ValueError):
            decode_steam_session(oversized)

    def test_no_metadata(self):
        self.assertEqual(decode_steam_session(encode_steam_session(b'synthetic')), (b'synthetic', None))

    def test_lowercase_hex_rejected(self):
        with self.assertRaises(ValueError):
            decode_steam_session('E-steam-' + base64.b64encode(b'00ffab00').decode())

    def test_invalid_channel_and_encoding(self):
        valid = encode_steam_session(b'synthetic')
        for value in (valid.replace('steam', 'other'), valid + '!', valid[:-1], 'E-steam-@@@@@@@', 'x' * 65537):
            with self.subTest(value_length=len(value)), self.assertRaises(ValueError):
                decode_steam_session(value)

    def test_summary_omits_values(self):
        ticket = b'private-ticket'
        value = encode_steam_session(ticket, {'app_id': 'private-app'})
        result = json.dumps(summarize_steam_session(value))
        for private in ('private-ticket', ticket.hex().upper(), value, 'private-app'):
            self.assertNotIn(private, result)

if __name__ == '__main__':
    unittest.main()
