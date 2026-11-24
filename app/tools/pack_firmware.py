"""Pack compiled radio firmware into app/fw/<chip>.bin.gz (embedded in the app).

Compile firmware/esp32-radio for each chip with arduino-cli and --output-dir out/<chip>, then:

    python tools/pack_firmware.py <out-dir>

Each <out-dir>/<chip>/esp32-radio.ino.merged.bin is trimmed of trailing 0xFF
padding (rounded up to 4 KB) and gzipped into fw/<chip>.bin.gz.
"""
import gzip
import os
import sys

CHIPS = ['esp32', 'esp32s2', 'esp32s3', 'esp32c3', 'esp32c5', 'esp32c6']

out = sys.argv[1]
fw = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fw')
os.makedirs(fw, exist_ok=True)
for chip in CHIPS:
    src = os.path.join(out, chip, 'esp32-radio.ino.merged.bin')
    if not os.path.exists(src):
        print('skip', chip, '(not built)')
        continue
    b = open(src, 'rb').read()
    end = (len(b.rstrip(b'\xff')) + 0xfff) & ~0xfff
    open(os.path.join(fw, chip + '.bin.gz'), 'wb').write(gzip.compress(b[:end], 9, mtime=0))
    print(chip, end, 'bytes')
