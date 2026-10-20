"""A2S protocol tests without opening a real network socket."""
import unittest
from unittest.mock import MagicMock, patch

from scripts import query


def info_packet():
    return (b'\xff\xff\xff\xffI\x11' + b'Test Server\x00Genesis\x00ark\x00ARK\x00'
            + b'\x00\x00' + bytes([2, 13, 0]) + b'dl' + bytes([1, 1]))


class QueryTests(unittest.TestCase):
    def test_parse_info(self):
        result = query.parse_info(info_packet())
        self.assertEqual(result['players'], 2)
        self.assertEqual(result['max_players'], 13)
        self.assertTrue(result['password_required'])
        self.assertEqual(result['map'], 'Genesis')

    def test_rejects_truncated_split_and_unexpected_packets(self):
        for data in (b'', b'\xff\xff\xff\xfe', b'\xff\xff\xff\xffI', info_packet()[:-1]):
            with self.subTest(data=data), self.assertRaises(query.QueryError):
                query.parse_info(data)

    def test_challenge_is_repeated_with_exact_challenge_bytes(self):
        socket = MagicMock()
        socket.recv.side_effect = [b'\xff\xff\xff\xffA1234', info_packet()]
        with patch('scripts.query.socket.socket') as factory:
            factory.return_value.__enter__.return_value = socket
            result = query.query_server('127.0.0.1', 27025, timeout=2)
        self.assertEqual(result['players'], 2)
        socket.connect.assert_called_once_with(('127.0.0.1', 27025))
        sent = socket.send.call_args_list
        self.assertEqual(sent[1].args[0], sent[0].args[0] + b'1234')


if __name__ == '__main__':
    unittest.main()
