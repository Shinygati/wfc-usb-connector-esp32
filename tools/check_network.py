"""Exercise backend DNS and libslirp HTTP with a synthetic guest; no USB needed."""
from collections import deque
import argparse
from pathlib import Path
import socket
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'proto'))
sys.path.insert(0, str(ROOT / 'tests'))
from network import SlirpNetwork
from dns_config import normalize_dns
from test_network import MAC, checksum, udp_frame

CLIENT = socket.inet_aton('192.168.77.10')
GATEWAY = socket.inet_aton('192.168.77.1')
GWMAC = bytes.fromhex('5255c0a84d01')
HOST = 'conntest.nintendowifi.net'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dns', type=normalize_dns, default='system')
    parser.add_argument('--dns-only', action='store_true', help='only test the DNS path; no HTTP request')
    parser.add_argument('--host', default=HOST, help='DNS name to query and HTTP host to test')
    args = parser.parse_args()
    if not args.dns_only and (args.dns != 'system' or args.host != HOST):
        parser.error('use --dns-only with a custom resolver or hostname')
    host = args.host.encode('idna').decode('ascii')
    if not host or any(not 0 < len(part) <= 63 for part in host.split('.')):
        parser.error('invalid DNS hostname')
    received = deque()
    net = SlirpNetwork(received.append, dns_upstream=args.dns)
    try:
        bootp = struct.pack('!BBBBIHH4s4s4s4s16s64s128s', 1, 1, 6, 0, 0x12345678,
                            0, 0x8000, b'\0'*4, b'\0'*4, b'\0'*4, b'\0'*4, MAC.ljust(16,b'\0'), b'', b'')
        for kind in (1, 3):
            options = b'\x63\x82\x53\x63\x35\x01' + bytes((kind,))
            if kind == 3:
                options += b'\x32\x04' + CLIENT + b'\x36\x04' + GATEWAY
            net.input(udp_frame((bootp + options + b'\xff').ljust(300,b'\0')))
        arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x800, 6, 4, 1, MAC, CLIENT, b'\0'*6, GATEWAY)
        net.input(b'\xff'*6 + MAC + b'\x08\x06' + arp)
        arp_reply = struct.pack('!HHBBH6s4s6s4s', 1, 0x800, 6, 4, 2, MAC, CLIENT, GWMAC, GATEWAY)
        net.input(GWMAC + MAC + b'\x08\x06' + arp_reply)
        question = b''.join(bytes((len(s),)) + s.encode() for s in host.split('.')) + b'\0\0\x01\0\x01'
        query = struct.pack('!HHHHHH', 0x2345, 0x100, 1, 0, 0, 0) + question
        net.input(udp_frame(query, CLIENT, socket.inet_aton('192.168.77.2'), 12345, 53, GWMAC))
        deadline = time.monotonic() + 10
        dns_ok = False
        while time.monotonic() < deadline and not dns_ok:
            net.poll()
            while received:
                frame = received.popleft()
                if len(frame) >= 54 and frame[12:14] == b'\x08\x00' and frame[23] == 17:
                    dns = frame[42:]
                    if dns[:2] == b'\x23\x45':
                        flags, answers = struct.unpack_from('!H', dns, 2)[0], struct.unpack_from('!H', dns, 6)[0]
                        if flags & 15 or not answers:
                            raise RuntimeError('DNS response: rcode=%d, answers=%d' % (flags & 15, answers))
                        dns_ok = True
            time.sleep(0.005)
        if not dns_ok:
            raise RuntimeError('DNS through the backend timed out: ' + str(dict(net.counts)))
        print('DNS via backend: OK (%s, upstream=%s)' % (host, args.dns), flush=True)
        if args.dns_only:
            return
        target = socket.inet_aton(socket.gethostbyname(host))
        sequence, acknowledgement = 1000, 0

        def send(flags, payload=b''):
            tcp = struct.pack('!HHIIBBHHH', 12346, 80, sequence, acknowledgement, 0x50, flags, 65535, 0, 0) + payload
            pseudo = CLIENT + target + struct.pack('!BBH', 0, 6, len(tcp))
            tcp = tcp[:16] + struct.pack('!H', checksum(pseudo + tcp)) + tcp[18:]
            ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, CLIENT, target)
            ip = ip[:10] + struct.pack('!H', checksum(ip)) + ip[12:]
            net.input(GWMAC + MAC + b'\x08\x00' + ip + tcp)

        send(2)
        sequence += 1
        response = bytearray()
        requested = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            net.poll()
            while received:
                frame = received.popleft()
                if len(frame) < 54 or frame[12:14] != b'\x08\x00' or frame[23] != 6:
                    continue
                offset = 14 + (frame[14] & 15) * 4
                tcp = frame[offset:14 + int.from_bytes(frame[16:18], 'big')]
                if len(tcp) < 20:
                    continue
                incoming_seq = int.from_bytes(tcp[4:8], 'big')
                flags = tcp[13]
                payload = tcp[(tcp[12] >> 4) * 4:]
                if flags & 4:
                    raise RuntimeError('TCP connection reset')
                if flags & 2 and not requested:
                    acknowledgement = incoming_seq + 1
                    send(16)
                    request = ('GET / HTTP/1.0\r\nHost: ' + host + '\r\n\r\n').encode()
                    send(24, request)
                    sequence += len(request)
                    requested = True
                elif incoming_seq == acknowledgement:
                    response.extend(payload)
                    acknowledgement += len(payload) + bool(flags & 1)
                    if payload or flags & 1:
                        send(16)
                if b'\r\n\r\n' in response and b'HTML Page' in response:
                    status_line = response.split(b'\r\n')[0]
                    if status_line.split()[1] != b'200':
                        raise RuntimeError('HTTP status was not 200')
                    print('TCP/HTTP via libslirp:', status_line.decode(), flush=True)
                    return
            time.sleep(0.005)
        raise RuntimeError('HTTP through libslirp timed out')
    finally:
        net.close()


if __name__ == '__main__':
    main()
