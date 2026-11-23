"""Cross-check wfckey.derive_key against the original x86 routine under Unicorn."""
import os, random, sys, pefile
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32
from unicorn.x86_const import UC_X86_REG_ESP
sys.path.insert(0, os.path.dirname(__file__))
import wfckey

EXE = sys.argv[1]
pe = pefile.PE(EXE)
img = pe.get_memory_mapped_image()
base = pe.OPTIONAL_HEADER.ImageBase
FN, STOP, STACK, IN, OUT = 0x404140, 0xDEAD0000, 0x00200000, 0x00300000, 0x00300100

def run(digits):
    mu = Uc(UC_ARCH_X86, UC_MODE_32)
    mu.mem_map(base, (len(img) + 0xFFF) & ~0xFFF); mu.mem_write(base, img)
    mu.mem_map(STACK - 0x10000, 0x20000); mu.mem_map(IN, 0x1000); mu.mem_map(STOP, 0x1000)
    mu.mem_write(IN, digits)
    sp = STACK - 0x100
    mu.mem_write(sp, STOP.to_bytes(4, 'little') + IN.to_bytes(4, 'little') + OUT.to_bytes(4, 'little'))
    mu.reg_write(UC_X86_REG_ESP, sp)
    mu.emu_start(FN, STOP)
    return bytes(mu.mem_read(OUT, 13))

rng = random.Random(1)
for n in range(200):
    d = wfckey.make_ssid(rng)[12:]
    a, b = run(d), wfckey.derive_key(d)
    assert a == b, (d, a.hex(), b.hex())
print("OK 200/200 ->", d.decode(), wfckey.derive_key(d).hex())
