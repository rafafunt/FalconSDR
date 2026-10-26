# Headless receiver (server mode)

The receiver runs FalconSDR as a service and serves its web UI on port
8765 to every device on the network: open `http://<receiver-ip>:8765/` in any
browser (PC, phone). There is no password by default; set one in
Settings → Access if other people share your network.

## Install / update

On the receiver (Debian, Ubuntu, Raspberry Pi OS - 64-bit recommended),
one line installs or updates it and starts the service:

```sh
curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash -s -- --server
```

It installs the latest release (`FALCONSDR_REF=<tag or branch>` in front of
`bash` picks another), keeps its checkout in `/opt/falconsdr-src` and runs
`install.sh` from it. Or, from a checkout of this repository:

```sh
sudo packaging/remote-server/install.sh
```

Either way the service is started (and on every boot) and the address to
open is printed at the end.

The script copies the app to `/opt/falconsdr`, creates its own Python
venv there, adds the unprivileged `falconsdr` user (settings in
`/var/lib/falconsdr/.config/FalconSDR`, recordings by default in
`/var/lib/falconsdr/FalconSDR/recordings`), installs the RTL-SDR udev
rule and DVB-T driver blacklist, and the systemd unit. Re-run it after
`git pull` to update; settings and recordings stay. An older install that ran
as root has its settings moved over automatically the first time.

`falconsdr-receiver.service` starts scanning right away (`--start`) and
closes open recordings cleanly when stopped - so a mode switcher that stops
and starts the unit (like `sdr-mode`) keeps working unchanged.

## Transcription and copies on your PC

A small receiver usually can't run Whisper. Start FalconSDR on your PC,
open Settings → Connect to a server, and enter the receiver's address. The PC
then appears under Settings → Transcription & computers on the receiver,
where you switch on transcription (uses its GPU) and/or keeping a copy of
every recording. If the PC already has its own recordings, the same page
offers to send them to the receiver with their transcripts and favorites.

## USB passthrough

The receiver must see the RTL-SDR dongle. Proxmox LXC (on the Proxmox host,
replace `CTID`):

```sh
pct set CTID -usb0 host=0bda:2838
pct restart CTID
```

LXD:

```sh
lxc config device add INSTANCE rtlsdr usb vendorid=0bda productid=2838
```

The service reconnects by itself once the device appears. Check with
`rtl_test -t` and `systemctl status falconsdr-receiver`.
