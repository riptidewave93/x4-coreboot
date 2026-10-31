#!/usr/bin/env python3
# End-to-end test of the X4 UART flash mode: board core (host build) <-> pty <-> host tool.
#   run_tests.py [coreboot.rom]   (default: the newest X4 build in output/ with the board core's layout)
# The update image is that image with some writable blocks changed. Covers both roles (normal and
# Top Swap recovery bootblock), unlocking, Top Swap entry and exit, and the idle return.
# Needs gcc and BuildEnv/coreboot with the UART flash patch. Runs in the builder container.

import glob
import os
import random
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CORE = os.path.join(REPO, "BuildEnv/coreboot/src/mainboard/radxa/x4")
TOOL = os.path.join(REPO, "scripts/x4_uart_flash.py")
sys.path.insert(0, os.path.join(REPO, "scripts"))
import x4_uart_flash as t  # noqa: E402

FLASH = 0x1000000
BLOCK = 0x1000
results = []
R = {}      # the stub's normal-role ranges by name: the layout under test


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")


def build_stub(tmp):
    exe = os.path.join(tmp, "stub")
    subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", CORE, "-o", exe,
                    os.path.join(HERE, "stub.c"), os.path.join(CORE, "uart_flash_proto.c")],
                   check=True)
    return exe


class Board:
    def __init__(self, stub, flash, env=None):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        e = dict(os.environ, **(env or {}))
        self.proc = subprocess.Popen([stub, str(self.master), flash], pass_fds=[self.master],
                                     env=e, stderr=subprocess.PIPE, text=True)

    def wait(self, timeout=60):
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = -1
        self.log = self.proc.stderr.read()
        os.close(self.master)
        os.close(self.slave)
        return rc


class FlakyAdapter:
    """Relay the board's pty to a fresh pty behind a fixed path, and unplug it once after
    the tool has sent `drop_after` bytes: the tool sees EIO, as with a real USB drop."""

    def __init__(self, board_port, path, drop_after):
        self.bfd = t.open_port(board_port)
        self.path, self.drop_after = path, drop_after
        self.sent = self.drops = 0
        self.m = None
        self._plug()
        self.stop = False
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _plug(self):
        self.m, self.s = os.openpty()
        tty.setraw(self.s)
        if os.path.lexists(self.path):
            os.unlink(self.path)
        os.symlink(os.ttyname(self.s), self.path)

    def _unplug(self):
        os.unlink(self.path)
        os.close(self.m)
        os.close(self.s)
        self.m = None

    def _run(self):
        down_until = 0.0
        while not self.stop:
            if self.m is None and time.monotonic() > down_until:
                self._plug()
            fds = [self.bfd] + ([self.m] if self.m is not None else [])
            r, _, _ = select.select(fds, [], [], 0.05)
            try:
                if self.bfd in r:
                    data = os.read(self.bfd, 65536)
                    if self.m is not None:
                        os.write(self.m, data)
                if self.m is not None and self.m in r:
                    data = os.read(self.m, 65536)
                    os.write(self.bfd, data)
                    self.sent += len(data)
            except OSError:
                pass
            if not self.drops and self.sent > self.drop_after and self.m is not None:
                self._unplug()
                self.drops += 1
                down_until = time.monotonic() + 3.0

    def close(self):
        self.stop = True
        self.th.join()
        if self.m is not None:
            self._unplug()
        os.close(self.bfd)


def make_flash(tmp, base, name):
    """The base image plus distinct patterns where the board's data must survive."""
    out = bytearray(base)
    bios = R["region 9"][1]
    out[0:bios] = b"\x11" * bios                            # descriptor, ME, region 9 stand-in
    for (a, b), v in ((R["RW_MRC_CACHE"], 0x33), (R["SMMSTORE"], 0x44)):
        out[a:b] = bytes([v]) * (b - a)                     # trained MRC cache, variables
    path = os.path.join(tmp, name)
    open(path, "wb").write(out)
    return path, bytes(out)


def changed(base, addrs, tmp, name):
    """base with one byte flipped in each block at addrs."""
    out = bytearray(base)
    for a in addrs:
        out[a + 0x123] ^= 0x5A
    path = os.path.join(tmp, name)
    open(path, "wb").write(out)
    return path, bytes(out)


def tool(port, *args):
    r = subprocess.run([sys.executable, TOOL, "--port", port, "--wait", "15", *args],
                       capture_output=True, text=True, timeout=600)
    return r.returncode, r.stdout + r.stderr


def regions_equal(a, b, ranges):
    return all(a[x:y] == b[x:y] for x, y in ranges)


def outside(ranges):
    """The complement of ranges in the flash."""
    out, pos = [], 0
    for a, b in sorted(ranges):
        out.append((pos, a))
        pos = b
    return out + [(pos, FLASH)]


