#!/bin/bash
# Configure and build configs/<board>.defconfig, or edit that defconfig:
#   [DEBUG=1] build.sh <board> build|menuconfig|savedefconfig
# DEBUG=1 adds configs/debug.fragment and builds as <board>-debug. An X4 build first builds
# the recovery block (configs/recovery.fragment) and puts it in the Top Swap block.
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
board="${1:?board}"
action="${2:-build}"
defconfig="${ourpath}/configs/${board}.defconfig"
[ -f "${defconfig}" ] || die "no configs/${board}.defconfig"
name="${board}"
if [ "${DEBUG:-0}" = 1 ]; then
    [ "${action}" = build ] || die "DEBUG=1 only builds; edit configs/debug.fragment by hand"
    name="${board}-debug"
fi

"${ourpath}/scripts/fetch.sh"
"${ourpath}/scripts/setup_builddir.sh" coreboot edk2
"${ourpath}/scripts/toolchain.sh"

tc="${ourpath}/preserve/toolchain/$(cat "${cbdir}/.crossgcc-version")/bin"
export PATH="${tc}:${PATH}"
obj="build/${name}"
cb_make() { make -C "${cbdir}" obj="${obj}" DOTCONFIG="${obj}/.config" XGCCPATH="${tc}/" "$@"; }

mkdir -p "${cbdir}/${obj}"
if [ "${name}" != "${board}" ]; then
    cat "${defconfig}" "${ourpath}/configs/debug.fragment" > "${cbdir}/${obj}/defconfig.in"
    defconfig="${cbdir}/${obj}/defconfig.in"
fi
cb_make defconfig KBUILD_DEFCONFIG="${defconfig}" >/dev/null
case "${action}" in
menuconfig) cb_make menuconfig ;&
savedefconfig)
    cb_make savedefconfig
    cp "${cbdir}/${obj}/defconfig" "${defconfig}"
    echo "Saved configs/${board}.defconfig"
    exit 0 ;;
build) ;;
*) die "unknown action '${action}'" ;;
esac

# The recovery block: the top 256KB of a timeless recovery build, the same for every build
if grep -q '^CONFIG_BOARD_RADXA_X4=y' "${cbdir}/${obj}/.config"; then
    rec="build/${board}-recovery"
    mkdir -p "${cbdir}/${rec}" "${buildenv}/recovery"
    cat "${ourpath}/configs/${board}.defconfig" "${ourpath}/configs/recovery.fragment" \
        > "${cbdir}/${rec}/defconfig.in"
    rec_make() { make -C "${cbdir}" obj="${rec}" DOTCONFIG="${rec}/.config" XGCCPATH="${tc}/" "$@"; }
    rec_make defconfig KBUILD_DEFCONFIG="${cbdir}/${rec}/defconfig.in" >/dev/null
    echo "Building ${board}-recovery (${RECOVERY_VERSION})"
    rec_make -j"$(nproc)" BUILD_TIMELESS=1 KERNELVERSION="${RECOVERY_VERSION}" >/dev/null
    dd if="${cbdir}/${rec}/coreboot.rom" of="${buildenv}/recovery/recovery-bootblock.bin" \
        bs=4K skip=$((0xFC0)) count=64 status=none
    "${cbdir}/util/scripts/config" --file "${cbdir}/${obj}/.config" \
        --set-str RADXA_X4_RECOVERY_BOOTBLOCK_FILE "${buildenv}/recovery/recovery-bootblock.bin"
    cb_make olddefconfig >/dev/null
fi

if grep -q '^CONFIG_PAYLOAD_EDK2=y' "${cbdir}/${obj}/.config"; then
    grep -qxF "CONFIG_EDK2_REPOSITORY=\"${EDK2_REPO}\"" "${cbdir}/${obj}/.config" ||
        die "configs/${board}.defconfig uses another EDK2 repository than ${EDK2_REPO}"
    # Our patched EDK2 tree (setup_builddir.sh), not the upstream tag
    "${cbdir}/util/scripts/config" --file "${cbdir}/${obj}/.config" --set-str EDK2_TAG_OR_REV x4-coreboot
    cb_make olddefconfig >/dev/null
fi

# iPXE's Intel driver in edk2's NIC driver slot (UefiPayloadPkg NETWORK_UNDI_DRIVER)
if grep -q '^CONFIG_EDK2_CUSTOM_BUILD_PARAMS=.*NETWORK_UNDI_DRIVER=TRUE' "${cbdir}/${obj}/.config"; then
    "${ourpath}/scripts/ipxe.sh"
    cp "${buildenv}/ipxe/intel.efidrv" "${edk2dir}/UefiPayloadPkg/NetworkDrivers/UndiDxe.efi"
fi

echo "Building ${name}"
cb_make -j"$(nproc)"
"${ourpath}/scripts/collect_output.sh" "${name}"
