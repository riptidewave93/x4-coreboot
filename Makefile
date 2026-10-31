# coreboot release tarballs; the blobs one carries 3rdparty/fsp, intel-microcode, blobs
export COREBOOT_VER := 26.09
export COREBOOT_SHA256 := cd980020c811627fe816eee933de15723681c9bcf370f7a680c41f0663c207da
export COREBOOT_BLOBS_SHA256 := 5f905829a405d5bca674b6e5ceaf20516f8016d94768e8ecd50eccf061cedc0f

# EDK2 payload: the fork and tag coreboot $(COREBOOT_VER) defaults to
export EDK2_REPO := https://github.com/mrchromebox/edk2
export EDK2_VER := 26.09.1
export EDK2_COMMIT := 57642656c3ddc5d55aeb2e8c3fb86c81276da4a7

# iPXE: its Intel driver is edk2's NIC driver for the I226-V (I226 IDs since d73981aec)
export IPXE_REPO := https://github.com/ipxe/ipxe
export IPXE_COMMIT := 1573f8c084ffd9e763992a6a739f308a9edd95a1

# Version of the X4 recovery block, built timeless so its bytes depend only on its sources.
# Bump it with configs/recovery-bootblock.sha256 when the block changes.
export RECOVERY_VERSION := x4-recovery-7

# Board config to build: configs/<BOARD>.defconfig
BOARD ?= radxa-x4
# 1: add configs/debug.fragment (SPEW console), output as <BOARD>-debug
DEBUG ?= 0
# Builds of each board kept in ./output (0: keep all)
KEEP ?= 3
# Release version without the v, set by CI for releases: the coreboot version becomes
# $(COREBOOT_VER)-x4-v$(RELEASE). An explicit KERNELVERSION wins.
RELEASE ?=
ifneq ($(RELEASE),)
KERNELVERSION ?= $(COREBOOT_VER)-x4-v$(RELEASE)
endif

DOCKER_IMAGE := x4-coreboot-builder:latest
DOCKER_NAME := x4-coreboot-builder
DOCKER_PLATFORM := linux/amd64

REPO_DIR := $(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
DOCKER_RUN := docker run --rm $(shell [ -t 0 ] && echo -it) --platform $(DOCKER_PLATFORM) \
	--user $(shell id -u):$(shell id -g) -e HOME=/tmp \
	$(if $(KERNELVERSION),-e KERNELVERSION="$(KERNELVERSION)") \
	-v "$(REPO_DIR)":/repo:Z -w /repo --name $(DOCKER_NAME) $(DOCKER_IMAGE)

.PHONY: build fetch toolchain menuconfig savedefconfig export-patches shell \
	test-uart-flash lint \
	builder-docker clean reset

# make [BOARD=radxa-x4] [DEBUG=1]: build configs/$(BOARD).defconfig into ./output
build : builder-docker
	$(DOCKER_RUN) make builder-build BOARD=$(BOARD) KEEP=$(KEEP) DEBUG=$(DEBUG)

# Download everything into ./preserve/dl without building
fetch : builder-docker
	$(DOCKER_RUN) make builder-fetch

# coreboot cross toolchain into ./preserve/toolchain (build does this when needed)
toolchain : builder-docker
	$(DOCKER_RUN) make builder-toolchain

# Edit configs/$(BOARD).defconfig (saved back on exit)
menuconfig : builder-docker
	$(DOCKER_RUN) make builder-menuconfig BOARD=$(BOARD)

# Normalise configs/$(BOARD).defconfig
savedefconfig : builder-docker
	$(DOCKER_RUN) make builder-savedefconfig BOARD=$(BOARD)

# Write commits made in BuildEnv's tree to patches/<tree>/: make export-patches [TREE=edk2]
TREE ?= coreboot
export-patches : builder-docker
	$(DOCKER_RUN) make builder-export-patches TREE=$(TREE)

# Test the X4 UART flash mode end to end (board core on a pty): make test-uart-flash
test-uart-flash : builder-docker
	$(DOCKER_RUN) python3 tests/uart_flash/run_tests.py

# Check the scripts: shellcheck, and Python syntax
lint : builder-docker
	$(DOCKER_RUN) sh -c 'shellcheck -x scripts/*.sh && python3 -m py_compile scripts/*.py tests/uart_flash/*.py'

# Interactive shell in the builder
shell : builder-docker
	docker run --rm -it --platform $(DOCKER_PLATFORM) --user $(shell id -u):$(shell id -g) \
		-e HOME=/tmp -v "$(REPO_DIR)":/repo:Z -w /repo $(DOCKER_IMAGE) /bin/bash

builder-docker :
	docker build --platform $(DOCKER_PLATFORM) -t $(DOCKER_IMAGE) .

# Targets below run inside the container
.PHONY: builder-build builder-fetch builder-toolchain builder-menuconfig \
	builder-savedefconfig builder-export-patches

builder-build :
	DEBUG=$(DEBUG) ./scripts/build.sh $(BOARD) build

builder-fetch :
	./scripts/fetch.sh

builder-toolchain :
	./scripts/fetch.sh coreboot
	./scripts/setup_builddir.sh
	./scripts/toolchain.sh

builder-menuconfig :
	./scripts/build.sh $(BOARD) menuconfig

builder-savedefconfig :
	./scripts/build.sh $(BOARD) savedefconfig

builder-export-patches :
	./scripts/export_patches.sh $(TREE)

# Keeps the download and toolchain cache in ./preserve
clean :
	rm -rf ./BuildEnv ./output

# clean + drop ./preserve and the builder image
reset : clean
	rm -rf ./preserve
	-docker image rm $(DOCKER_IMAGE)
