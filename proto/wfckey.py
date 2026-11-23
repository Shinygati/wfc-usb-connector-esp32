"""Nintendo Wi-Fi USB Connector: SSID format and WEP-104 key derivation.

Clean reimplementation of NintendoWFCReg.exe FUN_00402550 / FUN_00404140 (v1.07).
The AP SSID is b"NWCUSBAP" + 4 spaces + 20 ASCII digits; the console derives the
13-byte WEP key from the digits, so the SSID alone is the whole shared secret.
"""
import random

MAGIC = b"NWCUSBAP"
_X1 = b"gwi'6&fs=0Nf~"
_X2 = b"%(egEr)ag(s&m"
_SBOX = bytes([10, 13, 14, 8, 9, 3, 6, 0, 12, 5, 2, 7, 11, 1, 15, 4])
_PERM = bytes([5, 1, 12, 4, 2, 3, 10, 0, 11, 7, 9, 8, 6])


def make_ssid(rng=random) -> bytes:
    digits = "".join("%05d" % rng.randrange(0x8000) for _ in range(4))
    return MAGIC + b"    " + digits.encode()


def derive_key(digits: bytes) -> bytes:
    """digits: the 20 bytes at SSID[12:32]."""
    assert len(digits) == 20
    k = bytearray(digits[i] ^ digits[13 + i % 7] for i in range(13))
    for i in range(3, 10):
        k[i] ^= digits[10 + i]
    k = bytearray(a ^ b for a, b in zip(k, _X1))
    t = bytes(k)
    for i in range(13):
        k[_PERM[i]] = t[i]
    k = bytearray(a ^ b for a, b in zip(k, _X2))
    k = bytearray(_SBOX[b >> 4] << 4 | _SBOX[b & 15] for b in k)
    k[0] ^= k[6]; k[3] ^= k[9]; k[6] = k[3] ^ k[6]; k[9] ^= k[0]; k[12] ^= k[0]
    k[1] ^= k[7]; k[4] ^= k[10]; k[7] = k[4] ^ k[7]; k[10] ^= k[1]; k[12] ^= k[1]
    k[2] ^= k[8]; k[5] ^= k[11]; k[8] = k[5] ^ k[8]; k[11] ^= k[2]; k[12] ^= k[2]
    return bytes(k)


def ssid_key(ssid: bytes) -> bytes:
    assert len(ssid) == 32 and ssid.startswith(MAGIC)
    return derive_key(ssid[12:32])
