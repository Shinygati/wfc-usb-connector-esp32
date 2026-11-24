"""Reference trace for rt2570_test.go: runs the original Python RT2570 driver
(proto/rt2570.py from wfc-usb-connector) against a simulated adapter and prints
every USB request, for comparison with the Go port.

    python3 testdata/rt2570_reference.py <path to wfc-usb-connector/proto>
"""
import sys, struct
sys.path.insert(0, sys.argv[1])
import usb.core, usb.util
import rt2570

def eeprom():
    e = bytearray(0x6E)
    for i in range(0x6E): e[i] = (i * 7 + 3) & 0xFF
    e[4:10] = bytes.fromhex('0016016a2b3c')
    struct.pack_into('<H', e, 0x0B * 2, (5 << 11) | (1 << 2) | (2 << 4))   # RF2525E, ant tx=1, rx=2
    for i in range(16): struct.pack_into('<H', e, (0x0E + i) * 2, 0xFFFF)
    struct.pack_into('<H', e, 0x0E * 2, 0x1A2B); struct.pack_into('<H', e, 0x10 * 2, 0x0C44)
    for i in range(14): e[0x3C + i] = 20 + i if i != 3 else 40
    return bytes(e)

class Fake:
    def __init__(s): s.regs = {}; s.log = []
    def set_configuration(s): pass
    def ctrl_transfer(s, t, req, value, index, data, timeout):
        if t == 0xC0:
            if req == 9: return eeprom()[:data]
            v = {0x4D0: 0, 0x4D4: 0, 0x422: 0x1E0, 0x4CE: 0x12, 0x400: 4}.get(index, s.regs.get(index, 0))
            s.log.append('R %04x' % index)
            return struct.pack('<H', v)
        d = bytes(data or b'')
        s.log.append('W %02x %d %04x %04x %s' % (t, req, value, index, d.hex()))
        if req == 6 and len(d) == 2: s.regs[index] = struct.unpack('<H', d)[0]
    def write(s, ep, data, timeout): s.log.append('B %02x %s' % (ep, bytes(data).hex()))

fake = Fake()
usb.core.find = lambda **k: fake
usb.util.claim_interface = lambda *a: None
d = rt2570.RT2570(); d.probe()
d.radio_on(6); d.enable_ap_sync(); d.modify(0x466, 0x1f, 1); d.set_rx_filter(promisc=True); d.start_rx(); d.led(True)
bcast = b'\x80\x00' + b'\x00\x00' + b'\xff' * 6 + bytes.fromhex('0016016a2b3c') * 2 + b'\x10\x00' + b'\x01' * 13
uni = b'\xb0\x00' + b'\x00\x00' + bytes.fromhex('0009bf123456') + bytes.fromhex('0016016a2b3c') * 2 + b'\x20\x00' + b'\x02' * 6
d.tx(bcast, ack=False, timestamp=False, new_seq=True)
d.tx(uni, ack=True, new_seq=True)
print('\n'.join(fake.log))
