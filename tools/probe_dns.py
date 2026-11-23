"""Check published WFC DNS endpoints without opening USB or changing PC settings.

Responses prove only DNS reachability, not authentication or game compatibility.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import socket
import struct
import time

SERVERS = {
    'Kaeru WFC': '178.62.43.212',
    'WiiLink DNS': '167.235.229.36',
    'WiiLink WFC': '5.161.56.11',
    'AltWFC': '172.104.88.237',
    'AltWFC alternative (historical)': '104.131.93.87',
    'Wiimmfi (Wii DNS)': '95.217.77.151',
}
HOSTS = ('conntest.nintendowifi.net', 'nas.nintendowifi.net',
         'dls1.nintendowifi.net', 'gamestats2.gs.nintendowifi.net')


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
        elif length & 0xc0 or length > 63:
            raise ValueError('Invalid DNS label')
        elif length == 0:
            return '.'.join(labels), end or offset + 1
        else:
            if offset + 1 + length > len(data):
                raise ValueError('Truncated DNS label')
            labels.append(data[offset + 1:offset + 1 + length].decode('ascii'))
            offset += length + 1
    raise ValueError('Too many DNS labels')


def parse_response(reply, identifier, hostname):
    if len(reply) < 12:
        raise ValueError('Short DNS response')
    ident, flags, questions, answers, _, _ = struct.unpack_from('!6H', reply)
    if ident != identifier or not flags & 0x8000 or flags & 0x7800 or questions != 1:
        raise ValueError('Unmatched DNS response')
    question, offset = dns_name(reply, 12)
    if question.lower() != hostname.lower() or reply[offset:offset + 4] != b'\0\x01\0\x01':
        raise ValueError('Unmatched DNS question')
    offset += 4
    records = []
    for _ in range(answers):
        name, offset = dns_name(reply, offset)
        if offset + 10 > len(reply):
            raise ValueError('Short DNS answer')
        kind, cls, ttl, size = struct.unpack_from('!HHIH', reply, offset)
        offset += 10
        if offset + size > len(reply):
            raise ValueError('Short DNS record')
        if cls == 1 and kind == 1 and size == 4:
            records.append({'name': name, 'type': 'A', 'value': socket.inet_ntoa(reply[offset:offset + 4])})
        elif cls == 1 and kind == 5:
            value, _ = dns_name(reply, offset)
            records.append({'name': name, 'type': 'CNAME', 'value': value})
        offset += size
    return {'rcode': flags & 15, 'truncated': bool(flags & 0x200), 'answers': records}


def receive_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise OSError('Connection closed before complete DNS response')
        data.extend(part)
    return bytes(data)


def probe(task):
    name, address, hostname, transport, timeout = task
    result = {'service': name, 'server': address, 'host': hostname, 'transport': transport}
    identifier = secrets.randbelow(65536)
    query = struct.pack('!6H', identifier, 0x100, 1, 0, 0, 0)
    query += b''.join(bytes([len(p)]) + p.encode('ascii') for p in hostname.split('.')) + b'\0\0\x01\0\x01'
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if transport == 'UDP' else socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((address, 53))
            if transport == 'UDP':
                sock.send(query)
                reply = sock.recv(65535)
            else:
                sock.sendall(struct.pack('!H', len(query)) + query)
                reply = receive_exact(sock, int.from_bytes(receive_exact(sock, 2), 'big'))
            result.update(parse_response(reply, identifier, hostname))
            result['status'] = 'response'
    except (OSError, ValueError) as exc:
        result['status'] = 'timeout' if isinstance(exc, TimeoutError) else 'error'
        result['error'] = str(exc)
        result['errno'] = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'runtime/dns-probes.json')
    parser.add_argument('--timeout', type=float, default=3)
    args = parser.parse_args()
    if not 0 < args.timeout <= 10:
        parser.error('--timeout must be in (0, 10]')
    tasks = [(name, address, host, transport, args.timeout) for name, address in SERVERS.items()
             for host in HOSTS for transport in ('UDP', 'TCP')]
    tasks += [('Control (Cloudflare)', '1.1.1.1', 'example.com', transport, args.timeout) for transport in ('UDP', 'TCP')]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(probe, tasks))
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for result in results:
        detail = 'rcode=%s %s' % (result['rcode'], ', '.join(a['value'] for a in result['answers'])) if result['status'] == 'response' else '%s errno=%s' % (result['status'], result['errno'])
        print('%-31s %-3s %-32s %s' % (result['service'], result['transport'], result['host'], detail), flush=True)
    print('Report: ' + str(args.output))


if __name__ == '__main__':
    main()
