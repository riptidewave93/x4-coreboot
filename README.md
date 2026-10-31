# x4-coreboot

coreboot with a UEFI (EDK2) payload for the Radxa X4 (Intel N100), replacing Radxa's stock AMI BIOS.

> **Warning:** this firmware replaces the board's stock BIOS, and a failed or faulty flash can brick the board until it is reflashed with an SPI programmer. Use it at your own risk: the authors accept no liability for damaged or bricked hardware.

## What you get

Tested on one board: the 8 GB model with eMMC.

| Feature | State |
|---|---|
| UEFI boot from USB, NVMe, eMMC; UEFI Shell built in | Works |
| Both micro-HDMI ports | Works |
| [Setup menu](#setup) | Works |
| Secure Boot, Microsoft keys, off by default | Works |
| TPM 2.0 (firmware TPM) | Works |
| Unique SMBIOS serial number and UUID | Works |
| EIST, PL1 13 W / PL2 25 W | Works |
| Network boot (PXE, HTTP), off by default | Untested |
| Flashing with Fpt from the EFI shell | Works |
| [Recovery](FLASHING.md#recovery) over UART, also for production builds | Works |
| S3 sleep | Works |
| Ethernet (2.5 GbE) in an OS | Works |
| Wi-Fi, Bluetooth, audio in an OS | Untested |
| Other RAM sizes, boards without eMMC | Untested |

## Build

You need Docker and make. The build downloads everything else.

```sh
make            # production build
make DEBUG=1    # debug build
```

| | Production | Debug |
|---|---|---|
| Boot log | In memory only (`cbmem -c`) | Also on UART0, 115200 8N1 |
| Extra boot time | None | About 25 s, to send the log over UART |
| Setup screen | Graphical | Text |
| Reflash over UART | From [recovery](FLASHING.md#recovery), with R179 | Yes |

Each build lands in `output/<date>-radxa-x4/` (or `…-debug/`) with `bios-region.bin`, `backup.nsh`, `install.nsh` and `SHA256SUMS`.

## Flash

Download a build from [Releases](../../releases) or make your own, then follow [`FLASHING.md`](FLASHING.md).

## Setup

Press **F2** at the logo, then System Configuration → Platform Setup Menu. F9 loads the defaults, F10 saves, and leaving setup after a change resets the board. Boot order is under Boot Manager.

| Option | Default |
|---|---|
| Onboard eMMC, Wi-Fi, Bluetooth | On |
| Network boot | Off |
| VT-d | On |
| CPU PL1 / PL2 (6-25 W) | 13 W / 25 W |
| Package power limit lock | Off |
| PCIe ASPM / clock power management | Off / On |

## Development

| Path | What |
|---|---|
| `configs/` | `radxa-x4.defconfig` (production) and `debug.fragment` (added by `DEBUG=1`) |
| `patches/coreboot/`, `patches/edk2/` | Our changes, as patch series on the release tags |
| `scripts/` | Build, image check and UART flash tools |
| `BuildEnv/` | Working trees, created by the build |
| `preserve/` | Download and toolchain cache |

To change coreboot or EDK2, commit in its working tree (`BuildEnv/coreboot`, or the EDK2 tree under it), then run `make export-patches` (`TREE=edk2` for EDK2). Other targets: `make menuconfig`, `make shell`, `make clean` (keeps the cache), `make reset` (drops it). Board facts are in [`FINDINGS.md`](FINDINGS.md), contributor rules in [`CLAUDE.md`](CLAUDE.md).

**Upstreaming:** take, change and submit any patch in `patches/` except the `[LOCAL]` ones. Most of the code was written with Claude Code, so expect to clean it up for review.

## License

GPL-2.0, see [`LICENSE`](LICENSE). Patches keep the license of the files they change.
