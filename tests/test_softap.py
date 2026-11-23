import json
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'proto'))
from ap import SoftAP, save_json, session_event
from wifi import BCAST, RATES, WEPTx, elements, header, ie, rc4, wep_decrypt
from rt2570 import RT2570

MAC = bytes.fromhex('001122334455')
OTHER = bytes.fromhex('001122334466')


class Radio:
    mac = bytes.fromhex('021122334455')

    def __init__(self):
        self.sent = []

    def tx(self, frame, **options):
        self.sent.append((frame, options))

    def set_beacon(self, frame):
        self.beacon = frame


class SoftAPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.radio = Radio()
        self.ap = SoftAP(self.radio, state=self.temp.name, logger=lambda _: None)

    def allow(self, mac=MAC):
        save_json(Path(self.temp.name) / 'allow.json', [mac.hex(':')])
        self.ap.reload_permissions()

    def test_desktop_telemetry_does_not_expose_key_material(self):
        self.allow()
        self.auth()
        self.assoc()
        event = session_event(self.ap)
        self.assertEqual(event['stations'], [{'mac': MAC.hex(':'), 'associated': True}])
        self.assertEqual(set(event), {'event', 'channel', 'counters', 'network', 'stations'})
        payload = json.dumps(event)
        self.assertNotIn(self.ap.ssid.decode('ascii'), payload)
        self.assertNotIn(self.ap.key.hex(), payload)

    def probe(self, ssid=None):
        if ssid is None:
            ssid = b'NWCUSBAP\0\x01\0\0' + 'TEST'.encode('utf-16le').ljust(20, b'\0')
        self.ap.handle(header(0x40, BCAST, MAC, BCAST) + ie(0, ssid) + ie(1, RATES))

    def auth(self):
        self.ap.handle(header(0xb0, self.radio.mac, MAC, self.radio.mac) + struct.pack('<HHH', 0, 1, 0))

    def assoc(self, ssid=None):
        body = struct.pack('<HH', 0x11, 10) + ie(0, ssid or self.ap.response_ssid(MAC)) + ie(1, RATES)
        self.ap.handle(header(0, self.radio.mac, MAC, self.radio.mac) + body)

    def test_pending_probe_hides_key_material(self):
        self.probe()
        frame, options = self.radio.sent[-1]
        ssid = dict(elements(frame[36:]))[0]
        self.assertEqual(frame[0], 0x50)
        self.assertEqual(ssid[:8], b'NWCUSBAP')
        self.assertEqual(ssid[9] & 1, 0)
        self.assertEqual(ssid[12:], b'\0' * 20)
        self.assertEqual(self.ap.pending[MAC.hex(':')]['name'], 'TEST')
        self.assertFalse(options['timestamp'])  # TSF is already in the body

    def test_permission_reload_releases_registration(self):
        self.probe()
        self.allow()
        self.probe()
        ssid = dict(elements(self.radio.sent[-1][0][36:]))[0]
        self.assertEqual(ssid[9] & 1, 1)
        self.assertEqual(ssid[12:], self.ap.ssid[12:])
        self.assertEqual(len(ssid), 32)

    def test_wildcard_and_other_network_probes_ignored(self):
        for ssid in (b'', b'Home', b'NWCUSBAP' + b'x' * 25):
            self.probe(ssid)
        self.assertFalse(self.radio.sent)

    def test_unauthorized_auth_rejected(self):
        self.auth()
        self.assertNotEqual(struct.unpack_from('<HHH', self.radio.sent[-1][0], 24)[2], 0)
        self.assertNotIn(MAC, self.ap.stations)

    def test_open_auth_association_and_revoke(self):
        self.allow()
        self.auth()
        self.assoc()
        self.assertTrue(self.ap.stations[MAC].associated)
        response = self.radio.sent[-1][0]
        self.assertEqual(struct.unpack_from('<HHH', response, 24), (0x11, 0, 0xc001))
        save_json(Path(self.temp.name) / 'allow.json', [])
        self.ap.reload_permissions()
        self.assertNotIn(MAC, self.ap.stations)

    def test_wrong_ssid_association_rejected(self):
        self.allow()
        self.auth()
        self.assoc(b'wrong network')
        self.assertFalse(self.ap.stations[MAC].associated)

    def test_shared_key_authentication(self):
        self.allow()
        first = struct.pack('<HHH', 1, 1, 0)
        self.ap.handle(header(0xb0, self.radio.mac, MAC, self.radio.mac) + first)
        challenge = dict(elements(self.radio.sent[-1][0][30:]))[16]
        self.assertEqual(len(challenge), 128)
        answer = struct.pack('<HHH', 1, 3, 0) + ie(16, challenge)
        protected = WEPTx(self.ap.key).encrypt(answer)
        self.ap.handle(header(0x40b0, self.radio.mac, MAC, self.radio.mac) + protected)
        self.assertTrue(self.ap.stations[MAC].authed)
        self.assertEqual(struct.unpack_from('<HHH', self.radio.sent[-1][0], 24), (1, 4, 0))

    def test_bad_shared_key_challenge_rejected(self):
        self.allow()
        self.ap.authenticate(MAC, struct.pack('<HHH', 1, 1, 0), False)
        protected = WEPTx(self.ap.key).encrypt(struct.pack('<HHH', 1, 3, 0) + ie(16, b'x' * 128))
        self.ap.authenticate(MAC, protected, True)
        self.assertFalse(self.ap.stations[MAC].authed)

    def test_retried_auth_keeps_same_challenge(self):
        self.allow()
        body = struct.pack('<HHH', 1, 1, 0)
        self.ap.authenticate(MAC, body, False, sequence=64)
        first = self.ap.stations[MAC].challenge
        self.ap.authenticate(MAC, body, False, sequence=64)
        self.assertEqual(first, self.ap.stations[MAC].challenge)
        self.ap.authenticate(MAC, body, False, sequence=80)
        self.assertNotEqual(first, self.ap.stations[MAC].challenge)

    def test_wep_ethernet_roundtrip_and_duplicate(self):
        self.allow()
        self.auth()
        self.assoc()
        received = []
        self.ap.network = type('Network', (), {'input': staticmethod(received.append)})()
        payload = b'\xaa\xaa\x03\0\0\0\x08\x00' + b'payload'
        encrypted = WEPTx(self.ap.key).encrypt(payload)
        frame = header(0x4108, self.radio.mac, MAC, OTHER, sequence=123) + encrypted
        self.ap.handle(frame)
        self.ap.handle(header(0x4908, self.radio.mac, MAC, OTHER, sequence=123) + encrypted)
        self.assertEqual(received, [OTHER + MAC + b'\x08\x00payload'])
        self.ap.output(MAC + OTHER + b'\x08\x00reply')
        reply = self.radio.sent[-1][0]
        self.assertEqual(reply[4:10], MAC)
        self.assertEqual(wep_decrypt(self.ap.key, reply[24:]), b'\xaa\xaa\x03\0\0\0\x08\x00reply')

    def test_sleep_queue_and_ps_poll(self):
        self.allow()
        self.auth()
        self.assoc()
        self.ap.running = True
        self.ap.handle(header(0x1148, self.radio.mac, MAC, OTHER))
        self.ap.output(MAC + OTHER + b'\x08\x00reply')
        self.assertEqual(len(self.ap.stations[MAC].queued), 1)
        tim = dict(elements(self.ap.beacon[36:]))[5]
        self.assertEqual(tim[3] & 2, 2)
        self.ap.handle(struct.pack('<HH6s6s', 0xa4, 0xc001, self.radio.mac, MAC))
        self.assertFalse(self.ap.stations[MAC].queued)
        self.assertEqual(dict(elements(self.ap.beacon[36:]))[5][3], 0)

    def test_state_survives_restart(self):
        self.allow()
        other = SoftAP(Radio(), state=self.temp.name, logger=lambda _: None)
        self.assertEqual(other.ssid, self.ap.ssid)
        self.assertIn(MAC, other.allowed)

    def test_malformed_frames_do_not_crash_or_grant_access(self):
        rng = random.Random(5)
        for size in range(256):
            self.ap.handle(rng.randbytes(size))
            self.ap.handle(header(0x40, BCAST, MAC, BCAST) + rng.randbytes(size))
        self.assertFalse(self.ap.allowed)
        self.assertFalse(self.ap.stations)


