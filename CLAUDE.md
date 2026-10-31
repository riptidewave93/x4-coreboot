# Contributing to x4-coreboot

Rules for anyone working in this repo, people and AI agents alike.

coreboot 26.09 with an EDK2 payload (MrChromebox UefiPayloadPkg) for the Radxa X4 (Intel N100, Alder Lake-N), replacing Radxa's stock AMI BIOS. Goal: a daily-use firmware with working Secure Boot.

| Doc | What |
|---|---|
| [`README.md`](README.md) | Status, build, setup, patch workflow |
| [`FLASHING.md`](FLASHING.md) | Fpt flashing, UART flash mode, recovery |
| [`FINDINGS.md`](FINDINGS.md) | Board and stock-firmware facts |
| [`TODO.md`](TODO.md) | Open work |

## Hardware safety

- **Write only the BIOS region** (`bios-region.bin`). The descriptor and ME stay stock.
- **Back up before the first flash on any board:** two full reads with `Fpt.efi -d`, matching sha256, kept off the board and outside the repo.
- **Have a way back before you flash.** A debug build can be reflashed over UART if its bootblock runs, and every build from its recovery block (FLASHING.md, "Recovery"). A production build reaches recovery only with R179 fitted, otherwise only an SPI programmer helps.
- **Flash only a saved build** (it passed `scripts/check_x4_image.py`) whose files match its `SHA256SUMS`.
- **UART flash mode can't rewrite the FMAP or the bootblock's 256 KB,** and doesn't move the FIT or microcode. It writes the recovery block only with `--write-recovery-block`. Only the recovery bootblock writes the FMAP and the bootblock (`--write-protected`). Otherwise a new build may go over UART only if those match the board's; `scripts/x4_uart_flash.py` refuses the rest. They change with:
  - **the build type:** the log level is built in, so use `make DEBUG=1`;
  - **the version string:** pin it with `make KERNELVERSION=<version>`, read from the board's boot log;
  - **the build date:** it comes from HEAD of `BuildEnv/coreboot`, so keep the dates of existing patches;
  - **the devicetree,** the flash layout, and anything else built into the bootblock.

## Build system

Usage is in [`README.md`](README.md).

- **Container:** always `linux/amd64`, because EDK2 builds with the host gcc.
- **Pins** are in the `Makefile`: coreboot 26.09 release tarballs with the blobs (FSP, microcode), MrChromebox EDK2 26.09.1 and iPXE. The build downloads nothing from Radxa.
- **Network boot:** EDK2's NetworkPkg (`CONFIG_EDK2_CUSTOM_BUILD_PARAMS`) with iPXE's `intel.efidrv` as the NIC driver, built by `scripts/ipxe.sh`. Don't extract or ship the Intel UNDI driver from the stock BIOS: Intel licenses it to OEMs only.
- **Cache:** `preserve/` holds downloads (`dl/`, sha256-checked) and the toolchain (`toolchain/`). `make clean` keeps it; `make reset` drops it. The toolchain has no Ada, so there is no libgfxinit: FSP GOP drives the display.
- **Trees:** `BuildEnv/coreboot` and the EDK2 workspace are local git trees: the release tag plus `patches/<tree>/` as commits with fixed dates. A fresh clone builds only what is in `patches/`, so export your commits (README, "Development"). `build.sh` points `CONFIG_EDK2_TAG_OR_REV` at the local tag `x4-coreboot`.
- **Configs:** `configs/radxa-x4.defconfig` is the production build: log level 5, CBMEM console only, no UART flash mode, EDK2's graphical setup UI (`EDK2_USE_LVGL_RENDERER` defaults on without EDK2 serial). `make DEBUG=1` adds `configs/debug.fragment` (serial console, UART flash mode, log level 8, text setup UI) and builds as `radxa-x4-debug`.
- **Recovery block:** `build.sh` first builds `radxa-x4-recovery` (the production defconfig plus `configs/recovery.fragment`) with `BUILD_TIMELESS=1` and `KERNELVERSION=$(RECOVERY_VERSION)`. Its top 256 KB goes into the Top Swap block of every build. `configs/recovery-bootblock.sha256` pins it: a change to bootblock code fails the image check until you update the pin and `RECOVERY_VERSION`.
- **Image check:** `scripts/check_x4_image.py` checks every image against the stock descriptor's regions (pinned in the script), the board layout and the recovery block pin, and writes `bios-region.bin` and `flashprog-layout.txt`. A build that fails it isn't saved.
- **Tests:** `make test-uart-flash` runs the UART flash protocol, in both roles, against the host tool over a pty. `make lint` runs shellcheck and a Python syntax check.
- **CI:** `.github/workflows/ci.yml` runs `make lint`, `make` and `make test-uart-flash` on pull requests and `main`. `make DEBUG=1` runs only in a manual run with the debug build type. CI calls only make targets, so anything it does works locally the same way.
- **Releases:** Release-Please (`release-please-config.json`) reads the commit subjects on `main` and keeps a release PR open: `feat` bumps the minor version, `fix` the patch version, other types don't release. Merging the PR tags `vX.Y.Z` with a draft release. CI builds that commit with `make RELEASE=X.Y.Z`, which sets the coreboot version to `26.09-x4-vX.Y.Z`, then attaches the production build and publishes the release. The same command rebuilds a release locally.
- **UART flash ranges** live only in the board's `uart_flash_proto.c`, which sends them by name in HELLO. `scripts/x4_uart_flash.py` holds no flash addresses: it reads the image's FMAP and FIT. `tests/uart_flash/stub.c` keeps a copy to catch drift, so change both together.
- **Other targets:** `make savedefconfig` normalises the defconfig. `KEEP=<n>` sets how many builds of each kind `output/` keeps (default 3, `0` keeps all).
- **Unexported work:** if `patches/` changes while a tree has commits or changes that weren't exported, the build stops instead of discarding them.
- **Setup options** are CFR forms that EDK2 stores as UEFI variables in SMMSTORE. EDK2 writes the defaults on its first boot; after that the stored values override Kconfig and the devicetree.
- **Vendor blobs** (ME, descriptor, FSP) stay out of git. Radxa's V007 zip is the reference for analysis only: the build must not need it.

