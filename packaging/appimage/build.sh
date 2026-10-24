#!/usr/bin/env bash
# Builds a self-contained FalconSDR AppImage: a PyInstaller --onedir bundle
# (its own Python + numpy/scipy/pyrtlsdrlib + the web UI's static files)
# packed with appimagetool into one executable file that runs on most
# modern distributions with no install step:
#
#   chmod +x FalconSDR-<version>-x86_64.AppImage
#   ./FalconSDR-<version>-x86_64.AppImage
#
# The AppImage counterpart of packaging/linux-deb/build.sh - same PyInstaller
# flags and the same reasons for them (AGENTS.md gotcha 35).
#
# Deliberately built in a *throwaway* venv (build-venv, gitignored, never
# the project's own .venv) so nothing from an existing dev environment
# leaks into the bundle by accident.
#
# Needs: python3, pip, `appimagetool` on PATH, plus `file` and
# `binutils` (appimagetool's own dependencies for inspecting binaries).
#
# What this build does NOT include, on purpose (same tradeoff as the .deb):
#   - SoapySDR support - RTL-SDR via the bundled pyrtlsdrlib native backend
#     is the only supported hardware in this build.
#   - No udev rule / DVB-T blacklist can ship inside an AppImage (it runs
#     unprivileged, installs nothing): on a machine with no RTL-SDR software
#     installed, copy packaging/linux-deb/60-rtl-sdr.rules to
#     /etc/udev/rules.d/ once and replug the dongle (same file the .deb/.rpm
#     install for you).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD="$SCRIPT_DIR/build"
VENV="$SCRIPT_DIR/build-venv"
ARCH="$(uname -m)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_ROOT/falconsdr/__init__.py")"
PKG=falconsdr

echo "== FalconSDR AppImage build =="
echo "repo root: $REPO_ROOT"
echo "version:   $VERSION"
echo "arch:      $ARCH"

command -v appimagetool >/dev/null 2>&1 || {
  echo "appimagetool not found on PATH (https://github.com/AppImage/appimagetool/releases)" >&2
  exit 1
}
command -v file >/dev/null 2>&1 || {
  echo "need 'file' (appimagetool dependency: apt install file / dnf install file)" >&2
  exit 1
}
command -v objcopy >/dev/null 2>&1 || {
  echo "need 'binutils' (appimagetool dependency: apt install binutils / dnf install binutils)" >&2
  exit 1
}

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

"$BUILD/dist/$PKG/$PKG" --self-test

echo "-- 3/4: assembling the AppDir --"
APPDIR="$BUILD/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -r "$BUILD/dist/$PKG" "$APPDIR/usr/bin/$PKG"
cp "$REPO_ROOT/packaging/linux-deb/falconsdr.desktop" "$APPDIR/usr/share/applications/"
cp "$APPDIR/usr/share/applications/falconsdr.desktop" "$APPDIR/falconsdr.desktop"
# desktop entry must point at the AppRun-launched binary for appimagetool lint
sed -i 's|^Exec=.*|Exec=falconsdr|' "$APPDIR/falconsdr.desktop" "$APPDIR/usr/share/applications/falconsdr.desktop"
cp "$REPO_ROOT/falconsdr/web/static/icon.svg" "$APPDIR/falconsdr.svg"
cp "$REPO_ROOT/falconsdr/web/static/icon.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/falconsdr.svg"
cp "$REPO_ROOT/packaging/icons/falconsdr.png" "$APPDIR/falconsdr.png" 2>/dev/null || true
cp "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
chmod 755 "$APPDIR/AppRun"
cp "$REPO_ROOT/LICENSE" "$APPDIR/"
"$VENV/bin/python" "$REPO_ROOT/packaging/collect_licenses.py" "$APPDIR/THIRD-PARTY-LICENSES.txt"

echo "-- 4/4: appimagetool --"
mkdir -p "$SCRIPT_DIR/out"
OUT="$SCRIPT_DIR/out/FalconSDR-${VERSION}-${ARCH}.AppImage"
# UPDATE_INFORMATION left empty on purpose: updates ship via GitHub Releases.
ARCH="$ARCH" appimagetool "$APPDIR" "$OUT"

echo "-- done --"
echo "AppImage: $OUT"
echo "Run with:   chmod +x $OUT && $OUT"
ls -lh "$OUT"
