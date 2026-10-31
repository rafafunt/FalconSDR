#!/bin/sh
# Builds the .deb inside Debian 11 (glibc 2.31). A PyInstaller bundle only
# runs where glibc is the same or newer than on the build machine: built on
# a current desktop it half-works on older Ubuntu/Debian ("GLIBC_2.xx not
# found" from some bundled modules). This one runs on Ubuntu 20.04+.
set -eu
cd "$(dirname "$0")/../.."
docker run --rm -v "$PWD":/src -w /src python:3.12-slim-bullseye sh -c "
    apt-get update -qq && apt-get install -y -qq binutils libhackrf0 libairspy0 >/dev/null
    packaging/linux-deb/build.sh
    chown -R $(id -u):$(id -g) packaging/linux-deb"
