/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Host build of the X4 UART flash core: a pty as UART0, a 16 MB file as NOR flash. With
 * UF_REBOOT set, resets and Top Swap changes restart it in the role BUC.TS selects.
 * UF_DIE_ERASES=n calls die() after n erases; UF_CUT_ERASES=n cuts the power after n.
 */

#include <fcntl.h>
#include <poll.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include "uart_flash_proto.h"

static int fd;
static uint8_t *flash;
static long drop_reply = -1, corrupt_rx = -1, rx_count, frames_out;
static long erases, writes, violations;
static uint8_t txbuf[16 + 4 * UF_MAX_CRCS + UF_BLOCK];
static size_t txlen;
static int buc, strap, ts_locked, reboot, idles, max_idles, swapped;
static long die_erases, cut_erases;
static enum uf_role role;
static jmp_buf boot;
static bool written;
static uint8_t unlocked;	/* from the valid UNLOCK requests of this session */
static int fmap_bad;		/* normal role: the session may not write at all */
static struct uf_ops ops;

static void finish(int rc);
static void die_now(void);

/* The board's ranges per role, kept independent of uart_flash_proto.c on purpose */
struct stub_range {
	uint32_t start, end;
	int kind, bit, flags;
	const char *name;
};

#define FM	UF_F_FMAP
static const struct stub_range normal_tab[] = {
	{ 0x000000, 0x001000, UF_K_PROTECTED, 0, FM | UF_F_KEEP, "SI_DESC" },
	{ 0x001000, 0x45c000, UF_K_PROTECTED, 0, FM | UF_F_KEEP, "SI_ME" },
	{ 0x45c000, 0x600000, UF_K_PROTECTED, 0, UF_F_KEEP, "region 9" },
	{ 0x600000, 0x620000, UF_K_WRITABLE, 0, FM | UF_F_DATA, "RW_MRC_CACHE" },
	{ 0x620000, 0x720000, UF_K_WRITABLE, 0, FM | UF_F_DATA, "SMMSTORE" },
	{ 0x720000, 0x721000, UF_K_PROTECTED, 0, FM, "FMAP" },
	{ 0x721000, 0xf80000, UF_K_WRITABLE, 0, FM, "COREBOOT" },
	{ 0xf80000, 0xfc0000, UF_K_UNLOCK, 4, UF_F_RESET | UF_F_TS, "recovery block" },
	{ 0xfc0000, 0x1000000, UF_K_PROTECTED, 0, UF_F_RESET, "bootblock" },
};
static const struct stub_range recovery_tab[] = {
	{ 0x000000, 0x001000, UF_K_PROTECTED, 0, FM | UF_F_KEEP, "SI_DESC" },
	{ 0x001000, 0x45c000, UF_K_PROTECTED, 0, FM | UF_F_KEEP, "SI_ME" },
	{ 0x45c000, 0x600000, UF_K_WRITABLE, 0, UF_F_FULL, "region 9" },
	{ 0x600000, 0x620000, UF_K_WRITABLE, 0, FM | UF_F_DATA, "RW_MRC_CACHE" },
	{ 0x620000, 0x720000, UF_K_WRITABLE, 0, FM | UF_F_DATA, "SMMSTORE" },
	{ 0x720000, 0x721000, UF_K_UNLOCK, 1, FM, "FMAP" },
	{ 0x721000, 0xf80000, UF_K_WRITABLE, 0, FM, "COREBOOT" },
	{ 0xf80000, 0xfc0000, UF_K_PROTECTED, 0, UF_F_KEEP | UF_F_RESET | UF_F_TS, "recovery block" },
	{ 0xfc0000, 0x1000000, UF_K_UNLOCK, 2, UF_F_RESET, "bootblock" },
};

static const struct stub_range *tab(enum uf_role r, size_t *n)
{
	*n = r == UF_ROLE_RECOVERY ? sizeof(recovery_tab) / sizeof(recovery_tab[0])
				   : sizeof(normal_tab) / sizeof(normal_tab[0]);
	return r == UF_ROLE_RECOVERY ? recovery_tab : normal_tab;
}

/* Violations: writes into a range the role never writes, or one this session didn't unlock */
static int allowed(uint32_t off, size_t len)
{
	size_t n;
	const struct stub_range *r = tab(role, &n);

	if (fmap_bad)
		return 0;
	for (size_t i = 0; i < n; i++) {
		if (off >= r[i].end || off + len <= r[i].start)
			continue;
		if (r[i].kind == UF_K_PROTECTED)
			return 0;
		if (r[i].kind == UF_K_UNLOCK && !(unlocked & r[i].bit))
			return 0;
	}
	return 1;
}

static uint32_t le32(uint32_t off);

