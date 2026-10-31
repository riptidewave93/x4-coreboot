#!/usr/bin/env python3
# Reflash a Radxa X4 over UART0, from the normal bootblock or the Top Swap recovery bootblock.
#   x4_uart_flash.py [--port DEV] output/<build>/coreboot.rom [--dry-run] [--write-data]
#   x4_uart_flash.py [--port DEV] coreboot.rom --enter-recovery [--write-protected] [--leave-recovery]
#   x4_uart_flash.py [--port DEV] --log FILE        (only capture the console)
# Start it, then power on the board. Python stdlib only.

import argparse
import glob
import os
import select
import struct
import sys
import termios
import time
import tty
import zlib
from collections import namedtuple

MAGIC = b"x4flash"
UNLOCK_MAGIC = b"UNLOCK-PROTECTED"
VERSION = 2
MAX_CRCS = 1024
MAX_FRAME = 0x2000  # resync on a longer length; raised to fit the block size HELLO reports
SOF = b"\xa5\x5a"
HELLO, CRCS, WRITE, READ, RESET, EXIT, UNLOCK, TOPSWAP = 1, 2, 3, 4, 5, 6, 7, 8
REPLY, NAK = 0x80, 0x7F
OK, SKIP, ERR = 0, 1, 2
NORMAL, RECOVERY = 0, 1
ROLES = {NORMAL: "normal bootblock", RECOVERY: "recovery bootblock"}
REASONS = {0: "none", 1: "bad CRC", 2: "bad length", 3: "bad type", 4: "not aligned",
           5: "not writable", 6: "range", 7: "read error", 8: "erase error",
           9: "write error", 10: "verify failed", 11: "unlock refused",
           12: "not in this role", 13: "the Top Swap block holds no recovery bootblock",
           14: "BUC.TS is locked", 15: "the Top Swap strap (R179) holds Top Swap on",
           16: "flash was written in this boot", 17: "the normal bootblock can't start",
           18: "flash reads follow Top Swap, so the bootblock can't be written",
           19: "the FMAP is not the one the normal bootblock expects",
           20: "the FMAP doesn't match this bootblock: enter recovery (--enter-recovery)"}
WRITABLE, UNLOCKABLE, PROTECTED = 0, 1, 2       # range kinds in HELLO
F_FMAP, F_DATA, F_FULL, F_KEEP, F_RESET, F_TS = 1, 2, 4, 8, 16, 32   # range flags in HELLO
S_SWAPPED, S_LAYOUT = 1, 2                      # HELLO status bits
FIT_PTR = 0x40     # the FIT pointer is at 4 GiB - 0x40: the image's last 64 bytes
PROBE_GLOB = "/dev/serial/by-id/usb-Raspberry_Pi_Debug_Probe*-if01*"


class Fail(Exception):
    pass


class LinkDown(Exception):
    pass


def find_port():
    found = sorted(glob.glob(PROBE_GLOB))
    if len(found) != 1:
        raise Fail(f"{len(found)} ports match {PROBE_GLOB}; pass --port")
    return found[0]


def open_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    a = termios.tcgetattr(fd)
    a[0] = 0                                       # iflag: no XON/XOFF, no CR mapping
    a[1] = 0                                       # oflag
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[3] = 0                                       # lflag
    a[4] = a[5] = termios.B115200
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def frame(ftype, seq, payload=b""):
    body = struct.pack("<BBH", ftype, seq & 0xFF, len(payload)) + payload
    return SOF + body + struct.pack("<I", zlib.crc32(body))


