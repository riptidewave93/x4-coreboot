#!/bin/bash
# Unpack coreboot (and EDK2) into ./BuildEnv as local git trees, apply patches/<tree>
# as commits on top of the release tag.
#   setup_builddir.sh [coreboot] [edk2] (default: coreboot)
# shellcheck source=scripts/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

unpack_coreboot() {
    mkdir -p "${cbdir}"
    tar -xJf "${dl}/coreboot-${COREBOOT_VER}.tar.xz" -C "${cbdir}" --strip-components=1
    tar -xJf "${dl}/coreboot-blobs-${COREBOOT_VER}.tar.xz" -C "${cbdir}" --strip-components=1
    git -C "${cbdir}" init -q
    # The release tarball has no .gitignore; keep 3rdparty and build output untracked
    printf '%s\n' /3rdparty/ /build/ '/.config*' /.xcompile /.ccwrap \
        /payloads/external/edk2/workspace/ /util/crossgcc/tarballs /util/crossgcc/xgcc \
        >> "${cbdir}/.git/info/exclude"
    git -C "${cbdir}" add -A
    date="@$(stat -c %Y "${cbdir}/.coreboot-version")"
    GIT_AUTHOR_DATE="${date}" GIT_COMMITTER_DATE="${date}" \
        git -C "${cbdir}" commit -q -m "coreboot ${COREBOOT_VER}"
    GIT_COMMITTER_DATE="${date}" git -C "${cbdir}" tag -a -m "coreboot ${COREBOOT_VER}" "${COREBOOT_VER}"
}

unpack_edk2() {
    mkdir -p "$(dirname "${edk2dir}")"
    tar -xzf "${dl}/edk2-${edk2_root}-${EDK2_VER}.tar.gz" -C "$(dirname "${edk2dir}")"
    mv "$(dirname "${edk2dir}")/edk2" "${edk2dir}"
}

# setup <tree> <dir> <release tag>
setup() {
    local tree="$1" dir="$2" base="$3" stamp="${stamps}/$1" want have
    want="${base} $(patch_hash "${tree}")"
    if [ ! -d "${dir}/.git" ]; then
        echo "Unpacking ${tree} ${base}"
        "unpack_${tree}"
    else
        [ -f "${stamp}" ] || die "BuildEnv ${tree} is incomplete, run 'make clean'"
        read -r have_base have_hash have_head < "${stamp}"
        [ "${have_base}" = "${base}" ] || die "BuildEnv has ${tree} ${have_base}, want ${base}. Run 'make clean'."
        have="${have_base} ${have_hash}"
        if [ "${have}" != "${want}" ]; then
            if [ "$(git -C "${dir}" rev-parse HEAD)" != "${have_head}" ] ||
                ! git -C "${dir}" diff --quiet HEAD; then
                die "BuildEnv ${tree} has local changes and patches/${tree} changed: 'make export-patches TREE=${tree}' or 'make clean'"
            fi
            echo "patches/${tree} changed, re-applying"
            git -C "${dir}" reset -q --hard "${base}"
        fi
    fi
    if [ "${have:-}" != "${want}" ]; then
        local patches=("${ourpath}"/patches/"${tree}"/*.patch)
        if [ -f "${patches[0]}" ]; then
            echo "Applying ${#patches[@]} patch(es) to ${tree}"
            git -C "${dir}" am -q --keep-non-patch --keep-cr --committer-date-is-author-date "${patches[@]}"
        fi
        mkdir -p "${stamps}"
        echo "${want} $(git -C "${dir}" rev-parse HEAD)" > "${stamp}"
    fi
}

[ $# -gt 0 ] || set -- coreboot
for tree in "$@"; do
    case "${tree}" in
    coreboot) setup coreboot "${cbdir}" "${COREBOOT_VER}" ;;
    edk2)
        setup edk2 "${edk2dir}" "${EDK2_VER}"
        # build.sh points coreboot at this tag, so it builds our patched EDK2
        git -C "${edk2dir}" tag -f x4-coreboot >/dev/null ;;
    *) die "unknown tree '${tree}'" ;;
    esac
done