/* The board would refuse this unlock: wrong magic, bits its table can't unlock, a normal-role
 * session with a moved FMAP, or the top block while flash reads follow Top Swap */
static void saw_unlock(const uint8_t *p, size_t len)
{
	size_t n;
	const struct stub_range *r = tab(role, &n);
	uint8_t bits = 0;

	for (size_t i = 0; i < n; i++)
		if (r[i].kind == UF_K_UNLOCK)
			bits |= r[i].bit;
	if (len != 17 || memcmp(p, "UNLOCK-PROTECTED", 16) || (p[16] & ~bits) || fmap_bad)
		return;
	if (role == UF_ROLE_RECOVERY && (p[16] & 2) && swapped)
		return;
	unlocked |= p[16];
}

/* Follows the frames the host sends, independent of uart_flash_proto.c */
static void snoop(uint8_t b)
{
	static uint8_t f[6 + 4 + UF_BLOCK + 4];
	static size_t got, want;

	if (got == 0 && b != 0xa5)
		return;
	if (got == 1 && b != 0x5a) {
		got = b == 0xa5;
		return;
	}
	f[got++] = b;
	if (got == 6) {
		want = 6 + (f[4] | f[5] << 8) + 4;
		if (want > sizeof(f))
			got = 0;
	}
	if (got < 6 || got < want)
		return;
	got = 0;
	uint32_t crc = f[want - 4] | f[want - 3] << 8 | f[want - 2] << 16 | (uint32_t)f[want - 1] << 24;
	if (crc == uf_crc32(0, f + 2, want - 6) && f[2] == UF_UNLOCK)
		saw_unlock(f + 6, want - 10);
}

/* The stub's own FMAP check: the signature and version at the FMAP block */
static int stub_fmap_bad(void)
{
	return memcmp(flash + 0x720000, "__FMAP__", 8) || flash[0x720008] != 1 ||
	       le32(0x720012) != 0x1000000;
}

static int s_rx(void)
{
	struct pollfd p = { fd, POLLIN, 0 };
	uint8_t b;

	if (poll(&p, 1, 1) <= 0 || read(fd, &b, 1) != 1)
		return -1;
	if (++rx_count == corrupt_rx)
		b ^= 0x10;
	snoop(b);
	return b;
}

/* Collects whole frames so one can be dropped to simulate a lost reply */
static void s_tx(uint8_t b)
{
	txbuf[txlen++] = b;
	if (txlen < 6)
		return;
	size_t total = 6 + (txbuf[4] | txbuf[5] << 8) + 4;
	if (txlen < total)
		return;
	if (++frames_out != drop_reply) {
		size_t done = 0;
		while (done < total) {
			ssize_t n = write(fd, txbuf + done, total - done);
			if (n > 0)
				done += n;
		}
	}
	txlen = 0;
}

static uint64_t s_now(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}

static int s_read(uint32_t off, void *buf, size_t len)
{
	if (off >= UF_FLASH_SIZE || len > UF_FLASH_SIZE - off)
		return -1;
	memcpy(buf, flash + off, len);
	return 0;
}

static int s_erase(uint32_t off, size_t len)
{
	erases++;
	if (!allowed(off, len) || off % UF_BLOCK || len != UF_BLOCK) {
		violations++;
		return -1;
	}
	memset(flash + off, 0xff, len);
	if (erases == cut_erases) {
		fprintf(stderr, "STUB: power cut\n");
		finish(49);
	}
	if (erases == die_erases)
		die_now();
	return 0;
}

static int s_write(uint32_t off, const void *buf, size_t len)
{
	const uint8_t *p = buf;

	writes++;
	if (!allowed(off, len)) {
		violations++;
		return -1;
	}
	for (size_t i = 0; i < len; i++)
		flash[off + i] &= p[i];	/* NOR: programming only clears bits */
	return 0;
}

static void finish(int rc)
{
	msync(flash, UF_FLASH_SIZE, MS_SYNC);
	fprintf(stderr, "STUB: erases=%ld writes=%ld violations=%ld\n", erases, writes,
		violations);
	exit(violations ? 99 : rc);
}

static void s_reset(void)
{
	fprintf(stderr, "STUB: reset\n");
	if (!reboot)
		finish(42);
	longjmp(boot, 1);
}

static void s_topswap(int on)
{
	if (ts_locked || (strap && !on)) {
		fprintf(stderr, "STUB: topswap %d unchanged\n", on);
		return;
	}
	buc = on;
	fprintf(stderr, "STUB: topswap %d\n", on);
	if (!reboot)
		finish(on ? 45 : 46);
	longjmp(boot, 1);
}

static int s_tss(void)
{
	return buc || strap;
}

static void s_idle(uint8_t reason)
{
	fprintf(stderr, "STUB: idle%s %d\n", reason ? " refused" : "", reason);
	if (max_idles && ++idles >= max_idles)
		finish(47);
	if (!reason)
		s_topswap(0);
}