class Link:
    """Framed request/reply over the tty; bytes outside frames are console text."""

    def __init__(self, port, auto, log=None, echo=True):
        self.port, self.auto, self.log, self.echo = port, auto, log, echo
        self.fd = open_port(port)
        self.buf = bytearray()
        self.frames = []
        self.max_len = MAX_FRAME
        self.seq = 0
        self.retries = 0
        self.reconnects = 0

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def reconnect(self, timeout=180.0, settle=2.0):
        """The adapter can drop off USB (it does on board power cycles): wait for it."""
        self.close()
        self.reconnects += 1
        print("\nUART adapter lost; waiting for it to come back...", file=sys.stderr, flush=True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            time.sleep(0.5)
            try:
                path = find_port() if self.auto else self.port
                time.sleep(settle)
                if not os.path.exists(path):
                    continue
                self.fd = open_port(path)
            except (Fail, OSError):
                continue
            self.buf.clear()
            self.frames.clear()
            print(f"UART adapter back on {os.path.realpath(path)}.", file=sys.stderr, flush=True)
            return
        raise Fail("the UART adapter did not come back. The board stays in flash mode: "
                   "do not power off, reconnect it and rerun this tool.")

    def _text(self, data):
        if not data:
            return
        if self.log:
            self.log.write(data)
            self.log.flush()
        if self.echo:
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()

    def _parse(self):
        while True:
            i = self.buf.find(SOF)
            if i < 0:
                keep = 1 if self.buf.endswith(SOF[:1]) else 0
                self._text(bytes(self.buf[:len(self.buf) - keep]))
                del self.buf[:len(self.buf) - keep]
                return
            self._text(bytes(self.buf[:i]))
            del self.buf[:i]
            if len(self.buf) < 6:
                return
            ftype, seq, ln = struct.unpack_from("<BBH", self.buf, 2)
            if ln > self.max_len:
                self._text(bytes(self.buf[:1]))
                del self.buf[:1]
                continue
            if len(self.buf) < 6 + ln + 4:
                return
            body = bytes(self.buf[2:6 + ln])
            (crc,) = struct.unpack_from("<I", self.buf, 6 + ln)
            if crc != zlib.crc32(body):
                self._text(bytes(self.buf[:1]))
                del self.buf[:1]
                continue
            self.frames.append((ftype, seq, body[4:]))
            del self.buf[:6 + ln + 4]

    def pump(self, timeout):
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
            data = os.read(self.fd, 65536) if r else b""
        except BlockingIOError:
            data = b""
        except (OSError, ValueError) as e:
            raise LinkDown() from e
        if r:
            if data:
                self.buf += data
                self._parse()

    def write(self, data):
        view = memoryview(data)
        try:
            while view:
                select.select([], [self.fd], [], 1.0)
                try:
                    n = os.write(self.fd, view)
                except BlockingIOError:
                    continue
                view = view[n:]
            termios.tcdrain(self.fd)
        except (OSError, ValueError, termios.error) as e:
            raise LinkDown() from e

    def _try(self, ftype, payload, timeout):
        self.seq = (self.seq + 1) & 0xFF or 1
        self.frames.clear()
        self.write(frame(ftype, self.seq, payload))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.05)
            for t, s, p in self.frames:
                if s == self.seq and t == ftype | REPLY:
                    return p
                if s == self.seq and t == NAK:
                    return None
        return None

    def request(self, ftype, payload=b"", timeout=5.0, retries=5):
        """Every request is safe to repeat, so a lost adapter does not use up a retry."""
        attempt = 0
        while attempt < retries:
            try:
                p = self._try(ftype, payload, timeout)
            except LinkDown:
                self.reconnect()
                continue
            if p is not None:
                return p
            attempt += 1
            self.retries += 1
        raise Fail(f"no valid reply to request type {ftype:#x} after {retries} tries")


Range = namedtuple("Range", "start end kind bit flags name")