def run_board(stub, tmp, base, label, img, args=(), env=None):
    """Flash image img (or none) with the tool; returns rc, output, board rc, before, after."""
    path, before = make_flash(tmp, base, f"flash-{label}.bin")
    board = Board(stub, path, env)
    proc = subprocess.Popen([sys.executable, TOOL, "--port", board.port, "--wait", "15",
                             *([img] if img else []), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    brc = board.wait(600)
    try:
        out = proc.communicate(timeout=8)[0]    # a board that died leaves the tool retrying
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
    return proc.returncode, out + board.log, brc, before, open(path, "rb").read()


def board_ranges(stub, role):
    """The stub's own copy of the board's ranges for a role."""
    out = subprocess.run([stub, "--ranges", str(role)], capture_output=True, text=True,
                         check=True).stdout
    rs = []
    for line in out.splitlines():
        a, b, kind, bit, flags, name = line.split(" ", 5)
        rs.append(t.Range(int(a, 16), int(b, 16), int(kind), int(bit), int(flags), name))
    return rs


def fake_hello(ranges, size=FLASH):
    return SimpleNamespace(ranges=ranges, size=size)


def hello_payload(ranges, size=FLASH):
    p = struct.pack("<BHIBBBBBB", t.VERSION, BLOCK, size, 0, 0, 0, 0, 0, 0) + bytes([len(ranges)])
    for r in ranges:
        n = r.name.encode()
        p += struct.pack("<IIBBBB", r.start, r.end, r.kind, r.bit, r.flags, len(n)) + n
    return p


def kept(out):
    """The tool's line naming ranges the board keeps although the image differs."""
    return next((ln for ln in out.splitlines() if ln.startswith("Kept as on the board")), "")


def pick_image(stub):
    want = [fake_hello(board_ranges(stub, role)) for role in (t.NORMAL, t.RECOVERY)]
    for f in sorted(glob.glob(os.path.join(REPO, "output/*-radxa-x4*/coreboot.rom")), reverse=True):
        rom = open(f, "rb").read()
        try:
            areas = t.fmap_areas(rom)
        except t.Fail:
            continue
        if not any(t.layout_problems(h, areas, len(rom)) for h in want):
            return f
    sys.exit("no X4 build in output/ has the board core's layout; build one or pass it")


def session(stub, path, env=None):
    """A board and a raw protocol link to it, in UART flash mode."""
    board = Board(stub, path, env)
    link = t.Link(board.port, auto=False, echo=False)
    h = t.wait_ready(link, 5)
    return board, link, h


def write(link, addr):
    p = link.request(t.WRITE, struct.pack("<I", addr) + b"\0" * BLOCK)
    return p[4], p[5]


def unlock(link, mask, magic=t.UNLOCK_MAGIC):
    p = link.request(t.UNLOCK, magic + bytes([mask]))
    return p[0], p[1], p[2]


def main():
    tmp = tempfile.mkdtemp(prefix="uf-test-")
    try:
        stub = build_stub(tmp)
        rom = sys.argv[1] if len(sys.argv) > 1 else pick_image(stub)
        print(f"image {os.path.relpath(rom, REPO)}")
        base = open(rom, "rb").read()
        areas, fit, ucode = t.layout(base)
        tabs = {role: board_ranges(stub, role) for role in (t.NORMAL, t.RECOVERY)}
        R.update({r.name: (r.start, r.end) for r in tabs[t.NORMAL]})
        fmap, (cb_lo, cb_hi), r9 = R["FMAP"], R["COREBOOT"], R["region 9"]
        TS, TOP = R["recovery block"], R["bootblock"]
        data = [R["RW_MRC_CACHE"], R["SMMSTORE"]]
        for role in (t.NORMAL, t.RECOVERY):
            check(f"role {role}: the image's FMAP agrees with the stub's ranges",
                  not t.layout_problems(fake_hello(tabs[role]), areas, len(base)))
        check("image has a recovery block (reset vector at the Top Swap block's end)",
              base[TS[1] - 16:TS[1]] != b"\xff" * 16)

        print("Layout check (no board)")
        nt = tabs[t.NORMAL]
        moved = [r._replace(start=r.start + BLOCK) if r.name == "SMMSTORE" else
                 r._replace(end=r.end + BLOCK) if r.name == "RW_MRC_CACHE" else r for r in nt]
        check("a board whose SMMSTORE starts elsewhere disagrees",
              any("SMMSTORE" in x for x in t.layout_problems(fake_hello(moved), areas, len(base))))
        gone = dict(areas)
        del gone["SMMSTORE"]
        check("an image without an FMAP area the board names disagrees",
              any("no FMAP area SMMSTORE" in x for x in t.layout_problems(fake_hello(nt), gone, len(base))))
        check("an image of another size disagrees",
              t.layout_problems(fake_hello(nt, FLASH * 2), areas, len(base)))
        check("ranges without the FMAP flag are not compared with the FMAP",
              not t.layout_problems(fake_hello([r._replace(name="x " + r.name) if not r.flags & 1
                                                else r for r in nt]), areas, len(base)))
        check("HELLO: the stub's table parses as a valid table",
              t.Hello(hello_payload(nt)).error is None and t.Hello(hello_payload(nt)).ranges == nt)
        check("HELLO: a table with a gap is rejected", t.Hello(hello_payload(nt[:3] + nt[4:])).error)
        check("HELLO: a table short of the flash end is rejected", t.Hello(hello_payload(nt[:-1])).error)
        check("HELLO: a truncated table is rejected", t.Hello(hello_payload(nt)[:-14]).error)
        guard = {a for lo, hi in [fmap, fit] + ucode for a in range(lo - lo % BLOCK, hi, BLOCK)}
        free = [a for a in range(cb_lo, cb_hi, BLOCK) if a not in guard]
        upd = free[:: max(1, len(free) // 300)]
        new, newb = changed(base, upd, tmp, "new.rom")
        code = [(cb_lo, cb_hi)]

        print("Normal role: full update")
        for label, extra in (("update", ()), ("data", ("--write-data",))):
            rc, out, brc, before, after = run_board(stub, tmp, base, label, new, extra)
            check(f"{label}: tool succeeds and the board resets", rc == 0 and brc == 42,
                  f"rc={rc} brc={brc}\n{out[-800:]}")
            check(f"{label}: COREBOOT below the Top Swap block equals the new image",
                  regions_equal(after, newb, code))
            keep = outside(code + (data if extra else []))
            check(f"{label}: everything else untouched", regions_equal(after, before, keep))
        check("data: MRC cache and SMMSTORE now equal the image", regions_equal(after, newb, data))

        print("Lost reply and corrupted request")
        rc, out, brc, before, after = run_board(stub, tmp, base, "faults", new, (),
                                                {"UF_DROP_REPLY": "8", "UF_CORRUPT_RX": "20000"})
        check("faults: tool succeeds, both were retried",
              rc == 0 and brc == 42 and "(2 retried requests)" in out, out[-400:])
        check("faults: COREBOOT equals the new image", regions_equal(after, newb, code))

        for when, drop_after in (("during the hold", 200), ("mid-write", 30000)):
            print(f"Adapter unplugged {when}")
            path, before = make_flash(tmp, base, "flash-drop.bin")
            board = Board(stub, path)
            ad = FlakyAdapter(board.port, os.path.join(tmp, "probe"), drop_after)
            rc, out = tool(ad.path, new, "--settle", "1")
            brc = board.wait()
            ad.close()
            after = open(path, "rb").read()
            check(f"drop {when}: tool reconnects, finishes, board resets",
                  ad.drops == 1 and rc == 0 and brc == 42 and "adapter back" in out,
                  f"drops={ad.drops} rc={rc} brc={brc}\n{out[-600:]}")
            check(f"drop {when}: COREBOOT equals the new image, everything else untouched",
                  regions_equal(after, newb, code) and regions_equal(after, before, outside(code)))

        print("Normal role: guards and dry run")
        cases = [("bootblock", [TOP[0] + 0x30000], "bootblock"),
                 ("FMAP block", [fmap[0] + 0x800 - 0x123], "FMAP"),
                 ("FIT", [fit[0] - fit[0] % BLOCK], "FIT"),
                 ("microcode", [ucode[0][0] - ucode[0][0] % BLOCK + BLOCK], "microcode"),
                 ("recovery block", [TS[0] + 0x10000], "--write-recovery-block")]
        for label, addrs, word in cases:
            img, _ = changed(newb, addrs, tmp, f"guard-{label}.rom")
            rc, out, brc, before, after = run_board(stub, tmp, base, "guard", img)
            check(f"guard: a changed {label} is refused, board boots on, flash unchanged",
                  rc == 1 and brc == 43 and word in out and after == before,
                  f"rc={rc} brc={brc}\n{out[-400:]}")
        moved = bytearray(newb)
        o = newb.find(b"__FMAP__")
        for k in range(struct.unpack_from("<H", moved, o + 0x36)[0]):
            e = o + 0x38 + k * 42
            if moved[e + 8:e + 16] == b"SMMSTORE":
                struct.pack_into("<I", moved, e + 4, struct.unpack_from("<I", moved, e + 4)[0] // 2)
        img = os.path.join(tmp, "guard-layout.rom")
        open(img, "wb").write(moved)
        rc, out, brc, before, after = run_board(stub, tmp, base, "layout", img)
        check("guard: another layout is refused, flash unchanged",
              rc == 1 and brc == 43 and "Fpt" in out and after == before, f"rc={rc}\n{out[-400:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "dry", new, ("--dry-run",))
        check("dry run: nothing written, board boots on",
              rc == 0 and brc == 43 and after == before, f"rc={rc} brc={brc}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "wp", new, ("--write-protected",))
        check("--write-protected is refused in the normal role",
              rc == 1 and "recovery bootblock" in out and after == before, out[-300:])

        print("Normal role: --write-recovery-block")
        rec_new, rec_newb = changed(newb, [TS[0], TS[0] + 0x20000], tmp, "rec.rom")
        rc, out, brc, before, after = run_board(stub, tmp, base, "recblk", rec_new,
                                                ("--write-recovery-block",))
        check("recovery block written with the flag, board resets",
              rc == 0 and brc == 42 and regions_equal(after, rec_newb, [TS] + code), out[-400:])
        check("recovery block: FMAP, top 256 KB, data, descriptor and ME untouched",
              regions_equal(after, before, outside([TS] + code)))

        print("Normal role: an FMAP that doesn't match the bootblock")
        badfm = bytearray(base)
        badfm[fmap[0]:fmap[1]] = b"\xff" * (fmap[1] - fmap[0])
        path, before = make_flash(tmp, bytes(badfm), "flash-badfmap.bin")
        board, link, h = session(stub, path)
        st, ul = write(link, cb_lo), unlock(link, 4)
        p = link.request(t.CRCS, struct.pack("<IH", cb_lo, 1))
        r = link.request(t.READ, struct.pack("<I", cb_lo))
        ts = link.request(t.TOPSWAP, b"\1")
        brc = board.wait()
        link.close()
        check("bad FMAP: HELLO reports it, WRITE and UNLOCK are refused, CRCS and READ work",
              h.status & 2 and st == (t.ERR, 20) and ul[:2] == (t.ERR, 20) and p[6] == t.OK
              and r[4] == t.OK, f"status={h.status} st={st} ul={ul}")
        check("bad FMAP: TOPSWAP 1 still enters recovery, flash unchanged",
              ts[:2] == bytes([t.OK, 0]) and brc == 45 and open(path, "rb").read() == before,
              f"ts={ts!r} brc={brc}")
        rc, out, brc, before, after = run_board(stub, tmp, bytes(badfm), "badfmap", rom)
        check("bad FMAP: the tool refuses to write and says to enter recovery, board boots on",
              rc == 1 and brc == 43 and "enter recovery" in out and after == before,
              f"rc={rc} brc={brc}\n{out[-300:]}")
        rc, out, brc, before, after = run_board(stub, tmp, bytes(badfm), "badfmap-fix", rom,
                                                ("--enter-recovery", "--write-protected",
                                                 "--leave-recovery"), {"UF_REBOOT": "1"})
        check("bad FMAP: enter recovery, write the FMAP, leave: board boots the normal role",
              rc == 0 and brc == 44 and after[fmap[0]:fmap[1]] == base[fmap[0]:fmap[1]]
              and "STUB: topswap 0" in out, f"rc={rc} brc={brc}\n{out[-500:]}")

        print("No magic: timeout")
        path, _ = make_flash(tmp, base, "flash-timeout.bin")
        board = Board(stub, path, {"UF_WINDOW_MS": "300"})
        brc = board.wait(10)
        check("timeout: board continues booting", brc == 44, f"brc={brc}")

        print("Normal role: protocol")
        path, before = make_flash(tmp, base, "flash-neg.bin")
        board = Board(stub, path)
        link = t.Link(board.port, auto=False, echo=False)
        noise = bytes(random.Random(1).randrange(256) for _ in range(300))
        link.write(noise + b"x4fl" + b"zz" + b"x4x4flash")
        deadline = time.monotonic() + 5
        h = None
        while time.monotonic() < deadline and h is None:
            link.pump(0.05)
            h = next((t.Hello(f[2]) for f in link.frames if f[0] == t.HELLO | t.REPLY), None)
        check("noise and partial magic before the real one: READY", h is not None)
        check("HELLO: version 2, normal role, Top Swap off, build id",
              h and h.ver == 2 and h.role == t.NORMAL and h.tss == 0 and h.buc_ts == 0
              and h.build == "stub-normal")
        check("HELLO: the normal ranges equal the stub's own table",
              h and h.error is None and h.ranges == tabs[t.NORMAL],
              h and "\n".join(map(str, h.ranges)))
        for label, addr in (("the descriptor", 0), ("the ME", 0x1000), ("region 9", r9[0]),
                            ("the FMAP", fmap[0]), ("the Top Swap block", TS[0]),
                            ("the Top Swap block's last block", TS[1] - BLOCK),
                            ("the bootblock's block", TOP[0]), ("the last block", 0xFFF000)):
            check(f"write to {label} is refused", write(link, addr) == (t.ERR, 5))
        check("unaligned write is refused", write(link, cb_lo + 0x800) == (t.ERR, 4))
        p = link.request(t.CRCS, struct.pack("<IH", cb_lo + 0x10, 1))
        check("unaligned CRC query is refused", p[6] == t.ERR)
        p = link.request(t.CRCS, struct.pack("<IH", 0xFFF000, 2))
        check("CRC query past the end is refused", p[6] == t.ERR)
        check("unlock with a wrong magic is refused", unlock(link, 4, b"UNLOCK-PROTECTEX")[:2] == (t.ERR, 11))
        check("unlock of the FMAP or top 256 KB is refused in the normal role",
              unlock(link, 1)[:2] == (t.ERR, 11) and unlock(link, 2)[:2] == (t.ERR, 11))
        check("Top Swap clear is refused in the normal role",
              link.request(t.TOPSWAP, b"\0")[:2] == bytes([t.ERR, 12]))
        bad = bytearray(t.frame(t.HELLO, 77))
        bad[-1] ^= 0xFF
        link.frames.clear()
        link.write(bytes(bad))
        end = time.monotonic() + 3
        while time.monotonic() < end and not link.frames:
            link.pump(0.05)
        check("corrupted frame gets a NAK (bad CRC)",
              link.frames and link.frames[0][0] == t.NAK and link.frames[0][2] == b"\x01")
        p = link.request(t.READ, struct.pack("<I", 0xFF0000))
        check("reading the bootblock is allowed and correct",
              p[4] == t.OK and p[5:] == before[0xFF0000:0xFF1000])
        p = link.request(t.EXIT)
        brc = board.wait()
        link.close()
        check("EXIT with nothing written continues booting", p == b"\x00" and brc == 43,
              f"p={p!r} brc={brc}")
        check("flash unchanged by the negative tests", open(path, "rb").read() == before)

        board, link, h = session(stub, path)
        check("unlock of the recovery block is accepted", unlock(link, 4) == (t.OK, 0, 4))
        st = write(link, TS[0])
        check("after the unlock, the Top Swap block is writable", st[0] == t.OK)
        check("after the unlock, the FMAP and top 256 KB are still refused",
              write(link, fmap[0]) == (t.ERR, 5) and write(link, TOP[0]) == (t.ERR, 5))
        st = write(link, cb_hi - BLOCK)
        p = link.request(t.EXIT)
        brc = board.wait()
        link.close()
        check("write at COREBOOT's last writable block succeeds", st[0] in (t.OK, t.SKIP))
        check("EXIT after writing resets instead of booting on", p == b"\x02" and brc == 42,
              f"p={p!r} brc={brc}")

        rec = {"UF_BUC": "1", "UF_IDLE_MS": "3000", "UF_MAX_IDLES": "1"}
        print("Recovery role: protocol")
        path, before = make_flash(tmp, base, "flash-rec.bin")
        board, link, h = session(stub, path, rec)
        check("HELLO: recovery role, Top Swap on, build id",
              h.role == t.RECOVERY and h.tss == 1 and h.buc_ts == 1 and h.build == "stub-recovery")
        check("HELLO: the recovery ranges equal the stub's own table",
              h.error is None and h.ranges == tabs[t.RECOVERY], "\n".join(map(str, h.ranges)))
        check("HELLO: flash reads don't follow Top Swap", h.status == 0)
        for label, addr in (("the descriptor", 0), ("the ME", 0x1000), ("the ME's last block", r9[0] - BLOCK),
                            ("the FMAP", fmap[0]), ("the top 256 KB", TOP[0]),
                            ("the Top Swap block", TS[0]), ("the Top Swap block's last block", TS[1] - BLOCK)):
            check(f"recovery: write to {label} is refused", write(link, addr) == (t.ERR, 5))
        check("recovery: write to region 9 succeeds", write(link, r9[0])[0] == t.OK)
        check("recovery: unlock of the recovery block is refused", unlock(link, 4)[:2] == (t.ERR, 11))
        check("recovery: Top Swap set is refused", link.request(t.TOPSWAP, b"\1")[:2] == bytes([t.ERR, 12]))
        check("recovery: unlock of the FMAP and top 256 KB is accepted", unlock(link, 3) == (t.OK, 0, 3))
        check("recovery: after the unlock, the FMAP and top 256 KB are writable",
              write(link, fmap[0])[0] == t.OK and write(link, TOP[0])[0] == t.OK)
        check("recovery: after the unlock, the Top Swap block, descriptor and ME are still refused",
              write(link, TS[0]) == (t.ERR, 5) and write(link, 0) == (t.ERR, 5)
              and write(link, 0x1000) == (t.ERR, 5))
        link.request(t.RESET)
        brc = board.wait()
        link.close()
        check("recovery: no write outside the allowed ranges reached the flash", brc == 42, f"brc={brc}")

        board, link, h = session(stub, path, dict(rec, UF_SWAPPED="1"))
        sw_top, sw_fmap = unlock(link, 2), unlock(link, 1)
        link.request(t.EXIT)
        board.wait()
        link.close()
        check("swapped flash reads: reported in HELLO, unlock of the top refused, FMAP allowed",
              h.status & 1 and sw_top[:2] == (t.ERR, 18) and sw_fmap[:2] == (t.OK, 0),
              f"status={h.status} top={sw_top} fmap={sw_fmap}")

        print("Recovery role: the tool")
        rc, out, brc, before, after = run_board(stub, tmp, base, "rec-upd", new, (), rec)
        check("recovery: COREBOOT update succeeds, board resets",
              rc == 0 and brc == 42 and regions_equal(after, newb, code)
              and regions_equal(after, before, outside(code)), f"rc={rc}\n{out[-400:]}")
        top_new, top_newb = changed(rec_newb, [0xFF0000, fmap[0] + 0x800 - 0x123,
                                               fit[0] - fit[0] % BLOCK], tmp, "top.rom")
        rc, out, brc, before, after = run_board(stub, tmp, base, "rec-top", top_new, (), rec)
        check("recovery: a changed bootblock, FMAP or FIT needs --write-protected",
              rc == 1 and "--write-protected" in out and after == before, f"rc={rc}\n{out[-400:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "rec-wp", top_new,
                                                ("--write-protected",), rec)
        written = code + [(fmap[0], fmap[0] + BLOCK), TOP]
        check("recovery --write-protected: COREBOOT, FMAP and top 256 KB written",
              rc == 0 and brc == 42 and regions_equal(after, top_newb, written), f"rc={rc}\n{out[-400:]}")
        check("recovery --write-protected: the recovery block is not written, and reported",
              regions_equal(after, before, outside(written)) and "recovery block" in kept(out))
        full = bytearray(top_newb)
        full[0:r9[0]] = b"\x22" * r9[0]
        full[r9[0]:r9[1]] = b"\x55" * (r9[1] - r9[0])
        full_path = os.path.join(tmp, "full.bin")
        open(full_path, "wb").write(full)
        rc, out, brc, before, after = run_board(stub, tmp, base, "rec-full", None,
                                                ("--full", full_path, "--write-protected"), rec)
        check("recovery --full: region 9, data, COREBOOT, FMAP and top written",
              rc == 0 and regions_equal(after, full, [r9] + data + written), f"rc={rc}\n{out[-400:]}")
        check("recovery --full: descriptor, ME and recovery block untouched and reported",
              after[:r9[0]] == before[:r9[0]] and after[TS[0]:TS[1]] == before[TS[0]:TS[1]]
              and "SI_DESC" in kept(out) and "SI_ME" in kept(out))
        rc, out, brc, before, after = run_board(stub, tmp, base, "rec-wrb", rec_new,
                                                ("--write-recovery-block",), rec)
        check("--write-recovery-block is refused in the recovery role",
              rc == 1 and "normal bootblock" in out and after == before, out[-300:])

        print("Top Swap entry and exit")
        sw = {"UF_REBOOT": "1", "UF_IDLE_MS": "4000", "UF_MAX_IDLES": "1"}
        rc, out, brc, before, after = run_board(stub, tmp, base, "enter", None,
                                                ("--enter-recovery",), sw)
        check("--enter-recovery without an image is refused", rc != 0 and "needs a coreboot.rom" in out,
              out[-300:])
        rc, out, brc, before, after = run_board(stub, tmp, base, "enter", rom,
                                                ("--enter-recovery",), sw)
        check("--enter-recovery with the board's build: board resets into the recovery role, "
              "nothing written", rc == 0 and brc == 47 and "STUB: topswap 1" in out
              and "STUB: boot role 1" in out and "Nothing to write" in out and after == before,
              f"rc={rc} brc={brc}\n{out[-600:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "enter-rec", rec_new,
                                                ("--enter-recovery",), sw)
        check("--enter-recovery is refused when the board's recovery block differs from the image",
              rc == 1 and brc == 43 and "differs from the image" in out and "topswap" not in out,
              f"rc={rc} brc={brc}\n{out[-600:]}")
        rt_new, rt_newb = changed(newb, [0xFF0000, fmap[0] + 0x800 - 0x123,
                                         fit[0] - fit[0] % BLOCK], tmp, "rt.rom")
        rc, out, brc, before, after = run_board(stub, tmp, base, "roundtrip", rt_new,
                                                ("--enter-recovery", "--write-protected",
                                                 "--leave-recovery"), {"UF_REBOOT": "1"})
        check("enter, flash with --write-protected, leave: board boots the normal role",
              rc == 0 and brc == 44 and "STUB: topswap 0" in out
              and out.rfind("STUB: boot role 0") > out.find("STUB: boot role 1") > 0,
              f"rc={rc} brc={brc}\n{out[-600:]}")
        check("round trip: COREBOOT, FMAP and top written, the rest untouched",
              regions_equal(after, rt_newb, written) and regions_equal(after, before, outside(written)))
        blank = bytearray(base)
        blank[TS[0]:TS[1]] = b"\xff" * (TS[1] - TS[0])
        blank_rom = os.path.join(tmp, "blank.rom")
        open(blank_rom, "wb").write(blank)
        rc, out, brc, before, after = run_board(stub, tmp, bytes(blank), "blank", blank_rom,
                                                ("--enter-recovery",), {"UF_REBOOT": "1"})
        check("--enter-recovery is refused over a blank Top Swap block, board boots on",
              rc == 1 and brc == 43 and "no recovery bootblock" in out and "topswap" not in out,
              f"rc={rc} brc={brc}\n{out[-400:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "locked", rom,
                                                ("--enter-recovery",), {"UF_REBOOT": "1", "UF_TS_LOCKED": "1"})
        check("--enter-recovery with BUC.TS locked: reported, board boots on",
              rc == 1 and brc == 43 and "locked" in out, f"rc={rc} brc={brc}\n{out[-400:]}")
        strap = {"UF_REBOOT": "1", "UF_STRAP": "1", "UF_IDLE_MS": "1500", "UF_MAX_IDLES": "1"}
        rc, out, brc, before, after = run_board(stub, tmp, base, "strap", None,
                                                ("--leave-recovery",), strap)
        check("--leave-recovery under the strap: reported, board stays in recovery",
              rc == 1 and brc == 47 and "R179" in out and "boot role 0" not in out,
              f"rc={rc} brc={brc}\n{out[-400:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "swap-leave", None, ("--leave-recovery",),
                                                {"UF_REBOOT": "1", "UF_BUC": "1", "UF_SWAPPED": "1",
                                                 "UF_IDLE_MS": "1500", "UF_MAX_IDLES": "1"})
        check("swapped flash reads: --leave-recovery and idle are refused, recovery stays",
              rc == 1 and brc == 47 and "follow Top Swap" in out and "idle refused 18" in out
              and "topswap 0" not in out, f"rc={rc} brc={brc}\n{out[-400:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "leave", None,
                                                ("--leave-recovery",), {"UF_REBOOT": "1", "UF_BUC": "1"})
        check("--leave-recovery: board boots the normal role",
              rc == 0 and brc == 44 and "STUB: topswap 0" in out and "boot role 0" in out,
              f"rc={rc} brc={brc}\n{out[-400:]}")

        print("Interrupted writes")
        cut = {"UF_BUC": "1", "UF_CUT_ERASES": "5"}
        rc, out, brc, before, after = run_board(stub, tmp, base, "cut", rt_new,
                                                ("--write-protected",), cut)
        cut_path = os.path.join(tmp, "flash-cut.bin")
        open(cut_path, "wb").write(after)
        check("power cut during a --write-protected run: the bootblock's reset vector block is "
              "blank", brc == 49 and after[FLASH - BLOCK:] == b"\xff" * BLOCK,
              f"brc={brc}\n{out[-400:]}")
        board = Board(stub, cut_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "300",
                                       "UF_WINDOW_MS": "300", "UF_MAX_IDLES": "2"})
        brc = board.wait(20)
        check("after the cut, idle keeps Top Swap: the bootblock can't start",
              brc == 47 and "idle refused 17" in board.log and "topswap 0" not in board.log,
              f"brc={brc}\n{board.log[-300:]}")
        board = Board(stub, cut_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "3000",
                                       "UF_MAX_IDLES": "1"})
        rc, out = tool(board.port, "--leave-recovery")
        brc = board.wait()
        check("after the cut, --leave-recovery is refused and the board stays in recovery",
              rc == 1 and brc == 47 and "can't start" in out and "topswap 0" not in board.log,
              f"rc={rc} brc={brc}\n{out[-300:]}\n{board.log[-300:]}")
        board = Board(stub, cut_path, {"UF_BUC": "1"})
        rc, out = tool(board.port, rt_new, "--write-protected")
        brc = board.wait()
        check("after the cut, rerunning the tool completes the write",
              rc == 0 and brc == 42 and regions_equal(open(cut_path, "rb").read(), rt_newb, written),
              f"rc={rc} brc={brc}\n{out[-300:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "cut-cb", new, (),
                                                {"UF_BUC": "1", "UF_CUT_ERASES": "3"})
        cut_path = os.path.join(tmp, "flash-cut-cb.bin")
        open(cut_path, "wb").write(after)
        check("power cut during a recovery COREBOOT write: the bootblock's reset vector block "
              "is blank, the rest of it intact", brc == 49 and after[FLASH - BLOCK:] == b"\xff" * BLOCK
              and after[TOP[0]:FLASH - BLOCK] == before[TOP[0]:FLASH - BLOCK], f"brc={brc}\n{out[-400:]}")
        board = Board(stub, cut_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "300",
                                       "UF_WINDOW_MS": "300", "UF_MAX_IDLES": "2"})
        brc = board.wait(20)
        check("after that cut and a reboot, idle keeps Top Swap",
              brc == 47 and "idle refused 17" in board.log, f"brc={brc}\n{board.log[-300:]}")
        board = Board(stub, cut_path, {"UF_BUC": "1"})
        rc, out = tool(board.port, new)
        brc = board.wait()
        check("after that cut, a rerun restores the bootblock and completes the write",
              rc == 0 and brc == 42 and regions_equal(open(cut_path, "rb").read(), newb, code + [TOP]),
              f"rc={rc} brc={brc}\n{out[-300:]}")
        fm_new, fm_newb = changed(newb, [fmap[0] + 0x800 - 0x123], tmp, "fm.rom")
        rc, out, brc, before, after = run_board(stub, tmp, base, "cut-fmap", fm_new,
                                                ("--write-protected",), {"UF_BUC": "1", "UF_CUT_ERASES": "2"})
        cut_path = os.path.join(tmp, "flash-cut-fmap.bin")
        open(cut_path, "wb").write(after)
        check("power cut during the FMAP write: the FMAP block and the bootblock's reset vector "
              "block are blank", brc == 49 and after[fmap[0]:fmap[1]] == b"\xff" * (fmap[1] - fmap[0])
              and after[FLASH - BLOCK:] == b"\xff" * BLOCK, f"brc={brc}\n{out[-400:]}")
        board = Board(stub, cut_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "300",
                                       "UF_WINDOW_MS": "300", "UF_MAX_IDLES": "2"})
        brc = board.wait(20)
        check("after the FMAP cut and a reboot, idle keeps Top Swap",
              brc == 47 and "idle refused" in board.log and "topswap 0" not in board.log,
              f"brc={brc}\n{board.log[-300:]}")
        board = Board(stub, cut_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "3000",
                                       "UF_MAX_IDLES": "1"})
        rc, out = tool(board.port, "--leave-recovery")
        brc = board.wait()
        check("after the FMAP cut, --leave-recovery is refused",
              rc == 1 and brc == 47 and "refused" in out and "topswap 0" not in board.log,
              f"rc={rc} brc={brc}\n{out[-300:]}")
        fmonly = bytearray(base)
        fmonly[fmap[0]:fmap[1]] = b"\xff" * (fmap[1] - fmap[0])
        fm_path, _ = make_flash(tmp, bytes(fmonly), "flash-fmonly.bin")
        board = Board(stub, fm_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "300",
                                      "UF_WINDOW_MS": "300", "UF_MAX_IDLES": "2"})
        brc = board.wait(20)
        check("a blank FMAP with an intact bootblock: idle keeps Top Swap (reason 19)",
              brc == 47 and "idle refused 19" in board.log, f"brc={brc}\n{board.log[-300:]}")
        board = Board(stub, fm_path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "3000",
                                      "UF_MAX_IDLES": "1"})
        rc, out = tool(board.port, "--leave-recovery")
        brc = board.wait()
        check("a blank FMAP: --leave-recovery is refused",
              rc == 1 and brc == 47 and "FMAP is not" in out, f"rc={rc} brc={brc}\n{out[-300:]}")
        board = Board(stub, cut_path, {"UF_REBOOT": "1", "UF_BUC": "1"})
        rc, out = tool(board.port, fm_new, "--write-protected", "--leave-recovery")
        brc = board.wait()
        check("after the FMAP cut, a rerun completes and the board leaves recovery",
              rc == 0 and brc == 44 and regions_equal(open(cut_path, "rb").read(), fm_newb, [fmap])
              and "STUB: topswap 0" in board.log, f"rc={rc} brc={brc}\n{out[-300:]}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "cut-rec", rec_new,
                                                ("--write-recovery-block",), {"UF_CUT_ERASES": "1"})
        cut_path = os.path.join(tmp, "flash-cut-rec.bin")
        open(cut_path, "wb").write(after)
        board, link, h = session(stub, cut_path)
        p = link.request(t.TOPSWAP, b"\1")
        link.request(t.EXIT)
        board.wait()
        link.close()
        check("power cut while writing the recovery block: its reset vector is gone, Top Swap "
              "set is refused", after[TS[1] - BLOCK:TS[1]] == b"\xff" * BLOCK
              and p[:2] == bytes([t.ERR, 13]), f"p={p!r}")
        rc, out, brc, before, after = run_board(stub, tmp, base, "die", new, (),
                                                {"UF_REBOOT": "1", "UF_BUC": "1", "UF_DIE_ERASES": "3"})
        check("die() after a write in recovery: halts with Top Swap set",
              brc == 48 and "halted in Top Swap" in out and "topswap 0" not in out,
              f"brc={brc}\n{out[-300:]}")

        print("Idle return")
        path, before = make_flash(tmp, base, "flash-idle.bin")
        board = Board(stub, path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "500",
                                   "UF_WINDOW_MS": "300"})
        brc = board.wait(20)
        check("idle: recovery clears Top Swap and the board boots the normal role",
              brc == 44 and "STUB: idle" in board.log and "STUB: topswap 0\n" in board.log
              and "boot role 0" in board.log, f"brc={brc}\n{board.log[-300:]}")
        board = Board(stub, path, {"UF_REBOOT": "1", "UF_STRAP": "1", "UF_IDLE_MS": "300",
                                   "UF_MAX_IDLES": "2"})
        brc = board.wait(20)
        check("idle under the strap: recovery stays", brc == 47 and "unchanged" in board.log
              and "boot role 0" not in board.log, f"brc={brc}\n{board.log[-300:]}")
        board, link, h = session(stub, path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "1500",
                                             "UF_WINDOW_MS": "300"})
        link.request(t.HELLO)
        time.sleep(3)
        brc = board.wait(20)
        link.close()
        check("idle inside a session: recovery clears Top Swap after the last request",
              brc == 44 and "STUB: idle 0" in board.log, f"brc={brc}\n{board.log[-300:]}")
        board, link, h = session(stub, path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "1500",
                                             "UF_WINDOW_MS": "300", "UF_MAX_IDLES": "2"})
        st = write(link, cb_lo)
        time.sleep(4)
        brc = board.wait(20)
        link.close()
        check("idle after a write in this boot: recovery keeps Top Swap",
              st[0] == t.OK and brc == 47 and "idle refused 16" in board.log
              and "topswap 0" not in board.log, f"st={st} brc={brc}\n{board.log[-300:]}")
        path, before = make_flash(tmp, base, "flash-idle.bin")
        nob = bytearray(open(path, "rb").read())
        nob[FLASH - 16:] = b"\xff" * 16
        open(path, "wb").write(nob)
        board = Board(stub, path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "300",
                                   "UF_WINDOW_MS": "300", "UF_MAX_IDLES": "2"})
        brc = board.wait(20)
        check("idle with no reset vector in the bootblock: recovery keeps Top Swap",
              brc == 47 and "idle refused 17" in board.log, f"brc={brc}\n{board.log[-300:]}")
        path, before = make_flash(tmp, base, "flash-idle.bin")
        board, link, h = session(stub, path, {"UF_REBOOT": "1", "UF_BUC": "1", "UF_IDLE_MS": "1500",
                                             "UF_WINDOW_MS": "300"})
        end = time.monotonic() + 3
        while time.monotonic() < end:
            link.write(bytes(random.Random(2).choice(range(0x10, 0xa0)) for _ in range(64)))
            time.sleep(0.02)
        brc = board.wait(20)
        link.close()
        check("idle with noise on the line: recovery still clears Top Swap",
              brc == 44 and "STUB: idle" in board.log, f"brc={brc}\n{board.log[-300:]}")
        check("idle: flash unchanged", open(path, "rb").read() == before)
    finally:
        shutil.rmtree(tmp)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
