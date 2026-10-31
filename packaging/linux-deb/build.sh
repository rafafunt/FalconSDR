#!/usr/bin/env bash
# Builds a self-contained ("fat") .deb for FalconSDR: a PyInstaller
# --onedir bundle (its own Python + numpy/scipy/pyrtlsdrlib + the web UI's
# static files, all frozen in) wrapped in a Debian package, so an end user
# needs nothing beyond `sudo apt install ./falconsdr_*.deb` - no pip, no
# venv. The UI itself runs in the browser the app opens.
#
# Deliberately built in a *throwaway* venv (packaging/linux-deb/build-venv,
# gitignored, never the project's own .venv) so nothing from an existing dev
# environment leaks into the bundle by accident - see this directory's own
# README.md for why that distinction matters and how to actually verify it.
#
# What this build does NOT include, on purpose (see README.md's "Known
# limitations" section for the full explanation):
#   - SoapySDR support (HackRF/LimeSDR/Airspy/SDRplay/USRP/...) - RTL-SDR via
#     the bundled pyrtlsdrlib native backend is the only supported hardware
#     in this build. SoapySDR is a system C library with its own plugin
#     architecture, not something that freezes cleanly into a single bundle.
#   - The in-UI "Install transcription" button - it needs a real, writable
#     Python/pip environment to install into, which a frozen build isn't.
#   - MP3 recording output - still works, but only if the user separately
#     has `ffmpeg` on PATH (a system binary this build can't bundle either).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD="$SCRIPT_DIR/build"
VENV="$SCRIPT_DIR/build-venv"
ARCH="$(dpkg --print-architecture)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_ROOT/falconsdr/__init__.py")"
PKG=falconsdr

echo "== FalconSDR .deb build =="
echo "repo root: $REPO_ROOT"
echo "version:   $VERSION"
echo "arch:      $ARCH"

echo "-- 1/5: fresh build venv (not the project's dev .venv) --"
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt" pyinstaller

echo "-- 2/5: PyInstaller (--onedir) --"
rm -rf "$BUILD"
mkdir -p "$BUILD"
# libhackrf + libairspy (BSD-3-Clause): HackRF/Airspy without SoapySDR - build-in-docker.sh installs the packages.
source "$SCRIPT_DIR/../linux-native-libs.sh"
collect_native_libs strict
(
  cd "$REPO_ROOT"
  # Absolute source paths for --add-data - PyInstaller resolves a relative
  # source against the .spec file's own directory (--specpath), not the
  # invocation cwd, which silently pointed at the wrong place here once
  # --specpath stopped being the repo root.
  #
  # --collect-data tzdata: tzdata (requirements.txt: "Windows has no system
  # time zone database - zoneinfo needs this package there") ships only data
  # files, no Python code a normal import touches - PyInstaller's static
  # import analysis doesn't see zoneinfo's own runtime fallback to it and
  # drops it by default. Confirmed by a real clean-container test: without
  # this flag, the frozen build raised ZoneInfoNotFoundError as soon as
  # anything touched a timezone - exactly the failure tzdata was added to
  # fix in the first place, silently reopened by freezing it.
  #
  # --collect-data pyrtlsdrlib: same class of bug, found on a real clean VM
  # test. pyrtlsdrlib.lib.load_librtlsdr() locates its bundled librtlsdr.so*
  # via importlib.resources.files() at runtime - invisible to PyInstaller's
  # static import analysis, so the .so was silently dropped. Without this
  # flag the build "worked" on the dev machine only because a *system*
  # rtl-sdr package happened to be installed there too (pyrtlsdr's own
  # fallback chain found that instead) - a genuinely clean machine has
  # neither, so RtlSdrBackend's native import fails, HAVE_RTLSDR_NATIVE
  # ends up False, and the app falls through to the SoapySDR path this
  # build deliberately doesn't ship, surfacing as "the python3-soapysdr
  # module is not installed" instead of ever finding the real dongle.
  #
  # --add-binary for libportaudio.so.2: live audio's equivalent of the same
  # bug again, but PyInstaller's automatic binary-dependency scan can't be
  # trusted here at all (unlike pyrtlsdrlib, fixable with --collect-data) -
  # sounddevice's Linux loader only ever calls ctypes.util.find_library(),
  # a system-wide ldconfig-cache search with no concept of this bundle's own
  # _internal/ directory, even on LD_LIBRARY_PATH (confirmed in a clean
  # container: the .so was present and still not found). Explicitly bundled
  # here, found at *this* build, and audio_output.py's own sys.frozen-gated
  # find_library() monkeypatch hands sounddevice its real path directly
  # instead of ever letting it search. Requires libportaudio2 installed on
  # the build machine (not in requirements.txt - it's a system package, same
  # category as the Linux dev README's SoapySDR/PyAudio system packages).
  # grep -m1 exits as soon as it finds a match, closing its end of the pipe
  # while ldconfig -p is still writing - under `set -o pipefail` (this
  # script's own header) that SIGPIPE makes the whole pipeline (and the
  # script) exit nonzero, so pipefail is turned off just for this one line.
  "$VENV/bin/pyinstaller" \
    --name "$PKG" \
    --onedir \
    --noconfirm \
    --add-data "$REPO_ROOT/falconsdr/locale/pl.po:falconsdr/locale" \
    --add-data "$REPO_ROOT/falconsdr/presets/band_presets.json:falconsdr/presets" \
    --add-data "$REPO_ROOT/falconsdr/web/static:falconsdr/web/static" \
    --collect-data tzdata \
    --collect-data pyrtlsdrlib     --collect-data imageio_ffmpeg     --collect-all pip \
    "${NATIVE_ARGS[@]}" \
    --distpath "$BUILD/dist" \
    --workpath "$BUILD/work" \
    --specpath "$BUILD" \
    main.py
)

