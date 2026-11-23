"""Integration with the real libslirp DLL, using a synthetic Ethernet guest."""
from pathlib import Path
import socket
import struct
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'proto'))
from network import SlirpNetwork

MAC = bytes.fromhex('021122334455')
ZERO = b'\0' * 4


def checksum(data):
    data += b'\0' if len(data) & 1 else b''
    value = sum(struct.unpack('!%dH' % (len(data) // 2), data))
    while value >> 16:
        value = (value & 65535) + (value >> 16)
    return (~value) & 65535


def udp_frame(payload, source=ZERO, destination=b'\xff'*4, sport=68, dport=67, dmac=b'\xff'*6):
    udp = struct.pack('!HHHH', sport, dport, len(payload) + 8, 0) + payload
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, source, destination)
    ip = ip[:10] + struct.pack('!H', checksum(ip)) + ip[12:]
    return dmac + MAC + b'\x08\x00' + ip + udp


@unittest.skipUnless(sys.platform == 'win32' and (Path(__file__).resolve().parents[1] /
                    'runtime/ucrt64/bin/libslirp-0.dll').exists(), 'private Windows libslirp runtime required')
class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.frames = []
        self.network = SlirpNetwork(self.frames.append, logger=lambda _: None)
        self.addCleanup(self.network.close)

    def request(self, kind, requested=None):
        bootp = struct.pack('!BBBBIHH4s4s4s4s16s64s128s', 1, 1, 6, 0, 0x12345678,
                            0, 0x8000, ZERO, ZERO, ZERO, ZERO, MAC.ljust(16,b'\0'), b'', b'')
        options = b'\x63\x82\x53\x63\x35\x01' + bytes((kind,))
        if requested:
            options += b'\x32\x04' + requested + b'\x36\x04' + socket.inet_aton('192.168.77.1')
        self.network.input(udp_frame((bootp + options + b'\xff').ljust(300,b'\0')))
        self.network.poll()
        reply = next(f for f in reversed(self.frames) if f[12:14] == b'\x08\x00')
        return reply[42:]

    def test_dhcp_discover_offer_request_ack(self):
        offer = self.request(1)
        self.assertEqual(offer[0], 2)
        self.assertEqual(offer[4:8], b'\x12\x34\x56\x78')
        self.assertEqual(offer[16:20], socket.inet_aton('192.168.77.10'))
        self.assertIn(b'\x35\x01\x02', offer[240:])
        self.assertIn(b'\x06\x04' + socket.inet_aton('192.168.77.2'), offer[240:])
        ack = self.request(3, offer[16:20])
        self.assertIn(b'\x35\x01\x05', ack[240:])

    def test_gateway_arp(self):
        source = socket.inet_aton('192.168.77.10')
        target = socket.inet_aton('192.168.77.1')
        arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x800, 6, 4, 1, MAC, source, b'\0'*6, target)
        self.network.input(b'\xff'*6 + MAC + b'\x08\x06' + arp)
        self.assertEqual(self.frames[-1][20:22], b'\0\x02')
        self.assertEqual(self.frames[-1][28:32], target)

    def test_winsock_poll_does_not_request_unsupported_pollpri(self):
        self.network.fds = []
        self.network.add_poll(123, 1 | 2 | 4, None)
        self.assertEqual(self.network.fds[0].events, 0x110)

    def test_custom_dns_guest_reply_and_timeout(self):
        from test_dns import QUERY, REPLY
        client = socket.inet_aton('192.168.77.10')
        self.network.dns_upstream = socket.inet_aton('178.62.43.212')
        for error in (False, True):
            self.frames.clear()
            with patch('network.forward_query', return_value=REPLY,
                       side_effect=TimeoutError('timeout') if error else None) as forward:
                self.network.input(udp_frame(QUERY, client, self.network.dns_virtual, 12345, 53))
                deadline = time.monotonic() + 2
                while not self.frames and time.monotonic() < deadline:
                    self.network.poll()
                    time.sleep(0.001)
                forward.assert_called_once_with(QUERY, '178.62.43.212')
            self.assertTrue(self.frames)
            frame = self.frames[-1]
            self.assertEqual(frame[:6], MAC)
            self.assertEqual(frame[26:34], self.network.dns_virtual + client)
            self.assertEqual(struct.unpack_from('!HH', frame, 34), (53, 12345))
            self.assertEqual(checksum(frame[14:34]), 0)
            self.assertEqual(frame[42:44], QUERY[:2])
            self.assertEqual(frame[45] & 15, 2 if error else 0)


class DNSCodecTests(unittest.TestCase):
    def test_system_resolver_response_preserves_id_and_question(self):
        query = bytes.fromhex('abcd01000001000000000000') + b'\x04test\x07example\0\0\x01\0\x01'
        record = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.10', 0))
        with patch('network.socket.getaddrinfo', return_value=[record]):
            reply = SlirpNetwork.resolve_query(query)
        self.assertEqual(reply[:2], query[:2])
        self.assertEqual(reply[6:8], b'\0\x01')
        self.assertIn(query[12:], reply)
        self.assertEqual(reply[-4:], socket.inet_aton('192.0.2.10'))

    def test_dns_malformed_queries_are_rejected(self):
        for query in (b'', b'x'*11, b'\0'*12, bytes.fromhex('123401000001000000000000c0ff')):
            self.assertIsNone(SlirpNetwork.resolve_query(query))


if __name__ == '__main__':
    unittest.main()
