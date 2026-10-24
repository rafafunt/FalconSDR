#!/bin/sh
# Builds the .rpm inside Fedora (glibc-current). A PyInstaller bundle only
# runs where glibc is the same or newer than on the build machine - same
# caveat as packaging/linux-deb/build-in-docker.sh (Debian 11 for the .deb).
# For the widest Fedora/openSUSE compatibility, build on the oldest still
# supported Fedora; ./build.sh alone is fine for your own machine.
set -eu
cd "$(dirname "$0")/../.."
docker run --rm -v "$PWD":/src -w /src fedora:41 sh -c "
    dnf install -y -q python3 rpm-build binutils systemd-rpm-macros >/dev/null
    packaging/rpm/build.sh
    chown -R $(id -u):$(id -g) packaging/rpm"
