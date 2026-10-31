#!/bin/bash
# Write the commits on top of the release tag in BuildEnv's tree to patches/<tree>/.
#   export_patches.sh coreboot|edk2
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
tree="${1:?tree}"
case "${tree}" in
coreboot) dir="${cbdir}"; base="${COREBOOT_VER}" ;;
edk2) dir="${edk2dir}"; base="${EDK2_VER}" ;;
*) die "unknown tree '${tree}'" ;;
esac
git -C "${dir}" diff --quiet HEAD || die "BuildEnv ${tree} has uncommitted changes"

rm -f "${ourpath}/patches/${tree}"/*.patch
mkdir -p "${ourpath}/patches/${tree}"
git -C "${dir}" format-patch -q --no-signature --zero-commit -o "${ourpath}/patches/${tree}" "${base}..HEAD"
echo "$(git -C "${dir}" rev-list --count "${base}..HEAD") patch(es) in patches/${tree}"
# The tree already has these commits; don't re-apply them on the next build
echo "${base} $(patch_hash "${tree}") $(git -C "${dir}" rev-parse HEAD)" > "${stamps}/${tree}"
