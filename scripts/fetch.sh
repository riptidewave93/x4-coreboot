#!/bin/bash
# Download sources into ./preserve/dl: fetch.sh [coreboot] [edk2] [ipxe] (default: all)
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

edk2_tarball() {
    local name="edk2-${edk2_root}-${EDK2_VER}.tar.gz" work
    [ -f "${dl}/${name}" ] && return
    echo "Cloning EDK2 ${EDK2_VER} with submodules"
    work="$(mktemp -d)"
    git clone -q --depth 1 --branch "${EDK2_VER}" --recurse-submodules --shallow-submodules \
        "${EDK2_REPO}" "${work}/edk2"
    # coreboot's --checkout also pulls 'update = none' submodules (TPM's wolfssl); cache them too
    git -C "${work}/edk2" submodule update -q --init --checkout --recursive --depth 1
    [ "$(git -C "${work}/edk2" rev-parse HEAD)" = "${EDK2_COMMIT}" ] ||
        die "EDK2 tag ${EDK2_VER} is not ${EDK2_COMMIT}"
    tar -C "${work}" -czf "${dl}/${name}.part" edk2
    mv "${dl}/${name}.part" "${dl}/${name}"
    rm -rf "${work}"
}

ipxe_tarball() {
    local name="ipxe-${IPXE_COMMIT}.tar.gz" work
    [ -f "${dl}/${name}" ] && return
    echo "Fetching iPXE ${IPXE_COMMIT}"
    work="$(mktemp -d)"
    git init -q "${work}/ipxe"
    git -C "${work}/ipxe" fetch -q --depth 1 "${IPXE_REPO}" "${IPXE_COMMIT}"
    git -C "${work}/ipxe" checkout -q FETCH_HEAD
    [ "$(git -C "${work}/ipxe" rev-parse HEAD)" = "${IPXE_COMMIT}" ] ||
        die "iPXE fetch did not give ${IPXE_COMMIT}"
    tar -C "${work}" --exclude=.git -czf "${dl}/${name}.part" ipxe
    mv "${dl}/${name}.part" "${dl}/${name}"
    rm -rf "${work}"
}

[ $# -gt 0 ] || set -- coreboot edk2 ipxe
for w in "$@"; do
    case "${w}" in
    coreboot)
        fetch "https://coreboot.org/releases/coreboot-${COREBOOT_VER}.tar.xz" \
            "coreboot-${COREBOOT_VER}.tar.xz" "${COREBOOT_SHA256}"
        fetch "https://coreboot.org/releases/coreboot-blobs-${COREBOOT_VER}.tar.xz" \
            "coreboot-blobs-${COREBOOT_VER}.tar.xz" "${COREBOOT_BLOBS_SHA256}" ;;
    edk2) mkdir -p "${dl}"; edk2_tarball ;;
    ipxe) mkdir -p "${dl}"; ipxe_tarball ;;
    *) die "unknown source '${w}'" ;;
    esac
done