## Patches and commits

- **Imported changes** (e.g. from Gerrit) keep their author, date, message and trailers. If you port or change one, add a bracketed note before the trailers saying what changed and for which coreboot release. Fixes beyond a port go in separate patches. `setup_builddir.sh` runs `git am --keep-non-patch`, so subject tags like `[WIP]` survive.
- **Upstream:** this repo doesn't send patches upstream; all fixes go into `patches/<tree>/`. Others may submit them (README, "Upstreaming"). `[LOCAL]` patches serve this repo only.
- **Keep the patch stack small:** group related changes in one patch (all devicetree fixes, or everything about one part such as the RP2040). Split only unrelated changes, and don't split grouped patches in reviews.
- **This repo's commits:** a conventional type and scope, then the effect in lowercase plain words, e.g. `fix(uart-flash): reconnect when the adapter drops off USB`. No AI co-author or "Generated with" trailers.
- **Patches in `patches/<tree>/`** use that project's subject style (`mb/radxa/x4: Turn off the TCSS xHCI`, `UefiPayloadPkg: …`), with a body to upstream standards: what the change does and why, for this commit only. No references to other patches in the series, plans, test narratives, or findings from other firmware or vendors.
- **Generic patches carry no product-specific motivation.** Only board-specific patches name the board.
- **Trailers:** patches written with Claude end with `[Generated by Claude, an AI model by Anthropic.]` and, except `[LOCAL]` ones, the user's own `Signed-off-by` line. Imported patches keep their own trailers.

## Public repo

Files, commit messages and patches must not contain:
- hostnames or domains of private machines, local paths, usernames, IPs;
- per-unit identifiers: serials, DMI UUIDs, MACs, hashes of board dumps;
- links to AI sessions.

Call the board used for testing "the test board". Reference only Radxa's factory V007 firmware. Examples use placeholder data.

## Writing style

Applies to docs, patches, commits and reports. Write for a busy human first, precise enough for an agent to act on.

- **Lead with the point.** The first sentence says what it is or what happened.
- **Plain words, short sentences.** Prefer colons, commas and full stops over dashes and parentheses. No jargon the reader didn't use first.
- **No filler:** "honestly", "genuinely", "actually", "really", "robust", "seamless", "comprehensive", "leverage", "it's worth noting", "in order to". No meta-talk ("Let me explain…").
- **Draft, then cut** about a third.
- **Facts only.** No speculation, "maybe" or "potential". State what was checked; say plainly what's untested.
- **Format:** bold lead-ins on bullets when a list mixes topics; tables for facts that compare.

**Docs**
- Only what a reader needs now: no history, finished plans, fixed bugs or research narrative. Git history holds the story; why a workaround exists goes in its commit or patch message.
- One home per topic; link to it instead of repeating it. Add a doc only when someone would otherwise get it wrong.
- Commands must work as written.
- README order: what it is, what you get, how to use it, a short developer section, the license.

**Code comments:** only for a non-obvious why (a constraint, a quirk, an ordering requirement), one or two lines, no change history. File headers at most 5 lines.

**Reports and questions:** outcome first, then only the details that matter. Name what's untested or assumed. A question gives one line of context, the choice and a recommendation, and is asked only when the answer changes what happens next.

**Final check:** have a fresh reviewer (or a fresh pass) check each doc or patch message against these rules, then grep it for private data: paths, usernames, IPs, serials, secrets.
