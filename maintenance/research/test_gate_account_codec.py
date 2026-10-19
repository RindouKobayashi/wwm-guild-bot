import unittest
import hashlib
from gate_account_codec import account_rpc, created_owner, rpc_index, incoming_calls
from gate_handshake_codec import byte_field, varint
from compact_gate_messages import inner_message
from extract_native_protocol_schema import fields
from bootstrap_test_api_token import only_existing_avatar, token_properties


class AccountCodecTests(unittest.TestCase):
    def test_binary_native_reconnect_strings_remain_bytes(self):
        from bootstrap_test_api_token import msgpack
        name = b'\x10' + varint(rpc_index('on_relay_to_avatar') << 1)
        args = ['abcdefghijklmnop', '203.0.113.1', 4000, 'qrstuvwxyz012345', b'\xff\x00\x80']
        message = byte_field(3, name) + byte_field(12, msgpack.packb(args, use_bin_type=False))
        calls = list(incoming_calls(10, byte_field(3, byte_field(5, message))))
        self.assertEqual(calls[0][0], 'on_relay_to_avatar')
        self.assertEqual(calls[0][1][-1], b'\xff\x00\x80')

    def test_native_reconnect_request_and_typed_initial_token_snapshot(self):
        from probe_test_gate_connect import avatar_reconnection
        from bootstrap_test_api_token import creation_token_values
        frame = avatar_reconnection('abcdefghijklmnop', 'qrstuvwxyz012345', b'\xff\x80')
        rows, _ = fields(frame[6:])
        self.assertEqual(dict((n, v) for n, w, v in rows)[2], 1)
        self.assertNotIn(7, [n for n, w, v in rows])
        with self.assertRaises(ValueError):
            avatar_reconnection('short', 'qrstuvwxyz012345', b'key')
        token = b'1792300000:0123456789abcdef0123456789abcdef'
        snapshot = byte_field(6, byte_field(3, byte_field(3, byte_field(4, token))))
        self.assertEqual(list(creation_token_values(snapshot)), [token])
        self.assertEqual(list(creation_token_values(byte_field(5, token))), [])

    def test_no_argument_handover_callback_and_restricted_confirmation(self):
        name = b'\x10' + varint(rpc_index('try_to_relay_other') << 1)
        body = byte_field(3, byte_field(5, byte_field(3, name)))
        self.assertEqual(list(incoming_calls(10, body)), [('try_to_relay_other', [])])
        owner = {'class': b'Account', 'id': b'abcdefghijklmnop', 'guid': 1}
        self.assertTrue(account_rpc(owner, 'confirm_relay_other', [1]))
        for args in ([], [2], [True], [1, 2]):
            with self.assertRaises(ValueError):
                account_rpc(owner, 'confirm_relay_other', args)
    def test_login_failure_callback_is_not_silently_discarded(self):
        from bootstrap_test_api_token import msgpack
        method = b'\x10' + varint(rpc_index('login_fail') << 1)
        message = byte_field(3, method) + byte_field(12, msgpack.packb(['private text', 12345, 'private data'], use_bin_type=True))
        body = byte_field(3, byte_field(5, message))
        calls = list(incoming_calls(10, body))
        self.assertEqual(calls[0][0], 'login_fail')
        self.assertEqual(calls[0][1][1], 12345)

    def test_native_device_identity_guard_and_player_field_exclusion(self):
        from test_native_login_context import device_from_row
        expected = hashlib.sha256(b'test-sdk-id').hexdigest()
        auth = {'udid': 'device-one', 'deviceid': 'sdk-device-one'}
        row = {'sdk_uid': 'test-sdk-id', 'udid': 'device-one', 'unisdk_deviceid': 'sdk-device-one',
               'account_id': 'test-account', 'aid': 'test-aid', 'engine_ver': 'version',
               'patch_ver': 'patch', 'app_ver': 'app', 'role_name': 'exclude',
               'role_id': 'exclude', 'sessionid': 'exclude', 'is_known_device': True}
        device = device_from_row(row, expected, auth, 12401, 'test-steam-id')
        self.assertEqual(device['server'], 12401)
        for key in ('role_name', 'role_id', 'sessionid', 'is_known_device'):
            self.assertNotIn(key, device)
        self.assertIsNone(device_from_row(dict(row, sdk_uid='main'), expected, auth, 12401, 'test-steam-id'))
        self.assertIsNone(device_from_row(dict(row, udid='other-device'), expected, auth, 12401, 'test-steam-id'))

    def test_dynamic_owner_and_skipped_large_avatar_properties(self):
        body = byte_field(1, b'PlayerAvatar') + byte_field(2, b'abcdefghijklmnop')
        body += b'\x18' + varint(123) + byte_field(6, b'x' * (2 * 1024 * 1024))
        owner = created_owner(body)
        self.assertEqual(owner['guid'], 123)
        self.assertEqual(owner['class'], b'PlayerAvatar')

    def test_duplicate_identity_and_truncated_props_fail_closed(self):
        body = byte_field(1, b'Account') + byte_field(2, b'abcdefghijklmnop') + b'\x18\x01'
        with self.assertRaises(ValueError):
            created_owner(body + b'\x18\x02')
        with self.assertRaises(ValueError):
            created_owner(body + b'\x32\x20bad')

    def test_no_gameplay_rpc_or_wrong_owner(self):
        owner = {'class': b'Account', 'id': b'abcdefghijklmnop', 'guid': 1}
        self.assertTrue(account_rpc(owner, 'sdk_login', [{}, {}, 12401]))
        for method in ('create_player', 'remove_player', 'move', 'attack'):
            with self.assertRaises(ValueError):
                account_rpc(owner, method, [])
        with self.assertRaises(ValueError):
            account_rpc(dict(owner, **{'class': b'PlayerAvatar'}), 'player_login', [])

    def test_native_caller_is_fresh_owner_and_sender_is_empty(self):
        owner = {'class': b'Account', 'id': b'abcdefghijklmnop', 'guid': 789}
        frame = account_rpc(owner, 'sdk_login', [{}, {}, 12401])
        method, rows = inner_message(frame[6:])
        self.assertEqual(method, 9)
        self.assertEqual([n for n, w, v in rows], [1, 2, 3, 5, 11, 12])
        self.assertEqual(next(v for n, w, v in rows if n == 5), owner['id'])
        self.assertEqual(next(v for n, w, v in rows if n == 11), b'')

    def test_only_one_active_existing_character_can_be_selected(self):
        self.assertEqual(only_existing_avatar([{'avatars': [{'id': 'abcdefghijklmnop', 'remove_ts': 0}]}]), 'abcdefghijklmnop')
        for avatars in ([], [{'id': 'abcdefghijklmnop'}, {'id': 'qrstuvwxyz012345'}],
                        [{'id': 'abcdefghijklmnop', 'remove_ts': 1}]):
            with self.assertRaises(ValueError):
                only_existing_avatar([{'avatars': avatars}])

    def test_token_from_property_value_only_not_arbitrary_text(self):
        token = b'1792057212:0123456789abcdef0123456789abcdef'
        prop = byte_field(3, byte_field(4, token))
        frame = b'\x08\x7b' + byte_field(3, prop)
        self.assertEqual(list(token_properties(frame)), [(123, token)])
        self.assertEqual(list(token_properties(byte_field(4, token))), [])

    def test_snapshot_dictionary_values_and_both_tokens(self):
        first = b'1792057212:0123456789abcdef0123456789abcdef'
        second = b'1792057213:abcdef0123456789abcdef0123456789'
        child = byte_field(2, byte_field(4, b'ms_request_token2')) + byte_field(3, byte_field(4, first))
        nested = byte_field(4, byte_field(2, child))
        frame = b'\x08\x7b' + byte_field(3, nested)
        frame += byte_field(3, byte_field(3, byte_field(4, second)))
        self.assertEqual(list(token_properties(frame)), [(123, first), (123, second)])
        name_only = b'\x08\x7b' + byte_field(3, byte_field(2, byte_field(4, first)))
        self.assertEqual(list(token_properties(name_only)), [])

    def test_snapshot_dictionary_depth_is_bounded(self):
        prop = byte_field(3, byte_field(4, b'1792057212:0123456789abcdef0123456789abcdef'))
        for _ in range(10):
            prop = byte_field(4, byte_field(2, prop))
        with self.assertRaises(ValueError):
            list(token_properties(b'\x08\x7b' + byte_field(3, prop)))
