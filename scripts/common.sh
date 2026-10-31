# shellcheck shell=bash disable=SC2034
# Sourced by the build scripts: paths, and the version pins exported by the Makefile.
set -euo pipefail
ourpath="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
dl="${ourpath}/preserve/dl"
buildenv="${ourpath}/BuildEnv"
cbdir="${buildenv}/coreboot"
edk2_root="$(echo "${EDK2_REPO}" | cut -d/ -f4)"
edk2dir="${cbdir}/payloads/external/edk2/workspace/${edk2_root}"
stamps="${buildenv}/stamps"

# Deterministic commits in the local trees, so patched trees and build dates repeat
export GIT_AUTHOR_NAME=x4-coreboot GIT_AUTHOR_EMAIL=build@x4-coreboot
export GIT_COMMITTER_NAME=x4-coreboot GIT_COMMITTER_EMAIL=build@x4-coreboot

die() { echo "error: $*" >&2; exit 1; }

# fetch <url> <file> <sha256>: download into ${dl} once and verify
fetch() {
    local url="$1" out="${dl}/$2" sum="$3"
    mkdir -p "${dl}"
    if [ ! -f "${out}" ]; then
        echo "Downloading $2"
        curl -fsSL --retry 3 -o "${out}.part" "${url}"
        mv "${out}.part" "${out}"
    fi
    echo "${sum}  ${out}" | sha256sum -c --quiet - || die "$2: bad sha256 (delete it to re-download)"
}

# Hash of a patch set, to notice when it changes
patch_hash() {
    find "${ourpath}/patches/$1" -name '*.patch' | sort | xargs -r cat | sha256sum | cut -d' ' -f1
}
