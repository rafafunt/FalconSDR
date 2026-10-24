#!/bin/sh
# FalconSDR one-line installer:
#
#   curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash
#
# Picks the right artifact for the machine it runs on:
#   - Debian/Ubuntu  -> official .deb from GitHub Releases
#   - Fedora/openSUSE -> official .rpm from GitHub Releases
#   - Arch/CachyOS   -> AUR package `falconsdr` (yay/paru/pacman+makepkg fallback)
#   - anything else (glibc-based, x86_64/aarch64) -> the AppImage from
#     GitHub Releases (no root needed)
#   - `--from-source` -> pip install from a git checkout instead (SoapySDR
#     hardware: HackRF/LimeSDR/Airspy/... - not in the packaged builds;
#     also the fallback for unsupported CPU architectures, musl-based
#     distros such as Alpine (the AppImage needs glibc and won't run
#     there), and releases that don't publish the matching asset yet)
#
# After installing, unplug and replug the RTL-SDR dongle once (udev rule),
# then run `falconsdr` - the UI opens at http://127.0.0.1:8765.
set -eu

REPO="${FALCONSDR_REPO:-rafafunt/FalconSDR}"
VERSION="${FALCONSDR_VERSION:-latest}"
FROM_SOURCE=0
for a in "$@"; do [ "$a" = "--from-source" ] && FROM_SOURCE=1; done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' (install it first)"; }

from_source_hint() {
  cat >&2 <<EOF
No prebuilt package fits this machine - install from source instead:
  curl -fsSL https://raw.githubusercontent.com/$REPO/master/packaging/install.sh | bash -s -- --from-source
(That path also supports SoapySDR hardware: HackRF/LimeSDR/Airspy/...)
EOF
}

# Only x86_64 and aarch64 have prebuilt assets. Anything else (e.g. a
# 32-bit ARM Raspberry Pi OS) must go --from-source rather than
# downloading a wrong-architecture .deb/.rpm.
check_arch() {
  case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) say "CPU architecture '$(uname -m)' has no prebuilt package."; from_source_hint; exit 1;;
  esac
}

# The AppImage (like the .deb/.rpm bundles) is built against glibc and
# won't run on musl-based distros such as Alpine.
is_musl() {
  [ -f /etc/alpine-release ] && return 0
  ldd --version 2>&1 | grep -qi musl && return 0
  return 1
}

# Curl wrapper that turns a missing release asset (404: the release
# doesn't publish that file yet) into a pointer at --from-source
# instead of a bare "curl: (22) 404".
fetch_asset() {
  url="$1"; out="$2"
  if ! curl -fsSL -o "$out" "$url"; then
    cat >&2 <<EOF
error: download failed:
  $url
That release asset may not exist (yet) - not every release publishes
every artifact (.deb/.rpm/AppImage). Check what's actually attached at
https://github.com/$REPO/releases, or install from source instead:
  curl -fsSL https://raw.githubusercontent.com/$REPO/master/packaging/install.sh | bash -s -- --from-source
EOF
    exit 1
  fi
}

release_tag() {
  # Resolves "latest" to e.g. v0.9.0 via the GitHub API; a pinned
  # FALCONSDR_VERSION=vX.Y.Z skips the network call.
  if [ "$VERSION" != "latest" ]; then printf '%s' "$VERSION"; return; fi
  need curl
  curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" |
    grep -m1 '"tag_name"' | cut -d'"' -f4
}

have_apt() { command -v apt-get >/dev/null 2>&1; }
have_dnf() { command -v dnf >/dev/null 2>&1; }
have_zypper() { command -v zypper >/dev/null 2>&1; }
have_pacman() { command -v pacman >/dev/null 2>&1; }

install_deb() {
  tag="$1"
  check_arch
  case "$(uname -m)" in
    x86_64) deb_arch=amd64;;
    aarch64) deb_arch=arm64;;
  esac
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  say "Downloading FalconSDR $tag (.deb)..."
  fetch_asset \
    "https://github.com/$REPO/releases/download/$tag/falconsdr_${tag#v}_${deb_arch}.deb" \
    "$tmp/falconsdr.deb"
  say "Installing (sudo apt install)..."
  sudo apt-get update -qq
  sudo apt install -y "$tmp/falconsdr.deb"
}

