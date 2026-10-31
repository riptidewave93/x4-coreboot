#!/bin/bash
# Copy a board's image and config to ./output/<date>-<board>/, keeping the last KEEP
# builds of that board (default 3, 0: all).
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
board="${1:?board}"
obj="${cbdir}/build/${board}"
out="${ourpath}/output/$(date +%Y%m%d-%H%M)-${board}"
keep=${KEEP:-3}

rm -rf "${out}"
mkdir -p "${out}"
cp "${obj}/coreboot.rom" "${out}/"
images=(coreboot.rom)
if grep -q '^CONFIG_BOARD_RADXA_X4=y' "${obj}/.config"; then
    python3 "${ourpath}/scripts/check_x4_image.py" "${obj}/coreboot.rom" "${obj}/.config" \
        "${out}/bios-region.bin" ||
        { rm -rf "${out}"; die "${board} image failed its checks"; }
    images+=(bios-region.bin)
    for nsh in backup.nsh install.nsh; do
        sed 's/$/\r/' "${ourpath}/scripts/${nsh}" > "${out}/${nsh}"
        images+=("${nsh}")
    done
    if [ -f "${buildenv}/recovery/recovery-bootblock.bin" ]; then
        cp "${buildenv}/recovery/recovery-bootblock.bin" "${out}/"
        images+=(recovery-bootblock.bin)
    fi
fi
cp "${obj}/.config" "${out}/config"
{
    echo "coreboot $(git -C "${cbdir}" describe --tags --dirty)"
    echo "edk2 ${EDK2_VER}+$(git -C "${edk2dir}" rev-list --count "${EDK2_VER}..x4-coreboot") patches"
    echo "toolchain $(cat "${cbdir}/.crossgcc-version")"
    if grep -q '^CONFIG_EDK2_CUSTOM_BUILD_PARAMS=.*NETWORK_UNDI_DRIVER=TRUE' "${obj}/.config"; then
        echo "ipxe ${IPXE_REPO} ${IPXE_COMMIT} (bin-x86_64-efi/intel.efidrv)"
    fi
} > "${out}/versions.txt"
"${obj}/cbfstool" "${out}/coreboot.rom" print > "${out}/cbfs.txt" 2>/dev/null || true
(cd "${out}" && sha256sum "${images[@]}" > SHA256SUMS)
echo "Saved ${out#"${ourpath}"/}/"

if [ "${keep}" -gt 0 ]; then
    find "${ourpath}/output" -mindepth 1 -maxdepth 1 -type d -name "2*-${board}" |
        sort | head -n -"${keep}" | xargs -r rm -rf
fi
