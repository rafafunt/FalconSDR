#!/usr/bin/env bash
# Builds a self-contained ("fat") .rpm for FalconSDR: a PyInstaller
# --onedir bundle (its own Python + numpy/scipy/pyrtlsdrlib + the web UI's
# static files, all frozen in) wrapped in an RPM, so an end user needs
# nothing beyond `sudo dnf install ./falconsdr-*.rpm` - no pip, no venv.
# The RPM counterpart of packaging/linux-deb/build.sh - same PyInstaller
# flags and the same reasons for them (AGENTS.md gotcha 35).
#
# Deliberately built in a *throwaway* venv (build-venv, gitignored, never
# the project's own .venv) so nothing from an existing dev environment
# leaks into the bundle by accident.
#
# What this build does NOT include, on purpose (same tradeoff as the .deb):
#   - SoapySDR support (HackRF/LimeSDR/Airspy/SDRplay/USRP/...) - RTL-SDR via
#     the bundled pyrtlsdrlib native backend is the only supported hardware.
#   - MP3 recording output works via the bundled imageio-ffmpeg binary, or
#     a system `ffmpeg` on PATH when present (weak dependency).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD="$SCRIPT_DIR/build"
VENV="$SCRIPT_DIR/build-venv"
ARCH="$(uname -m)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_ROOT/falconsdr/__init__.py")"
PKG=falconsdr

echo "== FalconSDR .rpm build =="
echo "repo root: $REPO_ROOT"
echo "version:   $VERSION"
echo "arch:      $ARCH"

echo "-- 1/4: fresh build venv (not the project's dev .venv) --"
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade --quiet pip
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt" pyinstaller

echo "-- 2/4: PyInstaller (--onedir) --"
rm -rf "$BUILD"
mkdir -p "$BUILD"
(
  cd "$REPO_ROOT"
  # Absolute source paths for --add-data (relative sources once resolved
  # against the wrong --specpath dir here - same trap as the .deb documents).
  # --collect-data tzdata / pyrtlsdrlib / imageio_ffmpeg: runtime-loaded data
  # PyInstaller's static analysis cannot see (AGENTS.md gotcha 35); without
  # them the bundle fails on a clean machine while working on the dev one.
  "$VENV/bin/pyinstaller" \
    --name "$PKG" \
    --onedir \
    --noconfirm \
    --add-data "$REPO_ROOT/falconsdr/locale/pl.po:falconsdr/locale" \
    --add-data "$REPO_ROOT/falconsdr/presets/band_presets.json:falconsdr/presets" \
    --add-data "$REPO_ROOT/falconsdr/web/static:falconsdr/web/static" \
    --collect-data tzdata \
    --collect-data pyrtlsdrlib \
    --collect-data imageio_ffmpeg \
    --collect-all pip \
    --distpath "$BUILD/dist" \
    --workpath "$BUILD/work" \
    --specpath "$BUILD" \
    main.py
)

# Fails the build if PyInstaller silently dropped something (tzdata,
# librtlsdr...) - falconsdr/selftest.py; the .deb and Windows builds do the same.
"$BUILD/dist/$PKG/$PKG" --self-test

echo "-- 3/4: assembling the RPM tree --"
ROOT="$BUILD/rpmroot"
rm -rf "$ROOT"
mkdir -p "$ROOT/opt/$PKG"
mkdir -p "$ROOT/usr/bin"
mkdir -p "$ROOT/usr/share/applications"
mkdir -p "$ROOT/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$ROOT/usr/share/doc/$PKG"
mkdir -p "$ROOT/usr/lib/udev/rules.d"
mkdir -p "$ROOT/etc/modprobe.d"

cp -r "$BUILD/dist/$PKG/." "$ROOT/opt/$PKG/"

cat > "$ROOT/usr/bin/$PKG" <<WRAPPER
#!/bin/sh
exec /opt/$PKG/$PKG "\$@"
WRAPPER
chmod 755 "$ROOT/usr/bin/$PKG"

