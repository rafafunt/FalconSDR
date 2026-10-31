#!/bin/sh
# Builds the .rpm inside Fedora (glibc-current). A PyInstaller bundle only
# runs where glibc is the same or newer than on the build machine - same
# caveat as packaging/linux-deb/build-in-docker.sh (Debian 11 for the .deb).
# For the widest Fedora/openSUSE compatibility, build on the oldest still
# supported Fedora; ./build.sh alone is fine for your own machine.
set -eu
cd "$(dirname "$0")/../.."
# hackrf / airspyone_host = libhackrf / libairspy (bundled, AGENTS.md gotcha 55); -e: a missing one fails the build.
docker run --rm -e FALCONSDR_REQUIRE_NATIVE_LIBS=1 -v "$PWD":/src -w /src fedora:41 sh -ec "
    dnf install -y -q python3 rpm-build binutils systemd-rpm-macros hackrf airspyone_host >/dev/null
    packaging/rpm/build.sh
    chown -R $(id -u):$(id -g) packaging/rpm"