class CodecTests(unittest.TestCase):
    def test_cck_tx_plcp_uses_rate_index_not_rx_signal(self):
        self.assertEqual(RT2570._plcp(100, 2), (0, 4, 64, 3, 0))
        for rate, index in ((2, 0), (4, 1), (11, 2), (22, 3)):
            self.assertEqual(RT2570._plcp(100, rate)[0], index)
        self.assertEqual(RT2570._plcp(100, 12), (11, 4, 40, 1, 1))

    def test_rc4_known_vector(self):
        self.assertEqual(rc4(b'Key', b'Plaintext').hex(), 'bbf316e8d940af0ad3')

    def test_wep_rejects_corruption_and_wrong_key(self):
        key = bytes(range(13))
        encrypted = WEPTx(key).encrypt(b'test message')
        self.assertEqual(wep_decrypt(key, encrypted), b'test message')
        self.assertIsNone(wep_decrypt(b'x' * 13, encrypted))
        self.assertIsNone(wep_decrypt(key, encrypted[:-1] + bytes((encrypted[-1] ^ 1,))))
        for size in range(8):
            self.assertIsNone(wep_decrypt(key, encrypted[:size]))

    def test_elements_reject_truncation(self):
        for data in (b'\0', b'\0\x20short'):
            with self.assertRaises(ValueError):
                elements(data)

    def test_usb_descriptor_alignment(self):
        dev = RT2570.__new__(RT2570)
        for size in (24, 25, 491, 492, 493, 1004, 2432):
            result = dev.build_tx(b'x' * size)
            self.assertEqual(len(result) % 2, 0)
            self.assertNotEqual(len(result) % 512, 0)
            self.assertEqual((int.from_bytes(result[:4], 'little') >> 16) & 0xfff, size)


if __name__ == '__main__':
    unittest.main()
