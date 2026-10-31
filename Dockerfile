# Builder image. Always x86_64: EDK2 builds with the host gcc.
# The coreboot cross toolchain is built by the build and cached in ./preserve/toolchain.
FROM debian:trixie-slim

RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential bison flex m4 gawk bc file pkg-config \
        zlib1g-dev libncurses-dev libssl-dev uuid-dev \
        python3 python3-venv python-is-python3 nasm imagemagick \
        git ca-certificates curl wget rsync patch diffutils findutils \
        bzip2 gzip xz-utils zstd unzip zip tar sed cpio \
        shellcheck locales; \
    sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen; locale-gen; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8