class Hello:
    """The board's HELLO: everything the tool knows about the board's flash comes from here."""

    def __init__(self, p):
        self.ver = p[0]
        self.error = None
        if self.ver != VERSION:
            self.error = (f"the board speaks UART flash protocol {self.ver}, this tool {VERSION}: "
                          "flash this build with Fpt (FLASHING.md)")
            return
        self.block, self.size = struct.unpack_from("<HI", p, 1)
        self.role, self.tss, self.buc_ts, self.unlocked, self.status, n = p[7:13]
        self.build = p[13:13 + n].decode(errors="replace")
        i = 14 + n
        self.ranges = []
        try:
            for _ in range(p[13 + n]):
                start, end, kind, bit, flags, ln = struct.unpack_from("<IIBBBB", p, i)
                self.ranges.append(Range(start, end, kind, bit, flags,
                                         p[i + 12:i + 12 + ln].decode(errors="replace")))
                i += 12 + ln
        except (struct.error, IndexError):
            self.ranges = []
        pos = 0
        for r in self.ranges:
            if r.start != pos or r.end <= r.start or r.end % max(self.block, 1):
                break
            pos = r.end
        if not self.block or self.size % self.block or pos != self.size or not self.size:
            self.error = "the board's HELLO has an invalid range table"

    def __str__(self):
        return (f"{ROLES.get(self.role, self.role)} {self.build}, Top Swap status {self.tss}, "
                f"BUC.TS {self.buc_ts}" +
                (", flash reads follow Top Swap" if self.status & S_SWAPPED else "") +
                (", FMAP doesn't match the bootblock" if self.status & S_LAYOUT else ""))

    def blocks(self, ranges):
        out = []
        for a, b in ranges:
            out.extend(range(a - a % self.block, b, self.block))
        return out

    def ts_range(self):
        """The Top Swap block: the range TOPSWAP 1 starts."""
        ts = [r for r in self.ranges if r.flags & F_TS]
        if len(ts) != 1:
            raise Fail("the board's HELLO names no single Top Swap block")
        return ts[0]


def wait_ready(link, wait_s):
    print(f"Sending '{MAGIC.decode()}'; power on the board now.", flush=True)
    end = time.monotonic() + wait_s
    last_hello = 0.0
    while time.monotonic() < end:
        try:
            link.write(MAGIC)
            now = time.monotonic()
            if now - last_hello > 0.5:
                link.write(frame(HELLO, 0))
                last_hello = now
            link.pump(0.05)
        except LinkDown:
            link.reconnect()
            continue
        for t, _, p in link.frames:
            if t == HELLO | REPLY:
                link.frames.clear()
                h = Hello(p)
                if not h.error:
                    link.max_len = max(MAX_FRAME, 8 + h.block, 7 + 4 * MAX_CRCS)
                return h
    raise Fail("the board did not enter UART flash mode")


def hold_stable(link, seconds):
    """Start writing only once the board has answered every ping for `seconds` in a row."""
    if seconds <= 0:
        return
    print(f"Holding until the link has been stable for {seconds:g} s...", flush=True)
    mark = (link.reconnects, link.retries)
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        link.request(HELLO, timeout=1.0, retries=20)
        if (link.reconnects, link.retries) != mark:
            print("Link hiccup; restarting the hold.", flush=True)
            mark = (link.reconnects, link.retries)
            start = time.monotonic()
        time.sleep(0.25)


def fmap_headers(rom):
    """Offsets of the FMAP headers that describe this image: signature, version 1, its size."""
    out, i = [], rom.find(b"__FMAP__")
    while i >= 0:
        if (i + 0x38 <= len(rom) and rom[i + 8] == 1 and
                struct.unpack_from("<I", rom, i + 18)[0] == len(rom)):
            out.append(i)
        i = rom.find(b"__FMAP__", i + 1)
    return out


def fmap_parse(rom, off):
    areas = {}
    for k in range(struct.unpack_from("<H", rom, off + 0x36)[0]):
        e = off + 0x38 + k * 42
        a_off, a_size = struct.unpack_from("<II", rom, e)
        areas[rom[e + 8:e + 40].split(b"\0")[0].decode(errors="replace")] = (a_off, a_off + a_size)
    return areas


def fmap_areas(rom):
    """The image's FMAP areas. The image must hold exactly one FMAP header."""
    heads = fmap_headers(rom)
    if len(heads) != 1:
        raise Fail(f"the image holds {len(heads)} FMAP headers, not one: not a coreboot image")
    return fmap_parse(rom, heads[0])