# Fails the build if PyInstaller silently dropped something (tzdata,
# librtlsdr...) - falconsdr/selftest.py; packaging/windows/build.ps1 does the same.
FALCONSDR_REQUIRE_NATIVE_LIBS=1 "$BUILD/dist/$PKG/$PKG" --self-test

echo "-- 3/5: assembling the .deb tree --"
DEBROOT="$BUILD/deb"
rm -rf "$DEBROOT"
mkdir -p "$DEBROOT/DEBIAN"
mkdir -p "$DEBROOT/opt/$PKG"
mkdir -p "$DEBROOT/usr/bin"
mkdir -p "$DEBROOT/usr/share/applications"
mkdir -p "$DEBROOT/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$DEBROOT/usr/share/doc/$PKG"
mkdir -p "$DEBROOT/etc/udev/rules.d"
mkdir -p "$DEBROOT/etc/modprobe.d"

cp -r "$BUILD/dist/$PKG/." "$DEBROOT/opt/$PKG/"

cat > "$DEBROOT/usr/bin/$PKG" <<WRAPPER
#!/bin/sh
exec /opt/$PKG/$PKG "\$@"
WRAPPER
chmod 755 "$DEBROOT/usr/bin/$PKG"

cp "$SCRIPT_DIR/falconsdr.desktop" "$DEBROOT/usr/share/applications/"
cp "$REPO_ROOT/falconsdr/web/static/icon.svg" \
   "$DEBROOT/usr/share/icons/hicolor/scalable/apps/falconsdr.svg"
cp "$SCRIPT_DIR/README.md" "$DEBROOT/usr/share/doc/$PKG/packaging-README.md"
# GPL: the license text must ship with the binary package.
cp "$SCRIPT_DIR/../../LICENSE" "$DEBROOT/usr/share/doc/$PKG/copyright"
# ...and those of everything bundled (packaging/collect_licenses.py).
"$VENV/bin/python" "$SCRIPT_DIR/../collect_licenses.py" "$DEBROOT/usr/share/doc/$PKG/THIRD-PARTY-LICENSES.txt"
append_native_licenses "$DEBROOT/usr/share/doc/$PKG/THIRD-PARTY-LICENSES.txt"

# Real user-reported gap: a fresh Ubuntu install has no udev rule granting a
# plain user access to an RTL-SDR dongle's USB device node (the `rtl-sdr`
# apt package normally ships one, but this self-contained build
# deliberately doesn't depend on any system package - see README.md) - the
# open() failure that causes looks identical to "no hardware detected" from
# the user's side, since this app's "auto" driver mode silently falls back
# to Simulated on any open failure (gotcha 8). Same for the kernel's own
# DVB-T driver claiming the dongle before this app ever gets a chance to.
cp "$SCRIPT_DIR/60-rtl-sdr.rules" "$DEBROOT/etc/udev/rules.d/"
cp "$SCRIPT_DIR/blacklist-rtl-dvb.conf" "$DEBROOT/etc/modprobe.d/"
cp "$SCRIPT_DIR/postinst" "$DEBROOT/DEBIAN/postinst"
chmod 755 "$DEBROOT/DEBIAN/postinst"

INSTALLED_SIZE_KB="$(du -sk "$DEBROOT/opt/$PKG" | cut -f1)"

cat > "$DEBROOT/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: hamradio
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE_KB
Recommends: ffmpeg
Conflicts: smartscanatc
Replaces: smartscanatc
Maintainer: rafafunt <rafafunt@gmail.com>
Description: Wideband SDR ATC/radio scanner (self-contained build)
 Scans a radio band via RTL-SDR and records audio only for transmissions
 the scanner actually stops on.
 .
 The interface runs in the web browser (http://127.0.0.1:8765), which the
 app opens on start; other devices on the network can use it too when run
 with --server.
 .
 This is a self-contained ("fat") build: its own Python runtime and all
 pip-installable dependencies (numpy, scipy, pyrtlsdrlib) are bundled in
 /opt/$PKG - nothing from the system Python is required. RTL-SDR hardware
 works out of the box.
 .
 Not included in this build: SoapySDR-based hardware (HackRF, LimeSDR,
 Airspy, SDRplay, USRP - needs a normal pip/source install instead), the
 in-app transcription installer, and MP3 output (needs a separately
 installed system ffmpeg). See /usr/share/doc/$PKG/packaging-README.md.
CONTROL

echo "-- 4/5: building the package --"
mkdir -p "$SCRIPT_DIR/out"
OUT="$SCRIPT_DIR/out/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$DEBROOT" "$OUT"

echo "-- 5/5: done --"
echo "Package: $OUT"
echo "Install with:   sudo apt install $OUT"
echo "Remove with:    sudo apt remove $PKG"
ls -lh "$OUT"
