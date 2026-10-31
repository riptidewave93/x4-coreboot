#!/usr/bin/env python3
# Check a Radxa X4 coreboot.rom against the stock descriptor's regions and write its BIOS region.
#   check_x4_image.py <coreboot.rom> <.config> <bios-region.bin out>
# Exits non-zero on any failed check, so a bad image never reaches output/. Also writes
# flashprog-layout.txt next to the output.
import hashlib
import lzma
import os
import struct
import sys

from x4_uart_flash import fmap_headers, fmap_parse

ROM_SIZE = 0x1000000
MMIO = 0xFF000000                   # 16MB flash mapped below 4G
TOP_SWAP = (0xF80000, 0xFC0000)     # descriptor TSBS: 256KB, swapped with the top block
TOP = (0xFC0000, ROM_SIZE)          # the bootblock's block
SMM_BLOCK = 0x10000                 # SMMSTORE v2 block size
# Regions of the stock descriptor, which stays on the board (FINDINGS.md, "Flash")
DESC = {'SI_DESC': (0x000000, 0x001000), 'SI_ME': (0x001000, 0x45C000),
        'SI_BIOS': (0x600000, ROM_SIZE)}
X4 = os.path.join(os.path.dirname(os.path.abspath(sys.argv[2])), '..', '..', 'src/mainboard/radxa/x4')
PIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'configs', 'recovery-bootblock.sha256')

rom_path, cfg_path, out_path = sys.argv[1:4]
rom = open(rom_path, 'rb').read()
failed = []


def check(ok, msg):
    print(('  ok    ' if ok else '  FAIL  ') + msg)
    if not ok:
        failed.append(msg)


def fit_entries(img, ptr_off=0xFFFFC0, ts=False):
    """FIT table offset and its entries as (type, flash offset). ts: addresses as the CPU
    sees them with Top Swap set, where the Top Swap block appears at the top."""
    def to_flash(addr):
        o = addr - MMIO
        return o ^ 0x40000 if ts and o >= TOP_SWAP[0] else o
    fit = to_flash(struct.unpack_from('<I', img, ptr_off)[0])
    if not 0 <= fit < ROM_SIZE or img[fit:fit + 8] != b'_FIT_   ':
        return fit, None
    n = struct.unpack_from('<I', img, fit + 8)[0] & 0xFFFFFF
    ents = []
    for i in range(1, n):
        addr, _, _, typ = struct.unpack_from('<QIHB', img, fit + 16 * i)
        ents.append((typ & 0x7F, to_flash(addr)))
    return fit, ents


def microcode_ok(img, o, lo, hi):
    """An ADL-N microcode update with a valid checksum, inside lo-hi."""
    hdr, rev, _, sig, _, loader = struct.unpack_from('<6I', img, o)
    total = struct.unpack_from('<I', img, o + 32)[0] or 2048
    ok = (hdr == 1 and loader == 1 and sig == 0xB06E0 and lo <= o and o + total <= hi and
          sum(struct.unpack_from(f'<{total // 4}I', img, o)) & 0xFFFFFFFF == 0)
    return ok, rev


def cbfs_files(img, area):
    """CBFS files in area (lo, hi) as dicts: name, header offset, data start and end, attrs."""
    files = []
    o = img.find(b'LARCHIVE', area[0], area[1])
    while 0 <= o < area[1]:
        size, typ, attr, data = struct.unpack_from('>4I', img, o + 8)
        name = img[o + 24:o + data].split(b'\0')[0].decode(errors='replace')
        files.append(dict(name=name, type=typ, hdr=o, start=o + data, end=o + data + size,
                          attr=o + attr if attr else 0))
        o = img.find(b'LARCHIVE', (o + data + size + 63) & ~63, area[1])
    return files


def cbfs_body(img, f):
    """A file's contents, LZMA-decoded if compressed."""
    body, a = img[f['start']:f['end']], f['attr']
    while a and a < f['start']:
        tag, alen = struct.unpack_from('>2I', img, a)
        if tag == 0x42435A4C and struct.unpack_from('>I', img, a + 8)[0] == 1:
            return lzma.decompress(body, format=lzma.FORMAT_ALONE)
        if not alen:
            break
        a += alen
    return body


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


print(f'Checking {rom_path}')
check(len(rom) == ROM_SIZE, f'image is 16MB (0x{len(rom):x})')

fmaps = fmap_headers(rom)
fmap_off = fmaps[0] if fmaps else -1
areas = fmap_parse(rom, fmap_off) if fmaps else {}
check(fmap_off >= 0 and areas.get('FMAP', (0,))[0] == fmap_off, f'FMAP found at 0x{fmap_off:x}, in its own area')
for name, reg in DESC.items():
    check(areas.get(name) == reg, f'{name} {areas.get(name)} matches the descriptor region {reg}')
bios = DESC['SI_BIOS']
inner = sorted((a, n) for n, a in areas.items()
               if bios[0] <= a[0] < bios[1] and n != 'SI_BIOS')
check([n for _, n in inner] == ['RW_MRC_CACHE', 'SMMSTORE', 'FMAP', 'COREBOOT'],
      f'BIOS region holds RW_MRC_CACHE, SMMSTORE, FMAP, COREBOOT: {[n for _, n in inner]}')
