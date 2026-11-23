"""Bounded UDP forwarding to an explicitly selected DNS service."""
import socket
import struct


def dns_name(data, offset):
    labels, end, visited = [], None, set()
    for _ in range(128):
        if offset >= len(data) or offset in visited:
            raise ValueError('Invalid DNS name')
        visited.add(offset)
        length = data[offset]
        if length & 0xc0 == 0xc0:
            if offset + 1 >= len(data):
                raise ValueError('Truncated DNS pointer')
            end = end or offset + 2
            offset = ((length & 0x3f) << 8) | data[offset + 1]
        elif length & 0xc0:
            raise ValueError('Invalid DNS label')
        elif length == 0:
            return '.'.join(labels), end or offset + 1
        else:
            if offset + 1 + length > len(data):
                raise ValueError('Truncated DNS label')
            labels.append(data[offset + 1:offset + 1 + length].decode('ascii'))
            offset += length + 1
    raise ValueError('Too many DNS labels')


def question_key(packet):
    if len(packet) < 12 or packet[2] & 0x78 or packet[4:6] != b'\0\x01':
        raise ValueError('Expected one standard DNS question')
    name, end = dns_name(packet, 12)
    if end + 4 > len(packet):
        raise ValueError('Truncated DNS question')
    return (name.lower(), packet[end:end + 4]), end + 4


def query_key(query):
    expected, end = question_key(query)
    if query[2] & 0x80:
        raise ValueError('Expected a DNS query')
    # Compression in a request could point outside the copied question.
    offset = 12
    while offset < end - 4 and query[offset]:
        if query[offset] & 0xc0:
            raise ValueError('Compressed DNS queries are not supported')
        offset += query[offset] + 1
    return expected, end


def short_reply(query, rcode=2, truncated=False):
    _, end = query_key(query)
    flags = 0x8080 | (int.from_bytes(query[2:4], 'big') & 0x0100) | rcode
    if truncated:
        flags |= 0x0200
    return query[:2] + struct.pack('!5H', flags, 1, 0, 0, 0) + query[12:end]


def forward_query(query, upstream, timeout=2):
    """Preserve service replies, including NXDOMAIN; never use a fallback DNS."""
    expected, _ = query_key(query)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((upstream, 53))
        sock.send(query)
        reply = sock.recv(65535)
    actual, _ = question_key(reply)
    if reply[:2] != query[:2] or not reply[2] & 0x80 or actual != expected:
        raise ValueError('Unmatched upstream DNS response')
    # This backend does not fragment Ethernet/IP packets or proxy TCP DNS.
    if len(reply) > 1472:
        return short_reply(query, rcode=0, truncated=True)
    return reply
