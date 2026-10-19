import ctypes
import struct
import unittest
from probe_steam_ticket import CallbackMsg, ticket_summary


def payload(handle=7, result=1, length=240):
    return struct.pack('<Iii', handle, result, length) + b'X' * 2560


class SteamTicketTests(unittest.TestCase):
    def test_windows_x64_layout(self):
        self.assertEqual(CallbackMsg.data.offset, 8)
        self.assertEqual(CallbackMsg.size.offset, 16)
        self.assertEqual(ctypes.sizeof(CallbackMsg), 24)

    def test_success_is_redacted(self):
        result = ticket_summary(payload(), 7)
        self.assertEqual(result['ticket_bytes'], 240)
        self.assertNotIn('XXXX', str(result))

    def test_rejects_wrong_handle_size_and_length(self):
        for value in (payload(handle=8), payload()[:-1], payload(length=0), payload(length=2561)):
            with self.assertRaises(ValueError):
                ticket_summary(value, 7)

    def test_failure_is_not_success(self):
        self.assertEqual(ticket_summary(payload(result=2), 7)['status'], 'steam_ticket_failed')


if __name__ == '__main__':
    unittest.main()