cp "$REPO_ROOT/packaging/linux-deb/falconsdr.desktop" "$ROOT/usr/share/applications/"
cp "$REPO_ROOT/falconsdr/web/static/icon.svg" \
   "$ROOT/usr/share/icons/hicolor/scalable/apps/falconsdr.svg"
cp "$SCRIPT_DIR/README.md" "$ROOT/usr/share/doc/$PKG/packaging-README.md"
cp "$REPO_ROOT/LICENSE" "$ROOT/usr/share/doc/$PKG/copyright"
"$VENV/bin/python" "$REPO_ROOT/packaging/collect_licenses.py" "$ROOT/usr/share/doc/$PKG/THIRD-PARTY-LICENSES.txt"

cp "$REPO_ROOT/packaging/linux-deb/60-rtl-sdr.rules" "$ROOT/usr/lib/udev/rules.d/"
cp "$REPO_ROOT/packaging/linux-deb/blacklist-rtl-dvb.conf" "$ROOT/etc/modprobe.d/"

echo "-- 4/4: building the package (rpmbuild) --"
TOP="$BUILD/rpm-top"
rm -rf "$TOP"
mkdir -p "$TOP/SPECS" "$TOP/BUILD" "$TOP/RPMS" "$TOP/SOURCES" "$TOP/SRPMS" "$SCRIPT_DIR/out"
cat > "$TOP/SPECS/$PKG.spec" <<SPEC
Name:           $PKG
Version:        $VERSION
Release:        1
Summary:        Wideband SDR ATC/radio scanner (self-contained build)
License:        GPL-3.0-or-later
URL:            https://github.com/rafafunt/FalconSDR
BuildArch:      $ARCH
# /opt/falconsdr ships its own bundled .so files (Python, numpy, ...):
# rpm's automatic dependency scan would pick those up as system
# requirements (e.g. libuuid-...so.1.3.0(UUID_1.0)) that no distro
# provides, making the package uninstallable. The bundle needs nothing
# from the system beyond glibc + udev, so both directions are off.
AutoReqProv:    no
# File-based requirement: Fedora ships udevadm in systemd-udev, openSUSE
# in udev - a package-name Require would only fit one of them.
Requires:       /usr/bin/udevadm
Recommends:     ffmpeg

%description
Scans a radio band via RTL-SDR and records audio only for transmissions
the scanner actually stops on. Self-contained bundle in /opt/$PKG.

%files
/opt/$PKG/
/usr/bin/$PKG
/usr/share/applications/falconsdr.desktop
/usr/share/icons/hicolor/scalable/apps/falconsdr.svg
/usr/share/doc/$PKG/
/usr/lib/udev/rules.d/60-rtl-sdr.rules
/etc/modprobe.d/blacklist-rtl-dvb.conf

%post
if command -v udevadm >/dev/null 2>&1; then
    timeout 5 udevadm control --reload-rules >/dev/null 2>&1 || true
    timeout 5 udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || true
fi
if command -v rmmod >/dev/null 2>&1; then
    timeout 5 rmmod dvb_usb_rtl28xxu >/dev/null 2>&1 || true
fi

%changelog
* Fri Oct 23 2026 FalconSDR - $VERSION-1
- Self-contained RPM (mirrors the .deb).
SPEC
rpmbuild -bb --buildroot "$ROOT" --define "_topdir $TOP" "$TOP/SPECS/$PKG.spec"
RPM_BUILT="$(find "$TOP/RPMS" -name '*.rpm' | head -n 1)"
OUT="$SCRIPT_DIR/out/$(basename "$RPM_BUILT")"
cp "$RPM_BUILT" "$OUT"

echo "-- done --"
echo "Package: $OUT"
echo "Install with:   sudo dnf install $OUT   (or: sudo rpm -i $OUT)"
echo "Remove with:    sudo dnf remove $PKG"
ls -lh "$OUT"