def layout(rom):
    """The image's FMAP areas, its FIT and the microcode the FIT lists."""
    areas = fmap_areas(rom)
    mmio = (1 << 32) - len(rom)
    fit = struct.unpack_from("<I", rom, len(rom) - FIT_PTR)[0] - mmio
    if not 0 <= fit <= len(rom) - 16 or rom[fit:fit + 8] != b"_FIT_   ":
        raise Fail("no FIT in the image")
    n = struct.unpack_from("<I", rom, fit + 8)[0] & 0xFFFFFF
    ucode = []
    for k in range(1, n):
        addr, _, _, typ = struct.unpack_from("<QIHB", rom, fit + 16 * k)
        o = addr - mmio
        if typ & 0x7F == 1 and 0 <= o <= len(rom) - 48:
            ucode.append((o, o + (struct.unpack_from("<I", rom, o + 32)[0] or 2048)))
    return areas, (fit, fit + 16 * n), ucode


def layout_problems(h, areas, size):
    """Where the image's layout disagrees with the ranges the board reports."""
    out = []
    if size != h.size:
        out.append(f"the image is {size:#x} bytes, the board's flash {h.size:#x}")
    for r in h.ranges:
        if not r.flags & F_FMAP:
            continue
        a = areas.get(r.name)
        if a is None:
            out.append(f"the image has no FMAP area {r.name}")
        elif a[0] != r.start or not r.end <= a[1]:
            out.append(f"{r.name} is {a[0]:#x}-{a[1]:#x} in the image, "
                       f"{r.start:#x}-{r.end:#x} on the board")
    return out


def fetch_crcs(link, h, addrs):
    """CRC32 per block, queried in contiguous runs of up to MAX_CRCS blocks."""
    crcs = {}
    i = 0
    while i < len(addrs):
        start = addrs[i]
        n = 1
        while (i + n < len(addrs) and n < MAX_CRCS and addrs[i + n] == start + n * h.block):
            n += 1
        p = link.request(CRCS, struct.pack("<IH", start, n), timeout=30.0)
        st = p[6]
        if st != OK or len(p) != 7 + 4 * n:
            raise Fail(f"CRC query at {start:#x} failed")
        for k, c in enumerate(struct.unpack_from(f"<{n}I", p, 7)):
            crcs[start + k * h.block] = c
        i += n
    return crcs


def fmt_eta(s):
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s"


def bail(link, msg):
    """Refuse before anything was written: let the board continue, then stop."""
    try:
        link.request(EXIT, timeout=3.0, retries=2)
    except Fail:
        pass
    raise Fail(msg)


def topswap(link, on):
    """Ask the board to set (on) or clear Top Swap. Returns once it resets."""
    try:
        p = link._try(TOPSWAP, bytes([on]), 5.0)
    except LinkDown:
        p = None
    if p is not None and p[0] != OK:
        bail(link, f"Top Swap {'set' if on else 'clear'} refused: {REASONS.get(p[1], p[1])}")
    # A second reply means the bit did not change and the board is still here
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        try:
            link.pump(0.05)
        except LinkDown:
            break
        for t, sq, q in link.frames:
            if t == TOPSWAP | REPLY and sq == link.seq and q[0] == ERR:
                link.frames.clear()
                bail(link, f"Top Swap did not change: {REASONS.get(q[1], q[1])}")
    link.frames.clear()


def differing(link, h, rom, ranges):
    """The blocks in ranges where the board differs from rom."""
    addrs = sorted(set(h.blocks(ranges)))
    crcs = fetch_crcs(link, h, addrs)
    return [a for a in addrs if crcs[a] != zlib.crc32(rom[a:a + h.block])]


def enter_recovery(args, link, h, rom):
    if h.role == RECOVERY:
        print("The board already runs the recovery bootblock.")
        return h
    ts = h.ts_range()
    if differing(link, h, rom, [(ts.start, ts.end)]):
        bail(link, f"the board's {ts.name} differs from the image's. Pass the board's own build, "
                   "or write the block first (Fpt, or --write-recovery-block).")
    print("Setting Top Swap: the board resets into the recovery bootblock.", flush=True)
    topswap(link, 1)
    h = wait_ready(link, args.wait)
    if h.error:
        raise Fail(h.error)
    print(f"Board: {h}.", flush=True)
    if h.role != RECOVERY:
        raise Fail("the board came back in the normal bootblock: Top Swap did not take")
    hold_stable(link, args.settle)
    return h


