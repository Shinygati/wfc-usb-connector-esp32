"""DNS selection, wire validation and explicit upstream forwarding."""
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'proto'))
from dns_config import DNS_PRESETS, normalize_dns
from dns_proxy import dns_name, forward_query, short_reply

QUERY = bytes.fromhex('abcd01000001000000000000') + b'\x03nas\x0cnintendowifi\x03net\0\0\x01\0\x01'
REPLY = QUERY[:2] + bytes.fromhex('81800001000100000000') + QUERY[12:] + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + socket.inet_aton('178.62.43.212')


class DNSConfigTests(unittest.TestCase):
    def test_preset_addresses_and_custom_input(self):
        for _, address, _ in DNS_PRESETS:
            self.assertEqual(normalize_dns(address), address)
        self.assertEqual(normalize_dns(' 192.168.1.1 '), '192.168.1.1')
        for value in (None, '', '1.2.3', 'example.com', '::1', '0.0.0.0', '224.1.1.1', '255.255.255.255'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_dns(value)


class DNSProxyTests(unittest.TestCase):
    def forward(self, reply):
        with patch('dns_proxy.socket.socket') as factory, patch('dns_proxy.socket.getaddrinfo') as system:
            sock = factory.return_value.__enter__.return_value
            sock.recv.return_value = reply
            result = forward_query(QUERY, '178.62.43.212')
            sock.connect.assert_called_once_with(('178.62.43.212', 53))
            sock.send.assert_called_once_with(QUERY)
            system.assert_not_called()
            return result

    def test_reply_and_nxdomain_are_preserved_without_system_fallback(self):
        self.assertEqual(self.forward(REPLY), REPLY)
        negative = short_reply(QUERY, rcode=3)
        self.assertEqual(self.forward(negative), negative)

    def test_wrong_id_question_or_response_flag_is_rejected(self):
        for reply in (b'x', b'xx' + REPLY[2:], REPLY.replace(b'nas', b'bad'), QUERY):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                self.forward(reply)

    def test_oversized_reply_is_truncated_to_guest_mtu(self):
        reply = self.forward(REPLY + b'\0' * 1500)
        self.assertTrue(reply[2] & 2)
        self.assertEqual(reply[6:12], b'\0' * 6)
        self.assertEqual(reply[12:], QUERY[12:])

    def test_timeout_does_not_fall_back_to_windows(self):
        with patch('dns_proxy.socket.socket') as factory, patch('dns_proxy.socket.getaddrinfo') as system:
            factory.return_value.__enter__.return_value.recv.side_effect = TimeoutError('timeout')
            with self.assertRaises(TimeoutError):
                forward_query(QUERY, '192.0.2.53')
            system.assert_not_called()

    def test_compression_loops_and_malformed_queries_are_rejected(self):
        self.assertEqual(dns_name(REPLY, len(QUERY))[0], 'nas.nintendowifi.net')
        with self.assertRaises(ValueError):
            dns_name(b'\xc0\0', 0)
        for query in (b'', QUERY[:13], QUERY[:12] + b'\xc0\x0c', REPLY):
            with self.subTest(query=query), self.assertRaises(ValueError):
                short_reply(query)


if __name__ == '__main__':
    unittest.main()
