"""WinUSB SoftAP with NWCUSBAP registration and explicit console permissions.

One event loop owns USB I/O. The Ethernet backend is optional; without it this
implements registration/association, not DHCP or Internet access.
"""
import argparse
from collections import Counter, deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import struct
import time

import wfckey
from dns_config import normalize_dns
from wifi import BCAST, RATES, WEPTx, elements, header, ie, wep_decrypt

DEFAULT_STATE = Path(__file__).resolve().parent / 'state'


def log(message):
    print(time.strftime('%H:%M:%S') + ' ' + message, flush=True)


def mac_bytes(value):
    try:
        result = bytes.fromhex(value.replace(':', '').replace('-', ''))
    except ValueError as error:
        raise ValueError('invalid MAC address') from error
    if len(result) != 6 or result[0] & 1 or result == b'\0' * 6:
        raise ValueError('expected a unicast MAC address')
    return result


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def session_event(ap):
    """Public session telemetry. Never expose the SSID or WEP key."""
    return {
        'event': 'status', 'channel': ap.channel,
        'counters': dict(ap.counts),
        'network': dict(ap.network.counts) if ap.network else {},
        'stations': [
            {'mac': station.mac.hex(':'), 'associated': station.associated}
            for station in ap.stations.values()
        ],
    }


@dataclass
class Station:
    mac: bytes
    aid: int
    authed: bool = False
    associated: bool = False
    challenge: bytes = b''
    challenge_until: float = 0
    auth_request_sequence: int = -1
    asleep: bool = False
    queued: deque = field(default_factory=lambda: deque(maxlen=32))
    last_sequence: int = -1
    last_seen: float = field(default_factory=time.monotonic)