def boot_path(h, rom):
    """What the normal boot starts from: the board's reset ranges outside the Top Swap block,
    and the image's FMAP header, FIT and microcode."""
    _, fit, ucode = layout(rom)
    fmap = fmap_headers(rom)[0]
    return ([(r.start, r.end) for r in h.ranges if r.flags & F_RESET and not r.flags & F_TS] +
            [(fmap, fmap + 0x38), fit] + ucode)


def leave_recovery(link, h, rom):
    if h.role != RECOVERY:
        raise Fail("--leave-recovery: the board runs the normal bootblock")
    if rom and differing(link, h, rom, boot_path(h, rom)):
        bail(link, "the board's bootblock, FMAP, FIT or microcode differ from the image: "
                   "flash it first with --write-protected, or pass the build the board has.")
    print("Clearing Top Swap: the board resets into the normal boot.", flush=True)
    topswap(link, 0)


def check_layout(link, h, rom):
    bad = layout_problems(h, fmap_areas(rom), len(rom))
    if bad:
        bail(link, f"the image's layout does not match the board's: {'; '.join(bad)}. "
                   "Flash it with Fpt (FLASHING.md).")


def flash(args, link, h, rom):
    """Write the image's differing blocks that this role and the flags allow. Returns True
    if anything was written."""
    _, fit, ucode = layout(rom)
    check_layout(link, h, rom)
    if h.status & S_LAYOUT:
        bail(link, f"the board refuses writes: {REASONS[20]}")
    wrong = ("--write-recovery-block works only from the normal bootblock"
             if args.write_recovery_block and h.role != NORMAL else
             "--write-protected works only from the recovery bootblock (--enter-recovery)"
             if args.write_protected and h.role != RECOVERY else None)
    if wrong:
        bail(link, wrong)
    flag = "--write-protected" if h.role == RECOVERY else "--write-recovery-block"
    mask = 0
    if args.write_protected or args.write_recovery_block:
        for r in h.ranges:
            if r.kind == UNLOCKABLE:
                mask |= r.bit
        if not mask:
            bail(link, f"{flag}: the board offers no range to unlock")

    def selected(r):
        if r.kind == UNLOCKABLE:
            return bool(mask & r.bit)
        if r.kind != WRITABLE:
            return False
        if r.flags & F_FULL:
            return bool(args.full)
        return not r.flags & F_DATA or bool(args.write_data or args.full)

    written = [r for r in h.ranges if selected(r)]
    target = h.blocks([(r.start, r.end) for r in written])
    tset = set(target)
    # Ranges this run doesn't write: they must match the image, except the ones the board keeps
    left = [r for r in h.ranges if r.kind != WRITABLE and not selected(r)]
    fixed = [(r.name, (r.start, r.end)) for r in left if not r.flags & F_KEEP]
    kept = [(r.name, (r.start, r.end)) for r in left if r.flags & F_KEEP]
    # The FIT and microcode may change only together with the block holding the FIT pointer
    boot = [("FIT", fit)] + [("microcode", r) for r in ucode]
    ptr_block = len(rom) - h.block

    guard = h.blocks([r for _, r in fixed + kept + boot])
    print(f"Writing {', '.join(r.name for r in written)}. "
          f"Reading {len(tset | set(guard))} block checksums...", flush=True)
    crcs = fetch_crcs(link, h, sorted(tset | set(guard)))

    def same(a):
        return crcs[a] == zlib.crc32(rom[a:a + h.block])

    blank_crc = zlib.crc32(bytes([0xFF]) * h.block)
    # In recovery, a blank reset block in the normal bootblock is one an earlier cut-off run
    # cleared: this run restores it
    pending = [r for r in h.ranges if h.role == RECOVERY and r.flags & F_RESET and
               not r.flags & F_TS and crcs[r.end - h.block] == blank_crc]

    def differs(lo, hi):
        skip = {r.end - h.block for r in pending}
        return not all(same(a) for a in h.blocks([(lo, hi)]) if a not in skip)
    moved = [name for name, r in fixed if differs(*r)]
    if ptr_block not in tset:
        moved += sorted({name for name, r in boot if differs(*r)})
    if moved:
        locked = {r.name for r in left if r.kind == UNLOCKABLE}
        hint = (f"add {flag}" if all(n in locked for n in moved) or
                (h.role == RECOVERY and not args.write_protected) else
                "flash this build with Fpt (FLASHING.md), or from the recovery bootblock with "
                "--write-protected" if h.role == NORMAL else
                "flash this build with Fpt (FLASHING.md)")
        bail(link, f"these differ from the board: {', '.join(moved)}. This run does not "
                   f"write them: {hint}.")
    differ = [name for name, r in kept if differs(*r)]
    if differ:
        print(f"Kept as on the board, different in the image: {', '.join(differ)}.")

    diff = [a for a in target if not same(a)]
    # A range that ends in a reset vector loses it first and gets it back last, so a cut-off
    # run leaves a block that can't start the CPU instead of a half-written one. In recovery,
    # any code write guards the normal bootblock this way, as recovery leaves only if it starts.
    def touched(r):
        return any(r.start <= a < r.end for a in diff)
    guarded = [r for r in written if r.flags & F_RESET and touched(r)] + \
        [r for r in pending if r not in written]
    if h.role == RECOVERY and any(not r.flags & F_DATA and touched(r) for r in written):
        guarded += [r for r in h.ranges
                    if r.flags & F_RESET and not r.flags & F_TS and r not in guarded]
    extra = 0
    for r in guarded:
        if r.kind == UNLOCKABLE and not mask & r.bit:
            extra |= r.bit
        elif r.kind != WRITABLE and not mask & r.bit:
            bail(link, f"the board can't write the {r.name}'s last block, which this run must "
                       "clear first")
    blank = bytes([0xFF]) * h.block
    plan = [(a, rom[a:a + h.block]) for a in diff]
    for r in guarded:
        last = r.end - h.block
        plan = ([(last, blank)] + [(a, d) for a, d in plan if a != last] +
                [(last, rom[last:last + h.block])])
    verify = sorted(tset | {r.end - h.block for r in guarded})
    est = len(plan) * (h.block / 11520.0 + 0.1)
    print(f"{len(diff)} blocks differ. About {fmt_eta(est)}.")
    if args.dry_run or not plan:
        print("Dry run: nothing written." if args.dry_run else "Nothing to write.")
        return False

    if mask | extra:
        p = link.request(UNLOCK, UNLOCK_MAGIC + bytes([mask | extra]), timeout=3.0)
        if p[0] != OK or p[2] & (mask | extra) != mask | extra:
            bail(link, f"unlock refused: {REASONS.get(p[1], p[1])}")

    t0 = time.monotonic()
    seen = link.reconnects
    for n, (a, data) in enumerate(plan, 1):
        if link.reconnects != seen:
            hold_stable(link, args.settle)
            seen = link.reconnects
        p = link.request(WRITE, struct.pack("<I", a) + data, timeout=10.0)
        (raddr,) = struct.unpack_from("<I", p)
        st, reason = p[4], p[5]
        if raddr != a or st == ERR:
            raise Fail(f"write {a:#x} failed: {REASONS.get(reason, reason)}. Do not power "
                       "off; rerun this tool to retry.")
        el = time.monotonic() - t0
        eta = el / n * (len(plan) - n)
        word = "OK" if st == OK else "same"
        print(f"\r[{n:4}/{len(plan)}] {a:#08x} {word:4}  ETA {fmt_eta(eta)} ",
              end="", flush=True)
    print()

    if link.reconnects != seen:
        hold_stable(link, args.settle)
    print("Verifying...", flush=True)
    crcs = fetch_crcs(link, h, verify)
    bad = [a for a in verify if not same(a)]
    if bad:
        raise Fail(f"{len(bad)} blocks still differ after writing, e.g. {bad[0]:#x}. "
                   "Do not power off; rerun this tool.")
    print(f"All blocks verified ({link.retries} retried requests).")
    return True


