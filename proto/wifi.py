"""802.11 and WEP codecs for the legacy connector, independent of USB."""
import secrets
import struct
import zlib

BCAST = b'\xff' * 6
RATES = b'\x82\x84\x0b\x16'


def elements(data):
    """Parse IEs, rejecting truncation."""
    result = []
    while data:
        if len(data) < 2 or len(data) < 2 + data[1]:
            raise ValueError('truncated information element')
        length = data[1]
        result.append((data[0], data[2:2 + length]))
        data = data[2 + length:]
    return result


def ie(kind, value):
    return bytes((kind, len(value))) + value


def header(fc, destination, source, bssid, sequence=0, duration=0):
    return struct.pack('<HH6s6s6sH', fc, duration, destination, source,
                       bssid, (sequence & 4095) << 4)


def rc4(key, data):
    if not key:
        raise ValueError('empty RC4 key')
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) & 255
        box[i], box[j] = box[j], box[i]
    i = j = 0
    output = bytearray()
    for value in data:
        i = (i + 1) & 255
        j = (j + box[i]) & 255
        box[i], box[j] = box[j], box[i]
        output.append(value ^ box[(box[i] + box[j]) & 255])
    return bytes(output)


def wep_decrypt(key, body):
    if len(body) < 8 or body[3] != 0:
        return None
    plain = rc4(body[:3] + key, body[4:])
    data = plain[:-4]
    if plain[-4:] != struct.pack('<I', zlib.crc32(data)):
        return None
    return data


class WEPTx:
    def __init__(self, key):
        self.key = key
        self.iv = secrets.randbelow(1 << 24)
        self.sent = 0

    def encrypt(self, data):
        if self.sent >= 1 << 24:
            raise RuntimeError('WEP IV space exhausted; restart the session')
        self.sent += 1
        self.iv = (self.iv + 1) & 0xffffff
        iv = self.iv.to_bytes(3, 'little')
        return iv + b'\0' + rc4(iv + self.key, data + struct.pack('<I', zlib.crc32(data)))