install_rpm() {
  tag="$1"; ver="${tag#v}"
  check_arch
  arch="$(uname -m)"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  say "Downloading FalconSDR $tag (.rpm)..."
  fetch_asset \
    "https://github.com/$REPO/releases/download/$tag/falconsdr-${ver}-1.${arch}.rpm" \
    "$tmp/falconsdr.rpm"
  if have_dnf; then sudo dnf install -y "$tmp/falconsdr.rpm"
  elif have_zypper; then sudo zypper --no-confirm install "$tmp/falconsdr.rpm"
  else sudo rpm -i "$tmp/falconsdr.rpm"; fi
}

install_aur() {
  # The AUR package repackages the official x86_64 .deb (PKGBUILD:
  # arch=('x86_64')) - anything else (e.g. Arch ARM on a Raspberry Pi)
  # has no binary to repackage.
  if [ "$(uname -m)" != "x86_64" ]; then
    say "CPU architecture '$(uname -m)' is not covered by the AUR package (x86_64 only)."
    from_source_hint; exit 1
  fi
  if command -v yay >/dev/null 2>&1; then yay -S --noconfirm falconsdr
  elif command -v paru >/dev/null 2>&1; then paru -S --noconfirm falconsdr
  else
    say "No AUR helper found - building with makepkg..."
    need git; need makepkg; need sudo
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
    git clone --depth 1 "https://aur.archlinux.org/falconsdr.git" "$tmp/falconsdr"
    (cd "$tmp/falconsdr" && makepkg -si --noconfirm)
  fi
}

install_appimage() {
  tag="$1"
  check_arch
  arch="$(uname -m)"
  dest="${FALCONSDR_APPIMAGE:-$HOME/Applications/FalconSDR-${tag}-${arch}.AppImage}"
  mkdir -p "$(dirname "$dest")"
  say "Downloading FalconSDR $tag (AppImage)..."
  fetch_asset \
    "https://github.com/$REPO/releases/download/$tag/FalconSDR-${tag#v}-${arch}.AppImage" \
    "$dest"
  chmod +x "$dest"
  say "AppImage saved to $dest"
  if [ "$(id -u)" = "0" ]; then :
  elif [ ! -f /etc/udev/rules.d/60-rtl-sdr.rules ] && [ ! -f /usr/lib/udev/rules.d/60-rtl-sdr.rules ]; then
    cat <<EOF

RTL-SDR USB access needs one manual step (AppImages install nothing):
  sudo curl -fsSL -o /etc/udev/rules.d/60-rtl-sdr.rules \\
    https://raw.githubusercontent.com/$REPO/master/packaging/linux-deb/60-rtl-sdr.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
then unplug and replug the dongle once.
EOF
  fi
  say "Run it with: $dest"
}

install_from_source() {
  say "Installing FalconSDR from source (SoapySDR-capable path)..."
  need python3; need git
  if [ ! -d FalconSDR ]; then git clone "https://github.com/$REPO.git"; fi
  cd FalconSDR
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cat <<EOF

Done. Start it with:
  .venv/bin/python main.py
(RTL-SDR udev rule: packaging/linux-deb/60-rtl-sdr.rules - see the README.
SoapySDR hardware: install python3-soapysdr + soapysdr-module-<device>.)
EOF
}

main() {
  if [ "$FROM_SOURCE" = "1" ]; then install_from_source; return; fi
  # No prebuilt bundle runs here: musl-based distros (Alpine, ...) can't
  # execute the glibc-linked AppImage, and there is no musl asset.
  if is_musl; then
    say "musl-based distribution detected - the AppImage needs glibc and won't run here."
    from_source_hint; exit 1
  fi
  check_arch
  if have_apt; then install_deb "$(release_tag)"
  elif have_dnf || have_zypper; then install_rpm "$(release_tag)"
  elif have_pacman; then install_aur
  else install_appimage "$(release_tag)"; fi
  cat <<EOF

Unplug and replug the RTL-SDR dongle once (USB permission rule), then run:
  falconsdr
The UI opens at http://127.0.0.1:8765.
EOF
}

main