def run(args, link):
    rom = None
    path = args.full or args.rom
    if path:
        rom = open(path, "rb").read()
        layout(rom)

    h = wait_ready(link, args.wait)
    if h.error:
        bail(link, h.error)
    print(f"\nBoard in UART flash mode: {h}.", flush=True)
    if rom:
        check_layout(link, h, rom)
    link.echo = False
    hold_stable(link, args.settle)
    switching = args.enter_recovery or args.leave_recovery
    if switching and args.dry_run:
        print("Dry run: Top Swap is not changed.")
    elif args.enter_recovery:
        h = enter_recovery(args, link, h, rom)

    wrote = flash(args, link, h, rom) if rom else False
    if args.leave_recovery and not args.dry_run:
        leave_recovery(link, h, rom)
    elif wrote and not args.no_reset:
        link.request(RESET, timeout=3.0)
        print("Board resets" + (" into the recovery bootblock: Top Swap stays set." if
                                h.role == RECOVERY else "."))
    else:
        p = link.request(EXIT, timeout=3.0)
        if p and p[0] == OK:
            print("Board continues booting." if h.role == NORMAL else
                  "The recovery bootblock waits for the next session.")
        else:
            print("Board resets (flash changed).")
    return 0


def capture(link, seconds):
    link.echo = True
    end = time.monotonic() + seconds if seconds else None
    try:
        while end is None or time.monotonic() < end:
            try:
                link.pump(0.2)
            except LinkDown:
                link.reconnect(timeout=max(1.0, end - time.monotonic()) if end else 3600.0)
    except (KeyboardInterrupt, Fail):
        pass


