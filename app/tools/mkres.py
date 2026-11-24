"""Write rsrc_windows_amd64.syso: a COFF object with the app icon, manifest and
version info, which the Go linker embeds into the .exe (so Explorer shows the icon).

    python tools/mkres.py icon.ico rsrc_windows_amd64.syso 1.1.0
"""
import struct
import sys

RT_ICON, RT_GROUP_ICON, RT_VERSION, RT_MANIFEST = 3, 14, 16, 24
LANG = 0x0409

MANIFEST = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="WFCConnector" version="1.0.0.0" processorArchitecture="amd64"/>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
    </windowsSettings>
  </application>
  <dependency><dependentAssembly>
    <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0"
      processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>
  </dependentAssembly></dependency>
</assembly>
'''


def pad4(b):
    return b + b'\0' * (-len(b) % 4)


def wstr(s):
    return s.encode('utf-16-le') + b'\0\0'


def ver_block(key, value=b'', children=b'', text=False):
    """VS_VERSIONINFO-style block: wLength, wValueLength, wType, szKey, pad, value, pad, children."""
    head_key = wstr(key)
    body = pad4(struct.pack('<HHH', 0, 0, 0) + head_key)
    vlen = len(value) // 2 if text else len(value)
    body += pad4(value) + children
    return struct.pack('<HHH', len(body), vlen, 1 if text else 0) + body[6:]


def version_info(version):
    parts = [int(x) for x in (version.split('.') + ['0'] * 4)[:4]]
    ms, ls = parts[0] << 16 | parts[1], parts[2] << 16 | parts[3]
    fixed = struct.pack('<13I', 0xFEEF04BD, 0x00010000, ms, ls, ms, ls, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    strings = b''
    for k, v in (('CompanyName', 'WFC Connector'), ('FileDescription', 'WFC Connector'),
                 ('FileVersion', version), ('InternalName', 'WFC-Connector'),
                 ('OriginalFilename', 'WFC-Connector.exe'), ('ProductName', 'WFC Connector'),
                 ('ProductVersion', version)):
        strings += pad4(ver_block(k, wstr(v), text=True))
    table = ver_block('040904b0', children=strings, text=True)
    sfi = ver_block('StringFileInfo', children=pad4(table), text=True)
    var = ver_block('Translation', struct.pack('<HH', 0x0409, 0x04b0))
    vfi = ver_block('VarFileInfo', children=pad4(var), text=True)
    return ver_block('VS_VERSION_INFO', fixed, pad4(sfi) + vfi)


def read_ico(path):
    data = open(path, 'rb').read()
    _, typ, count = struct.unpack_from('<HHH', data)
    assert typ == 1
    entries = []
    for i in range(count):
        w, h, cc, res, planes, bpp, size, off = struct.unpack_from('<BBBBHHII', data, 6 + 16 * i)
        entries.append(((w, h, cc, res, planes, bpp), data[off:off + size]))
    return entries


def to_dib(png):
    """PNG -> 32-bit DIB (BITMAPINFOHEADER + BGRA bottom-up + AND mask) for small icon sizes."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert('RGBA')
    w, h = im.size
    px = im.load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))
    row = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(row)
        for x in range(w):
            if px[x, y][3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    hdr = struct.pack('<IiiHHIIiiII', 40, w, h * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    return hdr + bytes(xor) + bytes(mask)


def build(ico, version):
    images = []
    for hdr, img in read_ico(ico):
        if img[:8] == b'\x89PNG\r\n\x1a\n' and hdr[0] != 0:  # keep PNG only for 256px (w==0)
            img = to_dib(img)
            hdr = hdr[:4] + (1, 32)
        images.append((hdr, img))
    group = struct.pack('<HHH', 0, 1, len(images))
    resources = []  # (type, id, data)
    for i, (hdr, img) in enumerate(images, 1):
        w, h, cc, res, planes, bpp = hdr
        group += struct.pack('<BBBBHHIH', w, h, cc, res, planes or 1, bpp or 32, len(img), i)
        resources.append((RT_ICON, i, img))
    resources.append((RT_GROUP_ICON, 1, group))
    resources.append((RT_VERSION, 1, version_info(version)))
    resources.append((RT_MANIFEST, 1, MANIFEST))
    resources.sort(key=lambda r: (r[0], r[1]))

    types = sorted({r[0] for r in resources})
    by_type = {t: [r for r in resources if r[0] == t] for t in types}
    # layout: root dir, type dirs, name dirs, data entries, data
    def dir_size(n):
        return 16 + 8 * n
    off = dir_size(len(types))
    type_dir_off = {}
    for t in types:
        type_dir_off[t] = off
        off += dir_size(len(by_type[t]))
    name_dir_off = {}
    for r in resources:
        name_dir_off[(r[0], r[1])] = off
        off += dir_size(1)
    entry_off = {}
    for r in resources:
        entry_off[(r[0], r[1])] = off
        off += 16
    data_off = {}
    for r in resources:
        off = (off + 7) & ~7
        data_off[(r[0], r[1])] = off
        off += len(r[2])
    sec = bytearray(off)

    def put_dir(at, children):
        struct.pack_into('<IIHHHH', sec, at, 0, 0, 0, 0, 0, len(children))
        for i, (ident, target, is_dir) in enumerate(children):
            struct.pack_into('<II', sec, at + 16 + 8 * i, ident, target | (0x80000000 if is_dir else 0))

    put_dir(0, [(t, type_dir_off[t], True) for t in types])
    for t in types:
        put_dir(type_dir_off[t], [(r[1], name_dir_off[(t, r[1])], True) for r in by_type[t]])
    relocs = []
    for r in resources:
        k = (r[0], r[1])
        put_dir(name_dir_off[k], [(LANG, entry_off[k], False)])
        struct.pack_into('<IIII', sec, entry_off[k], data_off[k], len(r[2]), 0, 0)
        relocs.append(entry_off[k])  # OffsetToData is an RVA: relocate against the section
        sec[data_off[k]:data_off[k] + len(r[2])] = r[2]

    # COFF object
    nreloc = len(relocs)
    hdr_size = 20 + 40
    raw_ptr = hdr_size
    reloc_ptr = raw_ptr + len(sec)
    sym_ptr = reloc_ptr + 10 * nreloc
    out = struct.pack('<HHIIIHH', 0x8664, 1, 0, sym_ptr, 1, 0, 0)
    out += struct.pack('<8sIIIIIIHHI', b'.rsrc', 0, 0, len(sec), raw_ptr, reloc_ptr, 0, nreloc, 0, 0x40000040)
    out += bytes(sec)
    for r in relocs:
        out += struct.pack('<IIH', r, 0, 3)  # IMAGE_REL_AMD64_ADDR32NB, symbol 0
    out += struct.pack('<8sIhHBB', b'.rsrc', 0, 1, 0, 3, 0)
    out += struct.pack('<I', 4)  # empty string table
    return out


if __name__ == '__main__':
    ico, dst, version = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else '1.0.0')
    open(dst, 'wb').write(build(ico, version))
    print('wrote', dst)
