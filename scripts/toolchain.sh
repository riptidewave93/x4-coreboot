#!/bin/bash
# Build coreboot's i386 and x64 cross toolchains (with iasl, nasm) once per toolchain
# version into ./preserve/toolchain/<version>. Prints nothing else when cached.
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CROSSGCC_VERSION="$(cat "${cbdir}/.crossgcc-version")"
export CROSSGCC_VERSION
dest="${ourpath}/preserve/toolchain/${CROSSGCC_VERSION}"
[ -f "${dest}/.complete" ] && exit 0

echo "Building the coreboot toolchain ${CROSSGCC_VERSION}, this takes a while"
mkdir -p "${dl}/crossgcc"
rm -rf "${cbdir}/util/crossgcc/tarballs" "${dest}"
ln -s "${dl}/crossgcc" "${cbdir}/util/crossgcc/tarballs"
make -C "${cbdir}" crossgcc-i386 crossgcc-x64 CPUS="$(nproc)" DEST="${dest}" \
    BUILDGCC_OPTIONS=--nocolor
touch "${dest}/.complete"
