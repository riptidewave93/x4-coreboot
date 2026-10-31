#!/bin/bash
# Build iPXE's Intel NIC driver as a UEFI SNP driver into ./BuildEnv/ipxe/intel.efidrv.
# EFI_DOWNGRADE_UX drops iPXE's LoadFile, so edk2's PXE/HTTP boot owns the boot options.
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
src="${buildenv}/ipxe-src"
out="${buildenv}/ipxe"

if [ "$(cat "${src}/.commit" 2>/dev/null)" != "${IPXE_COMMIT}" ]; then
    rm -rf "${src}"
    mkdir -p "${src}"
    tar -C "${src}" --strip-components=1 -xzf "${dl}/ipxe-${IPXE_COMMIT}.tar.gz"
    echo "${IPXE_COMMIT}" > "${src}/.commit"
fi
mkdir -p "${src}/src/config/local"
echo '#define EFI_DOWNGRADE_UX' > "${src}/src/config/local/general.h"
echo "Building iPXE intel.efidrv"
# DEBUG means debug objects to iPXE: keep ours (also passed in MAKEFLAGS) out
env -u DEBUG -u MAKEFLAGS -u MFLAGS -u MAKELEVEL make -s -C "${src}/src" -j"$(nproc)" bin-x86_64-efi/intel.efidrv >/dev/null
mkdir -p "${out}"
cp "${src}/src/bin-x86_64-efi/intel.efidrv" "${out}/intel.efidrv"
