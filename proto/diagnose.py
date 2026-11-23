"""Bounded RT2570 receive test. Does not transmit frames or store payloads."""
import argparse
import collections
import json
import time

from rt2570 import RT2570, MAC_CSR17, TXRX_CSR2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', type=int, choices=range(1, 12), default=6)
    parser.add_argument('--seconds', type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error('--seconds must be in (0, 300]')
    dev = RT2570()
    counts = collections.Counter()
    try:
        dev.probe()
        print(dev.describe(), flush=True)
        dev.radio_on(args.channel)
        print('RADIO_ON BBP0=%02x STATE=%04x' %
              (dev.bbp_read(0), dev.read(MAC_CSR17)), flush=True)
        dev.set_rx_filter(promisc=True)
        dev.start_rx()
        print('RX channel=%d filter=%04x' % (args.channel, dev.read(TXRX_CSR2)), flush=True)
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            packet = dev.rx(100)
            if packet is None:
                continue
            frame, rssi, flags = packet
            if len(frame) < 24:
                continue
            kind = '%d/%d' % ((frame[0] >> 2) & 3, frame[0] >> 4)
            counts[kind] += 1
            if counts[kind] <= 3:
                print('RX type/subtype=%s bytes=%d rssi_raw=%d' %
                      (kind, len(frame), rssi), flush=True)
        print('SUMMARY ' + json.dumps(dict(counts), sort_keys=True), flush=True)
    finally:
        dev.close()
        print('RADIO_OFF', flush=True)


if __name__ == '__main__':
    main()