/* As the board's die_notify(), then halt */
static void die_now(void)
{
	uint8_t reason = uf_leave_reason(&ops, false);

	fprintf(stderr, "STUB: die, reason %d\n", reason);
	if (!reason)
		s_topswap(0);
	fprintf(stderr, "STUB: halted in Top Swap\n");
	finish(48);
}

static uint32_t le32(uint32_t off)
{
	return flash[off] | flash[off + 1] << 8 | flash[off + 2] << 16 | (uint32_t)flash[off + 3] << 24;
}

/* The CPU sees the Top Swap block at the top while BUC.TS or the strap is set */
static uint32_t s_mmio_fit_ptr(void)
{
	int ts = (buc || strap) && !swapped;

	return le32(ts ? 0xfbffc0 : 0xffffc0);
}

static void say(const char *s)
{
	if (write(fd, s, strlen(s)) < 0)
		finish(98);
}

int main(int argc, char **argv)
{
	const char *e;
	int ffd, window, idle_ms;

	ops = (struct uf_ops){ s_rx, s_tx, s_now, s_read, s_erase, s_write, s_reset, s_topswap,
			       s_tss, s_tss, s_idle, s_mmio_fit_ptr, 0, UF_ROLE_NORMAL, NULL,
			       &written };
	if (argc == 3 && !strcmp(argv[1], "--ranges")) {
		/* This stub's own table, for the tests to compare with HELLO */
		size_t n;
		const struct stub_range *r = tab(atoi(argv[2]), &n);

		for (size_t i = 0; i < n; i++)
			printf("%x %x %d %d %d %s\n", r[i].start, r[i].end, r[i].kind, r[i].bit,
			       r[i].flags, r[i].name);
		return 0;
	}
	if (argc != 3) {
		fprintf(stderr, "usage: stub <pty-master-fd> <16MB-flash-file> | --ranges <role>\n");
		return 2;
	}
	fd = atoi(argv[1]);
	ffd = open(argv[2], O_RDWR);
	flash = mmap(NULL, UF_FLASH_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, ffd, 0);
	if (ffd < 0 || flash == MAP_FAILED)
		return 2;
	if ((e = getenv("UF_DROP_REPLY")))
		drop_reply = atol(e);
	if ((e = getenv("UF_CORRUPT_RX")))
		corrupt_rx = atol(e);
	window = (e = getenv("UF_WINDOW_MS")) ? atoi(e) : 2000;
	idle_ms = (e = getenv("UF_IDLE_MS")) ? atoi(e) : 600000;
	buc = (e = getenv("UF_BUC")) ? atoi(e) : 0;
	strap = !!getenv("UF_STRAP");
	ts_locked = !!getenv("UF_TS_LOCKED");
	reboot = !!getenv("UF_REBOOT");
	max_idles = (e = getenv("UF_MAX_IDLES")) ? atoi(e) : 0;
	swapped = !!getenv("UF_SWAPPED");
	die_erases = (e = getenv("UF_DIE_ERASES")) ? atol(e) : -1;
	cut_erases = (e = getenv("UF_CUT_ERASES")) ? atol(e) : -1;

	setjmp(boot);
	role = s_tss() ? UF_ROLE_RECOVERY : UF_ROLE_NORMAL;
	ops.role = role;
	ops.idle_ms = role == UF_ROLE_RECOVERY ? idle_ms : 0;
	ops.build_id = role == UF_ROLE_RECOVERY ? "stub-recovery" : "stub-normal";
	rx_count = 0;
	txlen = 0;
	written = false;
	fprintf(stderr, "STUB: boot role %d\n", role);
	if (role == UF_ROLE_NORMAL) {
		say("[NOTE ]  X4: send 'x4flash' within 2 s for UART flash mode\r\n");
		if (!uf_wait_magic(&ops, window)) {
			fprintf(stderr, "STUB: no magic\n");
			finish(44);
		}
		say("[NOTE ]  X4: UART flash mode\r\n");
		unlocked = 0;
		fmap_bad = stub_fmap_bad();
		uf_serve(&ops);
		fprintf(stderr, "STUB: exit to boot\n");
		finish(43);
	}
	for (;;) {
		say("[NOTE ]  X4 recovery stub: send 'x4flash' for UART flash mode\r\n");
		if (!uf_wait_magic(&ops, ops.idle_ms)) {
			s_idle(uf_leave_reason(&ops, false));
			continue;
		}
		say("[NOTE ]  X4 recovery: UART flash mode\r\n");
		unlocked = 0;
		fmap_bad = 0;
		uf_serve(&ops);
		fprintf(stderr, "STUB: recovery session ended\n");
	}
}
