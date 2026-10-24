#!/bin/sh
# Builds the AppImage inside Debian 11 (glibc 2.31). A PyInstaller bundle
# only runs where glibc is the same or newer than on the build machine:
# built straight on a current desktop it half-works on older distros
# ("GLIBC_2.xx not found" from some bundled modules) - same caveat as
# packaging/linux-deb/build-in-docker.sh. This one runs on Debian 11+,
# Ubuntu 20.04+, Fedora 41, Arch and newer.
set -eu
cd "$(dirname "$0")/../.."
docker run --rm -v "$PWD":/src -w /src python:3.12-slim-bullseye sh -c "
    apt-get update -qq && apt-get install -y -qq binutils file curl ca-certificates >/dev/null
    curl -fsSL -o /usr/local/bin/appimagetool \
      https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x /usr/local/bin/appimagetool
    export APPIMAGE_EXTRACT_AND_RUN=1
    packaging/appimage/build.sh
    chown -R $(id -u):$(id -g) packaging/appimage"
