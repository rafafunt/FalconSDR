# FalconSDR — AppImage (test build)

## What this is

One executable file that runs on most modern x86_64/aarch64 distributions
with no install step, no root, no pip:

```
chmod +x FalconSDR-<version>-x86_64.AppImage
./FalconSDR-<version>-x86_64.AppImage
```

Inside is the same self-contained
[PyInstaller](https://pyinstaller.org/) `--onedir` bundle the .deb/.rpm
ship (its own Python + numpy/scipy/pyrtlsdrlib + the web UI), packed with
[appimagetool](https://github.com/AppImage/appimagetool). The app opens its
UI in the browser (http://127.0.0.1:8765) like every other build.

## What it cannot do (by design, not bugs)

- **No udev rule / DVB-T blacklist.** An AppImage installs nothing, so on a
  machine with no RTL-SDR software installed yet, USB access needs one
  manual step (the same two files the .deb/.rpm install for you):
  ```
  sudo cp packaging/linux-deb/60-rtl-sdr.rules /etc/udev/rules.d/
  sudo cp packaging/linux-deb/blacklist-rtl-dvb.conf /etc/modprobe.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
  ```
  then unplug and replug the dongle once. Without it, opening the dongle
  as a plain user fails and the app's "auto" driver mode quietly falls back
  to Simulation (AGENTS.md gotcha 8) - indistinguishable from missing
  hardware.
- **RTL-SDR only**, MP3 via the bundled `imageio-ffmpeg` binary or a system
  `ffmpeg` on PATH, transcription via the bundled pip (`falconsdr --pip`) -
  the same tradeoff as the .deb/.rpm (see their READMEs).

## Building it

Needs `appimagetool` on PATH plus python3/pip and appimagetool's own
dependencies `file` and `binutils`:

```
packaging/appimage/build.sh
```

For a file other people will run, build it in Docker:

```
packaging/appimage/build-in-docker.sh
```

That runs `build.sh` inside Debian 11 (glibc 2.31), so the AppImage
starts on Debian 11+, Ubuntu 20.04+, Fedora 41, Arch and newer - the
same glibc-floor trick as `packaging/linux-deb/build-in-docker.sh`.
`./build.sh` alone is fine for your own machine.

Builds in a **throwaway venv** (`packaging/appimage/build-venv/`,
gitignored) - never the project's own `.venv`. Output:
`packaging/appimage/out/FalconSDR-<version>-<arch>.AppImage`.

`falconsdr --self-test` (`falconsdr/selftest.py`) runs on the bundle before
it is packed, same as the .deb/.rpm/Windows builds.

## Testing without fooling yourself

Run it with an empty profile and from outside the repo so the source tree
cannot prop it up:

```
cd /
HOME=/tmp/falconsdr-appimage-test ./FalconSDR-*.AppImage --self-test
HOME=/tmp/falconsdr-appimage-test ./FalconSDR-*.AppImage --no-browser --port 18810
```

then open `http://127.0.0.1:18810/`, pick Simulation (Debug mode) or the
real dongle, and confirm live audio + a recording + its waveform render.