def main():
    ap = argparse.ArgumentParser(description="Radxa X4 UART flash mode host tool")
    ap.add_argument("rom", nargs="?", help="coreboot.rom of the build to flash")
    ap.add_argument("--full", metavar="IMAGE",
                    help="a full flash image (e.g. an Fpt backup) instead of a coreboot.rom: "
                         "also writes RW_MRC_CACHE, SMMSTORE and, from recovery, region 9. "
                         "The descriptor and ME are never written")
    ap.add_argument("--enter-recovery", action="store_true",
                    help="from the normal bootblock: check the board's recovery block against "
                         "the image, set Top Swap, then flash the image from the recovery "
                         "bootblock (the board's own build writes nothing)")
    ap.add_argument("--leave-recovery", action="store_true",
                    help="from the recovery bootblock: clear Top Swap at the end, booting normally; "
                         "with an image, its bootblock and FMAP must match the board's first")
    ap.add_argument("--write-protected", action="store_true",
                    help="from the recovery bootblock: also write the ranges it protects until asked "
                         "(the FMAP and the normal bootblock)")
    ap.add_argument("--write-recovery-block", action="store_true",
                    help="from the normal bootblock: also write the recovery block")
    ap.add_argument("--port", help="serial device (default: auto-detect the debug probe)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--write-data", action="store_true",
                    help="also write RW_MRC_CACHE and SMMSTORE (default: keep the board's)")
    ap.add_argument("--no-reset", action="store_true", help="end with EXIT instead of RESET")
    ap.add_argument("--wait", type=float, default=120.0, help="seconds to wait for the board")
    ap.add_argument("--log", help="append the console to this file, also after the reset")
    ap.add_argument("--settle", type=float, default=5.0,
                    help="seconds the link must answer every ping before writes start "
                         "(again after any reconnect); 0 disables")
    ap.add_argument("--log-seconds", type=float, default=0,
                    help="stop logging after this many seconds (default: until Ctrl-C)")
    args = ap.parse_args()

    port = args.port or find_port()
    log = open(args.log, "ab") if args.log else None
    link = Link(port, auto=not args.port, log=log)
    try:
        if args.rom and args.full:
            ap.error("give a coreboot.rom or --full IMAGE, not both")
        if args.enter_recovery and not (args.rom or args.full):
            ap.error("--enter-recovery needs a coreboot.rom whose recovery block matches the "
                     "board's: the board's own build, or any build with the same pin")
        if args.rom or args.full or args.enter_recovery or args.leave_recovery:
            rc = run(args, link)
            if log:
                capture(link, args.log_seconds)
            return rc
        if not log:
            ap.error("give a coreboot.rom to flash, --enter-recovery, --leave-recovery or --log FILE")
        capture(link, args.log_seconds)
        return 0
    except Fail as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
