"""Userspace driver for the Ralink RT2570 (Nintendo Wi-Fi USB Connector) over libusb/WinUSB.

Register semantics follow the public rt2500usb documentation (Linux rt2x00 headers);
this is an independent implementation for AP use.
"""
import struct
import time

import libusb_package
import usb.backend.libusb1
import usb.core
import usb.util

VID, PID = 0x0411, 0x008B

# vendor requests
REQ_DEVICE_MODE, REQ_SINGLE_WRITE, REQ_MULTI_WRITE, REQ_MULTI_READ = 1, 2, 6, 7
REQ_EEPROM_READ, REQ_RX_CONTROL = 9, 12
MODE_TEST = 4
OUT, IN = 0x40, 0xC0

# CSRs
MAC_CSR0, MAC_CSR1, MAC_CSR2, MAC_CSR5, MAC_CSR8 = 0x400, 0x402, 0x404, 0x40A, 0x410
MAC_CSR9, MAC_CSR10, MAC_CSR11, MAC_CSR12 = 0x412, 0x414, 0x416, 0x418
MAC_CSR13, MAC_CSR14, MAC_CSR15, MAC_CSR16 = 0x41A, 0x41C, 0x41E, 0x420
MAC_CSR17, MAC_CSR18, MAC_CSR19, MAC_CSR20, MAC_CSR22 = 0x422, 0x424, 0x426, 0x428, 0x42C
TXRX_CSR0, TXRX_CSR1, TXRX_CSR2 = 0x440, 0x442, 0x444
TXRX_CSR5, TXRX_CSR6, TXRX_CSR7, TXRX_CSR8 = 0x44A, 0x44C, 0x44E, 0x450
TXRX_CSR10, TXRX_CSR11 = 0x454, 0x456
TXRX_CSR18, TXRX_CSR19, TXRX_CSR20, TXRX_CSR21 = 0x464, 0x466, 0x468, 0x46A
SEC_CSR0 = 0x480
PHY_CSR2, PHY_CSR4, PHY_CSR5, PHY_CSR6 = 0x4C4, 0x4C8, 0x4CA, 0x4CC
PHY_CSR7, PHY_CSR8, PHY_CSR9, PHY_CSR10 = 0x4CE, 0x4D0, 0x4D2, 0x4D4
STA_CSR0, STA_CSR3, STA_CSR5 = 0x4E0, 0x4E6, 0x4EA

RF2522, RF2523, RF2524, RF2525, RF2525E, RF5222 = 0, 1, 2, 3, 5, 0x10

# (rf1, rf2, rf3, rf4) per channel 1..14
RF_TABLES = {
    RF2522: [(0x2050, 0xC1FDA + 0x14 * i if i < 13 else 0xC20FA, 0x101, 0) for i in range(14)],
    RF2523: [(0x22010, v, 0xE0111, 0xA03 if c == 14 else 0xA1B) for c, v in zip(range(1, 15),
             [0xC9E, 0xCA2, 0xCA6, 0xCAA, 0xCAE, 0xCB2, 0xCB6, 0xCBA, 0xCBE, 0xD02, 0xD06, 0xD0A, 0xD0E, 0xD1A])],
    RF2524: [(0x32020, v, 0x101, 0xA03 if c == 14 else 0xA1B) for c, v in zip(range(1, 15),
             [0xC9E, 0xCA2, 0xCA6, 0xCAA, 0xCAE, 0xCB2, 0xCB6, 0xCBA, 0xCBE, 0xD02, 0xD06, 0xD0A, 0xD0E, 0xD1A])],
    RF2525: [(0x22020, 0x80000 | v, 0x60111, 0xA03 if c == 14 else 0xA1B) for c, v in zip(range(1, 15),
             [0xC9E, 0xCA2, 0xCA6, 0xCAA, 0xCAE, 0xCB2, 0xCB6, 0xCBA, 0xCBE, 0xD02, 0xD06, 0xD0A, 0xD0E, 0xD1A])],
    RF2525E: [(0x22010, v, 0x60111, r4) for v, r4 in [
        (0x89A, 0xE1B), (0x89E, 0xE07), (0x89E, 0xE1B), (0x8A2, 0xE07), (0x8A2, 0xE1B), (0x8A6, 0xE07),
        (0x8A6, 0xE1B), (0x8AA, 0xE07), (0x8AA, 0xE1B), (0x8AE, 0xE07), (0x8AE, 0xE1B), (0x8B2, 0xE07),
        (0x8B2, 0xE1B), (0x8B6, 0xE23)]],
    RF5222: [(0x22020, v, 0x101, 0xA1B if c == 14 else 0xA0B) for c, v in zip(range(1, 15),
             [0x1136, 0x113A, 0x113E, 0x1182, 0x1186, 0x118A, 0x118E, 0x1192, 0x1196, 0x119A, 0x119E,
              0x11A2, 0x11A6, 0x11AE])],
}
RF2525E_HALF = [0x8AA, 0x8AE, 0x8AE, 0x8B2, 0x8B2, 0x8B6, 0x8B6, 0x8BA, 0x8BA, 0x8BE, 0x8B7, 0x902, 0x902, 0x906]

