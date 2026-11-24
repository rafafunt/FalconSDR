#!/bin/sh
# Installs or updates the FalconSDR receiver from this checkout (run as root
# on the receiver):  sudo packaging/remote-server/install.sh
# Safe to re-run: code is synced to /opt/falconsdr, settings and recordings
# are kept, and a running service is restarted.
set -eu
SRC=$(cd "$(dirname "$0")/../.." && pwd)
DEST=/opt/falconsdr
HOME_DIR=/var/lib/falconsdr
UNIT=falconsdr-receiver.service

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

# A fresh Debian/Raspberry Pi OS Lite lacks some of these.
if command -v apt-get >/dev/null && ! { command -v rsync && python3 -c "import ensurepip"; } >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync python3-venv >/dev/null
fi

# HackRF/Airspy without SoapySDR (experimental) use the system libraries - small, and harmless without the hardware.
if command -v apt-get >/dev/null && ! dpkg -s libhackrf0 libairspy0 >/dev/null 2>&1; then
    { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libhackrf0 libairspy0 >/dev/null; } ||
        echo "note: could not install libhackrf0 / libairspy0 (only needed for a HackRF or Airspy without SoapySDR)"
fi

getent group plugdev >/dev/null || groupadd --system plugdev
if ! id falconsdr >/dev/null 2>&1; then
    useradd --system --home-dir "$HOME_DIR" --no-create-home --shell /usr/sbin/nologin --groups plugdev falconsdr
fi
mkdir -p "$HOME_DIR"

# Older installs ran as root - carry their settings and library data over once.
for sub in .config/FalconSDR .local/share/FalconSDR; do
    if [ -d "/root/$sub" ] && [ ! -e "$HOME_DIR/$sub" ]; then
        mkdir -p "$HOME_DIR/$(dirname "$sub")"
        cp -a "/root/$sub" "$HOME_DIR/$sub"
        echo "migrated /root/$sub"
    fi
done

mkdir -p "$DEST"
rsync -a --delete --exclude .venv --exclude .git --exclude '__pycache__' \
    --exclude 'packaging/linux-deb/build*' --exclude 'packaging/linux-deb/out' \
    --exclude 'packaging/rpm/build*' --exclude 'packaging/rpm/out' \
    --exclude 'packaging/appimage/build*' --exclude 'packaging/appimage/out' \
    --exclude 'packaging/windows/build*' --exclude 'packaging/windows/out' \
    --exclude demo "$SRC/" "$DEST/"
[ -x "$DEST/.venv/bin/python" ] || python3 -m venv --system-site-packages "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --upgrade -r "$DEST/requirements.txt"

install -D -m 644 "$SRC/packaging/linux-deb/60-rtl-sdr.rules" /etc/udev/rules.d/60-rtl-sdr.rules
install -D -m 644 "$SRC/packaging/linux-deb/blacklist-rtl-dvb.conf" /etc/modprobe.d/blacklist-rtl-dvb.conf
if command -v udevadm >/dev/null; then
    timeout 5 udevadm control --reload-rules >/dev/null 2>&1 || true
    timeout 5 udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || true
fi
command -v rmmod >/dev/null && timeout 5 rmmod dvb_usb_rtl28xxu >/dev/null 2>&1 || true

# The service user owns its data and its venv (so "Install transcription" in the UI can pip into it).
chown -R falconsdr:falconsdr "$HOME_DIR" "$DEST/.venv"
CONFIG="$HOME_DIR/.config/FalconSDR/config.json"
if [ -f "$CONFIG" ]; then
    REC_DIR=$("$DEST/.venv/bin/python" -c "import json,os,sys; p=json.load(open(sys.argv[1])).get('recording', {}).get('output_dir', ''); print(os.path.realpath(p) if p else '')" "$CONFIG")
    HOME_REAL=$(realpath "$HOME_DIR")
    case "$REC_DIR" in
        "$HOME_REAL"|"$HOME_REAL"/*)
            mkdir -p "$REC_DIR" && chown -R falconsdr:falconsdr "$REC_DIR"
            ;;
        "") ;;
        *)
            echo "note: custom recordings dir '$REC_DIR' is outside $HOME_DIR - ensure user 'falconsdr' can write to it." >&2
            ;;
    esac
fi

install -m 644 "$SRC/packaging/remote-server/$UNIT" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet "$UNIT"
systemctl restart "$UNIT"  # starts it, or restarts it onto the new code
echo "Web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8765/"
