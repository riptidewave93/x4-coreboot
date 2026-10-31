# Radxa X4 board reference

Facts about the Radxa X4 (Intel N100) that the code and Radxa's v1.11 schematic don't show. Hardware checks were read-only, on the 8 GB model.

## Stock firmware

| Item | Value |
|---|---|
| Image | Radxa BIOS V007, AMI Aptio V 5.27: [zip](https://dl.radxa.com/x/x4/Radxa_X4_BIOS_V007.zip), sha256 `8261e86d40ff7733211c64898dfb0ce947b94d2fcd50259bc87ccdd67db07082` |
| `EFI/X4-V007.bin` in the zip (16 MB) | sha256 `cd89121a9fd0ac30147bc4387ea8764a0ec5296e894fe7ee6afc45053d218e79` |
| CSME | 16.50.12.1453, Consumer LP |
| FSP | 12.2.137.48. coreboot uses the IoT ADL-N FSP 12.226.138.64, which has a different UPD layout |
| Flash tool | Intel `Fpt.efi` 16.50.0.7105, in the zip |

A factory image holds per-unit data only in the ME data, the NVRAM banks and the DMI store (`0x670000`, Radxa's SMBIOS serial). A BIOS reflash erases the last two.

## Flash

Winbond W25Q128JV, 16 MB, 3.3 V.

| Region | Range |
|---|---|
| Descriptor | `0x000000-0x000FFF` |
| ME | `0x001000-0x45BFFF` |
| Device expansion (erased) | `0x45C000-0x5FFFFF` |
| BIOS | `0x600000-0xFFFFFF` (10 MB) |

coreboot's BIOS region layout is `board.fmd`:

| Area | Range |
|---|---|
| `RW_MRC_CACHE` (memory training, 62 KB of data on the 8 GB model) | `0x600000-0x61FFFF` |
| `SMMSTORE` (UEFI variables: setup settings, Secure Boot keys) | `0x620000-0x71FFFF` |
| `FMAP` | `0x720000-0x720FFF` |
| `COREBOOT` (CBFS) | `0x721000-0xFFFFFF` |

Inside CBFS, the raw file `recovery-bootblock` holds the recovery bootblock in the Top Swap block `0xF80000-0xFBFFFF`, with its own FIT and microcode. The normal bootblock is at the top.

- **Descriptor:** every FLMSTR is `0xFFFFFFFF`, so the host can read and write every region.
- **Boot Guard and AMI Secure Flash are not provisioned** (MSR `0x13A` bit 0 = 0, empty manifests), so coreboot can run on this board.
- **Locks in coreboot:** the protected ranges and BIOS Lock Enable stay off (`BOOTMEDIA_LOCK_NONE`), so Fpt can write the BIOS region from an EFI shell. Stock V007 behaves the same.

## Board notes

- **Memory:** every model is one LPDDR5X package on two channels. GPP_E10/E11 pick the SPD. The schematic's strap table (page 15) is older than the 12 GB model and doesn't match the firmware; the port follows the firmware.
- **eMMC strap:** GPP_B16 (`EN_EMMC`) reads the same with and without eMMC, so it can't detect the eMMC. On boards without eMMC, firmware and Linux just log one failed probe (untested).
- **HDMI:** the schematic's net names are swapped against the silkscreen: J2 (HDMI1, next to the USB-C jack) has the `HDMI2_*` nets and runs from Type-C PHY 2; J3 (HDMI2) has the `HDMI_*` nets and runs from DDI B.
- **VBT:** the FSP's display driver needs a BDB 256 VBT and shows nothing with stock's BDB 251. The board's `data.vbt` is the stock VBT converted to BDB 256.
- **M.2 lane split:** descriptor strap PCHSTRP28 bits 12:11 (offset `0x170`) sets it: `11` is one x4 link (stock), `00` is four x1 links. Firmware can't change it at boot.
- **USB-C jack:** 12 V power input only (PD sink), no data or video.
- **Fan:** wired straight to 5 V; it always runs.
- **RP2040:** shares the platform reset and has its own flash; it can't reach the host SPI flash.
- **Top Swap strap** (GPP_B14): its pull-up, [R179](FLASHING.md#recovery), isn't fitted. With one fitted, the CPU starts from the 256 KB Top Swap block `0xF80000-0xFBFFFF` (size set by the descriptor) instead of the top 256 KB.
- **Top Swap by software:** BUC.TS (PCR RTC `0x3414` bit 0) sets Top Swap too, at once. It lives in the RTC well, so resets keep it and only an RTC reset clears it: 12 V and the coin cell (J1) out for 60 s. With the strap fitted the bit reads 1 and can't be cleared, per Intel's datasheet. The bootblock, which runs before FSP, can set and clear it. The EFI shell can't: the RTC PCR endpoint (`0xFDC33400-0xFDC3341F`) reads all 0xFF there.

## PCI devices

| Device | ID | Location |
|---|---|---|
| Wi-Fi, Realtek RTL8852BE (M.2) | `10EC:B852` | behind `00:1C.0` (root port 4; shows as `1C.0` because root port 1 is off) |
| 2.5 GbE, Intel I226-V | `8086:125C` rev 04 | behind `00:1C.6` (root port 7) |
| NVMe (M.2 M-key) | – | root port 9 |
| eMMC controller | `8086:54C4` | `00:1A.0` |
| Audio | Realtek ALC269Q | HDA |