class SoftAP:
    def __init__(self, dev, channel=6, state=DEFAULT_STATE, network=None, logger=log, monitor_rx=True):
        self.dev, self.channel, self.state = dev, channel, Path(state)
        self.bssid, self.network, self.log = dev.mac, network, logger
        self.state.mkdir(parents=True, exist_ok=True)
        ssid_path = self.state / 'ssid.bin'
        if not ssid_path.exists():
            ssid_path.write_bytes(wfckey.make_ssid(secrets.SystemRandom()))
        self.ssid = ssid_path.read_bytes()
        if len(self.ssid) != 32 or not self.ssid.startswith(wfckey.MAGIC) or not self.ssid[12:].isdigit():
            raise ValueError('invalid saved SSID; preserve state and investigate')
        self.key = wfckey.ssid_key(self.ssid)
        self.wep = WEPTx(self.key)
        self.allowed, self.pending, self.stations = set(), {}, {}
        self.counts = Counter()
        self.running = False
        self.monitor_rx = monitor_rx
        self.started_ns = time.monotonic_ns()
        self.sequence = 0
        self.next_beacon = 0
        self.reload_permissions()
        save_json(self.state / 'pending.json', self.pending)

    def reload_permissions(self):
        path = self.state / 'allow.json'
        if not path.exists():
            save_json(path, [])
        allowed = {mac_bytes(value) for value in json.loads(path.read_text(encoding='utf-8'))}
        if len(allowed) > 32:
            raise ValueError('at most 32 consoles may be allowed')
        revoked = self.allowed - allowed
        for mac in revoked:
            self.stations.pop(mac, None)
        self.allowed = allowed
        if revoked:
            self.update_beacon()

    def response_ssid(self, mac):
        ssid = bytearray(self.ssid)
        if mac in self.allowed:
            ssid[9] |= 1
        else:
            ssid[9] &= 0x0e
            ssid[12:] = b'\0' * 20
        return bytes(ssid)

    def station(self, mac):
        if mac not in self.stations:
            if len(self.stations) >= 32:
                return None
            used = {s.aid for s in self.stations.values()}
            self.stations[mac] = Station(mac, next(a for a in range(1, 33) if a not in used))
        return self.stations[mac]

    def management(self, subtype, mac, body, timestamp=False):
        self.sequence = (self.sequence + 1) & 4095
        self.dev.tx(header(subtype << 4, mac, self.bssid, self.bssid,
                           sequence=self.sequence,
                           duration=314 if mac != BCAST else 0) + body,
                    ack=mac != BCAST, timestamp=False, new_seq=True)

    def beacon_body(self, ssid, tim=False):
        tsf = (time.monotonic_ns() - self.started_ns) // 1000
        body = struct.pack('<QHH', tsf, 100, 0x11)
        body += ie(0, ssid) + ie(1, RATES) + ie(3, bytes((self.channel,)))
        if tim:
            bitmap = bytearray(5)
            for station in self.stations.values():
                if station.associated and station.queued:
                    bitmap[station.aid // 8] |= 1 << (station.aid % 8)
            body += ie(5, b'\0\x01\0' + bitmap)
        return body

    def update_beacon(self):
        if self.running:
            self.beacon = header(0x80, BCAST, self.bssid, self.bssid) + self.beacon_body(b'', tim=True)

    def tick(self):
        now = time.monotonic()
        if self.running and now >= self.next_beacon:
            self.management(8, BCAST, self.beacon_body(b'', tim=True))
            self.counts['beacon_tx'] += 1
            self.next_beacon += 0.1024
            if self.next_beacon <= now:
                self.next_beacon = now + 0.1024

    def start(self):
        self.dev.radio_on(self.channel)
        self.dev.enable_ap_sync()
        # Use ordinary TX and software TSF while debugging the hardware beacon
        # guardian/generator; it must not compete for the bulk OUT endpoint.
        self.dev.modify(0x466, 0x1f, 1)
        self.dev.set_rx_filter(promisc=self.monitor_rx)
        self.running = True
        self.update_beacon()
        self.dev.start_rx()
        self.dev.led(True)
        if hasattr(self.dev, 'read'):
            self.log('CSR ' + ' '.join('%04x=%04x' % (r, self.dev.read(r)) for r in
                     (0x402, 0x404, 0x406, 0x408, 0x40a, 0x40c, 0x40e, 0x442, 0x444, 0x464, 0x466)))

    def handle(self, frame):
        if len(frame) < 10:
            return
        fc = int.from_bytes(frame[:2], 'little')
        if fc & 3:
            return
        kind, subtype = (fc >> 2) & 3, (fc >> 4) & 15
        if len(frame) >= 24 and frame[10:16] in self.allowed:
            tag = 'peer_%d_%d' % (kind, subtype)
            self.counts[tag] += 1
            if self.counts[tag] <= 3:
                self.log('RX console type=%d/%d destination=%s bssid=%s bytes=%d' %
                         (kind, subtype, frame[4:10].hex(':'), frame[16:22].hex(':'), len(frame)))
        if kind == 1 and subtype == 10 and len(frame) == 16:
            station = self.stations.get(frame[10:16])
            aid = int.from_bytes(frame[2:4], 'little') & 0x3fff
            if frame[4:10] == self.bssid and station and station.associated and station.aid == aid:
                if station.queued:
                    self.send_ethernet(station, station.queued.popleft(), force=True)
                    self.update_beacon()
            return
        if len(frame) < 24 or frame[4:10] not in (self.bssid, BCAST):
            return
        mac, body = frame[10:16], frame[24:]
        if mac[0] & 1 or mac == b'\0' * 6 or mac == self.bssid:
            return
        if kind == 0 and fc & 0x0300:
            return
        if kind == 0 and subtype == 4:
            try:
                fields = elements(body)
            except ValueError:
                return
            if not fields or fields[0][0] != 0:
                return
            ssid = fields[0][1]
            if not 8 <= len(ssid) <= 32 or not ssid.startswith(wfckey.MAGIC):
                return
            self.counts['nwc_probe'] += 1
            if self.counts['nwc_probe'] <= 3:
                self.log('NWC probe length=%d flags=%s allowed=%s saved_ssid=%s' %
                         (len(ssid), ssid[8:12].hex(), mac in self.allowed,
                          len(ssid) == 32 and ssid[12:] == self.ssid[12:]))
            if len(ssid) >= 12 and ssid[9] & 3 and mac not in self.allowed:
                label = mac.hex(':')
                if label not in self.pending and len(self.pending) < 32:
                    name = ssid[12:32].decode('utf-16le', errors='replace').split('\0')[0]
                    self.pending[label] = {'name': name, 'flags': ssid[9], 'kind': ssid[10]}
                    save_json(self.state / 'pending.json', self.pending)
                    self.log('PERMISSION PENDING %s name=%s' % (label, ascii(name)))
            self.management(5, mac, self.beacon_body(self.response_ssid(mac)), timestamp=True)
            return
        if frame[4:10] != self.bssid:
            return
        if kind == 0 and frame[16:22] != self.bssid:
            return
        if kind == 0 and subtype == 11:
            self.authenticate(mac, body, bool(fc & 0x4000), int.from_bytes(frame[22:24], 'little'))
            return
        station = self.stations.get(mac)
        if not station or mac not in self.allowed:
            return
        station.last_seen = time.monotonic()
        if kind == 0 and subtype in (0, 2):
            self.associate(station, body, subtype)
        elif kind == 0 and subtype in (10, 12):
            station.associated = False
            station.queued.clear()
            if subtype == 12:
                station.authed = False
            self.update_beacon()
        elif kind == 2 and station.associated and fc & 0x0300 == 0x0100:
            was_asleep = station.asleep
            station.asleep = bool(fc & 0x1000)
            if was_asleep and not station.asleep:
                while station.queued:
                    self.send_ethernet(station, station.queued.popleft(), force=True)
                self.update_beacon()
            if subtype == 4:
                return
            if subtype != 0 or not fc & 0x4000 or fc & 0x0400 or frame[22] & 15:
                return
            sequence = int.from_bytes(frame[22:24], 'little')
            if fc & 0x0800 and sequence == station.last_sequence:
                return
            plain = wep_decrypt(self.key, body)
            if plain is None or not plain.startswith(b'\xaa\xaa\x03\0\0\0') or len(plain) < 8:
                self.counts['bad_wep_or_llc'] += 1
                return
            station.last_sequence = sequence
            self.counts['data_rx'] += 1
            if self.network:
                self.network.input(frame[16:22] + mac + plain[6:])

    def authenticate(self, mac, body, protected, sequence=None):
        if protected:
            body = wep_decrypt(self.key, body)
            if body is None:
                return
        if len(body) < 6:
            return
        algorithm, transaction, status = struct.unpack_from('<HHH', body)
        if transaction not in (1, 3) or status:
            return
        if mac not in self.allowed:
            self.management(11, mac, struct.pack('<HHH', algorithm, transaction + 1, 13))
            return
        station = self.station(mac)
        if station is None:
            return
        result, challenge = 0, b''
        if algorithm == 0 and transaction == 1 and not protected:
            station.authed = True
        elif algorithm == 1 and transaction == 1 and not protected:
            station.authed = False
            if (sequence is None or sequence != station.auth_request_sequence or
                    time.monotonic() > station.challenge_until):
                station.challenge = secrets.token_bytes(128)
                station.challenge_until = time.monotonic() + 5
                station.auth_request_sequence = sequence
            challenge = ie(16, station.challenge)
        elif algorithm == 1 and transaction == 3 and protected:
            try:
                received = dict(elements(body[6:])).get(16)
            except ValueError:
                received = None
            station.authed = bool(station.challenge and received == station.challenge and
                                  time.monotonic() <= station.challenge_until)
            result = 0 if station.authed else 15
        else:
            result = 13 if algorithm not in (0, 1) else 14
        self.management(11, mac, struct.pack('<HHH', algorithm, transaction + 1, result) + challenge)
        self.counts['auth'] += 1
        self.log('AUTH %s algorithm=%d step=%d status=%d' % (mac.hex(':'), algorithm, transaction, result))

    def associate(self, station, body, subtype):
        offset = 10 if subtype == 2 else 4
        if len(body) < offset:
            return
        try:
            fields = dict(elements(body[offset:]))
        except ValueError:
            return
        status = 0
        if not station.authed:
            status = 9
        elif fields.get(0) not in (self.ssid, self.response_ssid(station.mac)):
            status = 1
        elif not {2, 4}.issubset({v & 127 for v in fields.get(1, b'') + fields.get(50, b'')}):
            status = 18
        station.associated = status == 0
        self.management(subtype + 1, station.mac,
                        struct.pack('<HHH', 0x11, status, 0xc000 | station.aid) + ie(1, RATES))
        self.counts['assoc'] += 1
        self.log('ASSOC %s status=%d' % (station.mac.hex(':'), status))

    def output(self, ethernet):
        if len(ethernet) < 14:
            return
        for station in list(self.stations.values()):
            if station.associated and (ethernet[0] & 1 or ethernet[:6] == station.mac):
                self.send_ethernet(station, ethernet)

    def send_ethernet(self, station, ethernet, force=False):
        if station.asleep and not force:
            station.queued.append(ethernet)
            self.update_beacon()
            return
        body = self.wep.encrypt(b'\xaa\xaa\x03\0\0\0' + ethernet[12:])
        fc = 0x4208 | (0x2000 if station.queued else 0)
        self.sequence = (self.sequence + 1) & 4095
        self.dev.tx(header(fc, station.mac, self.bssid, ethernet[6:12],
                           sequence=self.sequence, duration=314) + body, ack=True, new_seq=True)
        self.counts['data_tx'] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--channel', type=int, choices=range(1, 12), default=6)
    run.add_argument('--seconds', type=float, default=300)
    run.add_argument('--monitor-rx', action=argparse.BooleanOptionalAction, default=True,
                     help='receive broadly and isolate this BSSID in software; enabled for the tested RT2570')
    run.add_argument('--network', choices=('slirp', 'none'), default='slirp')
    run.add_argument('--dns', type=normalize_dns, default='system', help='system resolver (default), or an IPv4 upstream DNS')
    run.add_argument('--events', action='store_true', help='emit JSON session events for the desktop app')
    commands.add_parser('pending')
    commands.add_parser('stop')
    commands.add_parser('allow').add_argument('mac', type=mac_bytes)
    commands.add_parser('revoke').add_argument('mac', type=mac_bytes)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    if args.command == 'stop':
        (args.state / 'stop').write_text('stop\n', encoding='ascii')
        log('Stop requested')
        return
    if args.command == 'pending':
        path = args.state / 'pending.json'
        print(path.read_text(encoding='utf-8') if path.exists() else '{}')
        return
    if args.command in ('allow', 'revoke'):
        path = args.state / 'allow.json'
        allowed = {mac_bytes(v).hex(':') for v in json.loads(path.read_text(encoding='utf-8'))} if path.exists() else set()
        value = args.mac.hex(':')
        if args.command == 'allow':
            allowed.add(value)
        else:
            allowed.discard(value)
        if len(allowed) > 32:
            parser.error('at most 32 consoles may be allowed')
        save_json(path, sorted(allowed))
        log('%s %s' % (args.command.upper(), value))
        return
    if not 0 < args.seconds <= 86400:
        parser.error('--seconds must be in (0, 86400]')
    from rt2570 import RT2570, STA_CSR5
    dev = RT2570()
    ap = None
    try:
        (args.state / 'stop').unlink(missing_ok=True)
        dev.probe()
        ap = SoftAP(dev, args.channel, args.state, monitor_rx=args.monitor_rx)
        if args.network == 'slirp':
            from network import SlirpNetwork
            ap.network = SlirpNetwork(ap.output, logger=log, dns_upstream=args.dns)
        ap.start()
        log('AP ACTIVE channel=%d BSSID=%s; waiting for console' % (args.channel, dev.mac.hex(':')))
        if ap.network is None:
            log('Registration/association only; networking disabled with --network none')
        end = time.monotonic() + args.seconds
        reload_at = report_at = event_at = 0
        while time.monotonic() < end and not (args.state / 'stop').exists():
            ap.tick()
            now = time.monotonic()
            if now >= reload_at:
                ap.reload_permissions()
                reload_at = now + 0.5
            if now >= report_at:
                log('STATUS beacons_hw=%d counters=%s network=%s' %
                    (dev.read(STA_CSR5), dict(ap.counts), dict(ap.network.counts) if ap.network else {}))
                report_at = now + 10
            if args.events and now >= event_at:
                print('@WFC ' + json.dumps(session_event(ap)), flush=True)
                event_at = now + 1
            packet = dev.rx(10)
            if packet:
                ap.handle(packet[0])
            if ap.network:
                ap.network.poll()
    except KeyboardInterrupt:
        log('Interrupted')
    finally:
        try:
            if ap and ap.network:
                ap.network.close()
        finally:
            dev.close()
            log('RADIO OFF')


if __name__ == '__main__':
    main()