edges = [bios[0]] + [x for a, _ in inner for x in a] + [bios[1]]
check(all(edges[i] == edges[i + 1] for i in range(0, len(edges), 2)),
      'they fill the BIOS region with no gaps or overlaps')
mrc, smm, cb = (areas.get(n, (0, 0)) for n in ('RW_MRC_CACHE', 'SMMSTORE', 'COREBOOT'))
check(cb[1] == ROM_SIZE, f'COREBOOT CBFS ends at the top of flash: 0x{cb[0]:x}-0x{cb[1]:x}')
check(all(a[0] % SMM_BLOCK == 0 and a[1] % SMM_BLOCK == 0 and a[1] > a[0] for a in (mrc, smm)),
      f'RW_MRC_CACHE 0x{mrc[0]:x}-0x{mrc[1]:x} and SMMSTORE 0x{smm[0]:x}-0x{smm[1]:x} are 64KB aligned')
check(all(a[1] <= TOP_SWAP[0] for a in (mrc, smm, areas.get('FMAP', (0, ROM_SIZE)))),
      f'RW_MRC_CACHE, SMMSTORE and FMAP lie below the Top Swap block 0x{TOP_SWAP[0]:x}')

check(rom[0xFFFFF0:ROM_SIZE] != b'\xff' * 16, 'reset vector present at the top of flash')
fit, ents = fit_entries(rom)
check(ents is not None and cb[0] <= fit < TOP_SWAP[0], f'FIT table at 0x{fit:x}, in COREBOOT below the Top Swap block')
ucode = [o for t, o in (ents or []) if t == 1]
check(bool(ucode), f'FIT has microcode entries: {[hex(o) for o in ucode]}')
for o in ucode:
    ok, rev = microcode_ok(rom, o, cb[0], TOP_SWAP[0])
    check(ok, f'microcode at 0x{o:x} (rev 0x{rev:x}) is for ADL-N, checksum ok, below the Top Swap block')
check(len(fmaps) == 1, f'the image holds a single FMAP header: {[hex(o) for o in fmaps]}')

files = cbfs_files(rom, cb)
by_name = {f['name']: f for f in files}
rec = by_name.get('recovery-bootblock')
check(rec is not None and (rec['start'], rec['end']) == TOP_SWAP and rec['type'] == 0x50,
      f'raw CBFS file recovery-bootblock fills the Top Swap block 0x{TOP_SWAP[0]:x}-0x{TOP_SWAP[1] - 1:x}')
block = rom[TOP_SWAP[0]:TOP_SWAP[1]]
check(block[-16:] != b'\xff' * 16, 'recovery block: reset vector present at 0xfbfff0')
ts_fit, ts_ents = fit_entries(rom, TOP_SWAP[1] - 0x40, ts=True)
ts_ucode = [o for t, o in (ts_ents or []) if t == 1]
check(ts_ents is not None and TOP_SWAP[0] <= ts_fit < TOP_SWAP[1] and bool(ts_ucode) and
      all(TOP_SWAP[0] <= o < TOP_SWAP[1] for t, o in ts_ents),
      f'recovery block: FIT at 0x{ts_fit:x} and its entries {[hex(o) for o in ts_ucode]} lie in the block')
for o in ts_ucode:
    ok, rev = microcode_ok(rom, o, *TOP_SWAP)
    check(ok, f'recovery block: microcode at 0x{o:x} (rev 0x{rev:x}) is for ADL-N, checksum ok')
pin = open(PIN).read().split()[0]
got = hashlib.sha256(block).hexdigest()
check(got == pin, f'recovery block sha256 {got} matches configs/recovery-bootblock.sha256')
bb = by_name.get('bootblock')
check(bb is not None and bb['end'] == ROM_SIZE and bb['start'] >= TOP[0],
      f'bootblock 0x{bb["start"] if bb else 0:x}-0x{ROM_SIZE - 1:x} lies in the top 256KB')
intruders = [f['name'] for f in files if f['name'] and f not in (rec, bb) and
             overlaps((f['hdr'], f['end']), (TOP_SWAP[0], ROM_SIZE))]
check(not intruders, f'no other CBFS file in 0x{TOP_SWAP[0]:x}-0x{ROM_SIZE - 1:x}: {intruders}')

vbt = cbfs_body(rom, by_name['vbt.bin']) if 'vbt.bin' in by_name else None
check(vbt == open(os.path.join(X4, 'data.vbt'), 'rb').read(), f'CBFS vbt.bin is the board\'s data.vbt ({len(vbt or b"")} bytes)')

if failed:
    sys.exit(f'{len(failed)} check(s) failed, not writing {out_path}')
open(out_path, 'wb').write(rom[bios[0]:bios[1]])
print(f'Wrote {out_path} (BIOS region 0x{bios[0]:x}-0x{bios[1] - 1:x})')
layout = os.path.join(os.path.dirname(out_path), 'flashprog-layout.txt')
with open(layout, 'w') as f:
    f.write(f'{bios[0]:08x}:{bios[1] - 1:08x} bios\n')
print(f'Wrote {layout}')
