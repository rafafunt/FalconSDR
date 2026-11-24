#!/bin/sh
# Builds the .deb inside Debian 11 (glibc 2.31). A PyInstaller bundle only
# runs where glibc is the same or newer than on the build machine: built on
# a current desktop it half-works on older Ubuntu/Debian ("GLIBC_2.xx not
# found" from some bundled modules). This one runs on Ubuntu 20.04+.
set -eu
cd "$(dirname "$0")/../.."
docker run --rm -e FALCONSDR_REQUIRE_NATIVE_LIBS=1 -v "$PWD":/src -w /src python:3.12-slim-bullseye sh -ec "
    # Debian 11 left the mirrors for archive.debian.org (deb.debian.org answered 404 for its security pool, 2026-11).
    printf 'deb http://archive.debian.org/debian bullseye main\\ndeb http://archive.debian.org/debian-security bullseye-security main\\n' > /etc/apt/sources.list
    apt-get -o Acquire::Check-Valid-Until=false update -qq && apt-get install -y -qq binutils libhackrf0 libairspy0 gcc curl ca-certificates >/dev/null
    curl --proto =https --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null
    . \$HOME/.cargo/env
    packaging/linux-deb/build.sh
    chown -R $(id -u):$(id -g) tetra-rs/target packaging/linux-deb"