TXD_SIZE, RXD_SIZE = 20, 16


class RT2570:
    def __init__(self):
        be = usb.backend.libusb1.get_backend(find_library=lambda _: libusb_package.get_library_path())
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, backend=be)
        if self.dev is None:
            raise RuntimeError("Nintendo Wi-Fi USB Connector not found (is WinUSB bound?)")
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        self.rf_shadow = [0] * 5
        self.eeprom = b""
        self.mac = b""
        self.rf = None
        self.rev = None

    # ---- raw access -------------------------------------------------------
    def vreq(self, req, value=0, index=0):
        self.dev.ctrl_transfer(OUT, req, value, index, None, 1000)

    def read(self, off):
        return struct.unpack("<H", bytes(self.dev.ctrl_transfer(IN, REQ_MULTI_READ, 0, off, 2, 1000)))[0]

    def write(self, off, val):
        self.dev.ctrl_transfer(OUT, REQ_MULTI_WRITE, 0, off, struct.pack("<H", val & 0xFFFF), 1000)

    def multiwrite(self, off, data):
        self.dev.ctrl_transfer(OUT, REQ_MULTI_WRITE, 0, off, bytes(data), 1000)

    def modify(self, off, mask, val):
        r = self.read(off)
        self.write(off, (r & ~mask) | (val & mask))

    def _wait(self, off, busy):
        for _ in range(100):
            r = self.read(off)
            if not r & busy:
                return r
            time.sleep(0.001)
        raise TimeoutError("register 0x%x busy" % off)

    def bbp_write(self, reg, val):
        self._wait(PHY_CSR8, 1)
        self.write(PHY_CSR7, (reg & 0x7F) << 8 | (val & 0xFF))

    def bbp_read(self, reg):
        self._wait(PHY_CSR8, 1)
        self.write(PHY_CSR7, 0x8000 | (reg & 0x7F) << 8)
        self._wait(PHY_CSR8, 1)
        return self.read(PHY_CSR7) & 0xFF

    def rf_write(self, word, val):
        self._wait(PHY_CSR10, 0x8000)
        self.write(PHY_CSR9, val & 0xFFFF)
        self.write(PHY_CSR10, 0x8000 | 20 << 8 | (val >> 16) & 0xFF)
        self.rf_shadow[word] = val

    def ee(self, word):
        return struct.unpack_from("<H", self.eeprom, word * 2)[0]

    # ---- probe ------------------------------------------------------------
    def probe(self):
        self.eeprom = bytes(self.dev.ctrl_transfer(IN, REQ_EEPROM_READ, 0, 0, 0x6E, 1000))
        self.mac = self.eeprom[4:10]
        ant = self.ee(0x0B)
        self.rf = (ant >> 11) & 0x1F
        self.ant_tx, self.ant_rx = (ant >> 2) & 3, (ant >> 4) & 3
        self.rev = self.read(MAC_CSR0)
        self.txpower = [b if b <= 31 else 24 for b in self.eeprom[0x3C:0x3C + 14]]
        return self

    def describe(self):
        return ("rev=0x%04x rf=0x%02x mac=%s ant_tx=%d ant_rx=%d txpower=%s"
                % (self.rev, self.rf, self.mac.hex(":"), self.ant_tx, self.ant_rx, self.txpower))

    # ---- bring-up ---------------------------------------------------------
    def set_state_awake(self):
        state = 3
        reg = state << 1 | state << 3
        self.write(MAC_CSR17, reg)
        reg |= 1
        self.write(MAC_CSR17, reg)
        for _ in range(100):
            r = self.read(MAC_CSR17)
            if (r >> 5) & 3 == state and (r >> 7) & 3 == state:
                return
            self.write(MAC_CSR17, reg)
            time.sleep(0.03)
        raise TimeoutError("device did not wake up")

    def init_registers(self):
        self.vreq(REQ_DEVICE_MODE, MODE_TEST, 0x0001)
        self.vreq(REQ_SINGLE_WRITE, 0x00F0, 0x0308)
        self.modify(TXRX_CSR2, 1, 1)                      # disable RX
        self.write(MAC_CSR13, 0x1111)
        self.write(MAC_CSR14, 0x1E11)
        self.modify(MAC_CSR1, 7, 3)                       # soft+bbp reset
        self.modify(MAC_CSR1, 7, 0)
        self.write(TXRX_CSR5, 0x80 | 13 | (0x80 | 12) << 8)
        self.write(TXRX_CSR6, 0x80 | 10 | (0x80 | 11) << 8)
        self.write(TXRX_CSR7, 0x80 | 7 | (0x80 | 6) << 8)
        self.write(TXRX_CSR8, 0x80 | 5)
        self.modify(TXRX_CSR19, 0x1F, 0)
        self.write(TXRX_CSR21, 0xE78F)
        self.write(MAC_CSR9, 0xFF1D)
        self.set_state_awake()
        self.modify(MAC_CSR1, 7, 4)                       # host ready
        if self.rev >= 3:
            self.modify(PHY_CSR2, 0x0002, 0)
        else:
            self.write(PHY_CSR2, 0x0002 | 3 << 12)
        self.write(MAC_CSR11, 0x0002)
        self.write(MAC_CSR22, 0x0053)
        self.write(MAC_CSR15, 0x01EE)
        self.write(MAC_CSR16, 0x0000)
        self.modify(MAC_CSR8, 0x0FFF, 2432)
        self.write(TXRX_CSR0, 24 << 3)                    # no cipher, IV offset = hdr
        self.modify(MAC_CSR18, 0x00FF, 90)
        self.modify(PHY_CSR4, 1, 1)
        # Explicit timestamp offset: warm/reset hardware may retain a different
        # value, which would overwrite SSID bytes in beacons/probe responses.
        self.modify(TXRX_CSR1, 0xFF00, 0x8000 | 24 << 8)

    def init_bbp(self):
        for _ in range(100):
            v = self.bbp_read(0)
            if v not in (0, 0xFF):
                break
            time.sleep(0.001)
        else:
            raise RuntimeError("BBP not ready")
        for reg, val in [(3, 0x02), (4, 0x19), (14, 0x1C), (15, 0x30), (16, 0xAC), (18, 0x18), (19, 0xFF),
                         (20, 0x1E), (21, 0x08), (22, 0x08), (23, 0x08), (24, 0x80), (25, 0x50), (26, 0x08),
                         (27, 0x23), (30, 0x10), (31, 0x2B), (32, 0xB9), (34, 0x12), (35, 0x50), (39, 0xC4),
                         (40, 0x02), (41, 0x60), (53, 0x10), (54, 0x18), (56, 0x08), (57, 0x10), (58, 0x08),
                         (61, 0x60), (62, 0x10), (75, 0xFF)]:
            self.bbp_write(reg, val)
        for i in range(16):
            w = self.ee(0x0E + i)
            if w not in (0, 0xFFFF):
                self.bbp_write(w >> 8, w & 0xFF)

    def config_antenna(self):
        # EEPROM SW diversity(0) -> HW diversity(1); A=1? rt2x00: 0=SW div,1=A,2=B -> we map like Linux
        def sel(v):
            return {0: 1, 1: 0, 2: 2}.get(v, 2)   # returns BBP/CSR field value
        tx, rx = sel(self.ant_tx), sel(self.ant_rx)
        r2, r14 = self.bbp_read(2), self.bbp_read(14)
        csr5, csr6 = self.read(PHY_CSR5), self.read(PHY_CSR6)
        r2 = (r2 & ~3) | tx
        csr5 = (csr5 & ~3) | tx
        csr6 = (csr6 & ~3) | tx
        r14 = (r14 & ~3) | rx
        if self.rf in (RF2525E, RF5222):
            r2 |= 4; csr5 |= 4; csr6 |= 4
            if self.rf == RF2525E:
                r14 &= ~4
        else:
            csr5 &= ~4; csr6 &= ~4
        self.bbp_write(2, r2)
        self.bbp_write(14, r14)
        self.write(PHY_CSR5, csr5)
        self.write(PHY_CSR6, csr6)

    def set_channel(self, ch, txpower=None):
        if not 1 <= ch <= 14:
            raise ValueError('channel must be between 1 and 14')
        if self.rf not in RF_TABLES:
            raise ValueError('unsupported RF chip: %r' % self.rf)
        rf1, rf2, rf3, rf4 = RF_TABLES[self.rf][ch - 1]
        p = self.txpower[ch - 1] if txpower is None else txpower
        rf3 = (rf3 & ~0x3E00) | (min(max(p, 0), 31) << 9)
        if self.rf == RF2525E:
            self.rf_write(2, RF2525E_HALF[ch - 1])
            if rf4:
                self.rf_write(4, rf4)
        self.rf_write(1, rf1)
        self.rf_write(2, rf2)
        self.rf_write(3, rf3)
        if rf4:
            self.rf_write(4, rf4)
        self.channel = ch

    def set_mac_bssid(self, mac):
        self.multiwrite(MAC_CSR2, mac)
        self.multiwrite(MAC_CSR5, mac)

    def set_rx_filter(self, promisc=False):
        # Keep control frames for AP power-save polling; drop CRC/PHY/version errors.
        reg = 0x2 | 0x4 | 0x40
        if not promisc:
            reg |= 0x10          # drop not-to-me unicast
        self.write(TXRX_CSR2, reg)   # also clears DISABLE_RX

    def start_rx(self):
        self.modify(TXRX_CSR2, 1, 0)

    def set_erp(self, beacon_int=100):
        self.modify(TXRX_CSR10, 4, 0)           # long preamble for DS compatibility
        self.write(TXRX_CSR11, 0x0003)          # basic rates 1/2 Mbps
        self.modify(TXRX_CSR18, 0xFFF0, beacon_int * 4 << 4)
        self.write(MAC_CSR10, 20)               # long slot (b compat)
        self.write(MAC_CSR11, 10)
        self.write(MAC_CSR12, 364)

    def enable_ap_sync(self):
        # AP mode is 3 (no synchronization to foreign beacons), not IBSS mode 2.
        preload = 144 + 48 + (24 + 20) * 8 // 1   # preamble + ~header duration at 1 Mbps (us)
        self.write(TXRX_CSR20, (preload >> 6) & 0x1FFF | 2 << 13)
        self.modify(TXRX_CSR18, 0x000F, 0)
        self.modify(TXRX_CSR19, 0x0006, 3 << 1)

    def radio_on(self, channel=6):
        if not self.eeprom:
            self.probe()
        self.init_registers()
        self.init_bbp()
        self.config_antenna()
        self.set_channel(channel)
        self.set_mac_bssid(self.mac)
        self.set_erp()

    def radio_off(self):
        try:
            self.modify(TXRX_CSR2, 1, 1)
            self.write(MAC_CSR13, 0x2121)
            self.write(MAC_CSR14, 0x2121)
            self.write(TXRX_CSR19, 0)
            self.vreq(REQ_RX_CONTROL, 0, 0)
        except usb.core.USBError:
            pass

    def close(self):
        try:
            self.radio_off()
        finally:
            usb.util.dispose_resources(self.dev)

    def led(self, link=True, activity=False):
        self.modify(MAC_CSR20, 3, (2 if link else 0) | (1 if activity else 0))

    # ---- frames -----------------------------------------------------------
    @staticmethod
    def _plcp(length, rate_500k):
        """Return (signal, service, len_lo, len_hi, ofdm) for the TX descriptor."""
        ofdm_sig = {12: 0xB, 18: 0xF, 24: 0xA, 36: 0xE, 48: 0x9, 72: 0xD, 96: 0x8, 108: 0xC}
        flen = length + 4   # FCS
        if rate_500k in ofdm_sig:
            return ofdm_sig[rate_500k], 0x04, flen & 0x3F, (flen >> 6) & 0x3F, 1
        # TX expects the CCK rate index (RX reports rate in 100 kbps instead).
        kbps100 = rate_500k * 5
        us = (flen * 8 * 10 + kbps100 - 1) // kbps100
        service = 0x04
        if rate_500k == 22:
            residual = (us * kbps100) - flen * 8 * 10
            if residual >= 80:
                service |= 0x80
        return {2: 0, 4: 1, 11: 2, 22: 3}[rate_500k], service, us & 0xFF, us >> 8, 0

    def build_tx(self, frame, rate_500k=2, ack=False, timestamp=False, retry=7, ifs=2, new_seq=True,
                 cipher=False):
        if not 10 <= len(frame) <= 2432:
            raise ValueError('invalid 802.11 frame length')
        if rate_500k not in (2, 4, 11, 22, 12, 18, 24, 36, 48, 72, 96, 108):
            raise ValueError('unsupported PHY rate')
        sig, svc, lo, hi, ofdm = self._plcp(len(frame), rate_500k)
        w0 = (retry & 0xF) << 4 | (0x200 if ack else 0) | (0x400 if timestamp else 0) | ofdm << 11 \
            | (0x1000 if new_seq else 0) | (ifs & 3) << 13 | (len(frame) & 0xFFF) << 16 \
            | (0x20000000 if cipher else 0)
        w1 = 24 | 2 << 6 | 4 << 8 | 10 << 12          # IV offset, AIFS=2, CWmin=4, CWmax=10
        w2 = sig | svc << 8 | lo << 16 | hi << 24
        buf = struct.pack("<5I", w0, w1, w2, 0, 0) + frame
        if len(buf) & 1:
            buf += b"\0"
        if len(buf) % 512 == 0:
            buf += b"\0\0"
        return buf

    def tx(self, frame, **kw):
        self.dev.write(0x01, self.build_tx(frame, **kw), 1000)

    def set_beacon(self, frame, rate_500k=2):
        self.modify(TXRX_CSR19, 0x10, 0)
        buf = self.build_tx(frame, rate_500k=rate_500k, timestamp=True, retry=0, ifs=3, new_seq=True)
        self.dev.write(0x01, b"\0", 1000)            # guardian byte
        self.dev.write(0x01, buf, 1000)
        r = self.read(TXRX_CSR19) | 0x1 | 0x8
        for v in (r | 0x10, r & ~0x10, r | 0x10, r & ~0x10, r | 0x10):
            self.write(TXRX_CSR19, v)

    def rx(self, timeout_ms=100):
        """Return (frame_without_fcs, rssi, flags) or None."""
        try:
            data = bytes(self.dev.read(0x81, 2560, timeout_ms))
        except usb.core.USBTimeoutError:
            return None
        if len(data) < RXD_SIZE:
            return None
        w0, w1 = struct.unpack_from("<II", data, len(data) - RXD_SIZE)
        size = (w0 >> 16) & 0xFFF
        if size < 14 or size > len(data) - RXD_SIZE:
            return None
        if w0 & 0x20 or w0 & 0x80:     # CRC / PHY error
            return None
        frame = data[:size]
        if len(frame) >= 4:
            frame = frame[:-4]         # strip FCS
        return frame, (w1 & 0xFF), w0
