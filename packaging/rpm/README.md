# FalconSDR — self-contained .rpm (test build)

## What this is

A "fat" RPM: [PyInstaller](https://pyinstaller.org/) freezes the app
together with its own Python interpreter and every pip-installable
dependency (numpy, scipy, pyrtlsdrlib) plus the web UI's static files into
one self-contained folder at `/opt/falconsdr`. A user installs it with
`sudo dnf install ./falconsdr-*.rpm` (or `sudo zypper install` /
`sudo rpm -i` on openSUSE) and gets a working scanner with **zero** extra
`pip install` steps for RTL-SDR. The RPM counterpart of
`packaging/linux-deb/` - same bundle, same tradeoff.

Also installs: a udev rule granting a plain user access to an RTL2832U
dongle's USB device (0bda:2832/2838), and a modprobe blacklist for the
kernel's own DVB-T TV-tuner driver
(`packaging/linux-deb/60-rtl-sdr.rules`, `blacklist-rtl-dvb.conf`).
A device already plugged in at install time still needs a replug.

## Known limitations of this build (by design, not bugs)

- **RTL-SDR only.** SoapySDR-based hardware (HackRF, LimeSDR, Airspy,
  SDRplay, USRP/UHD) isn't available - same reason as the .deb: SoapySDR
  is a system C library with its own plugin architecture that doesn't
  freeze into a single bundle. That hardware needs the regular
  `pip install -r requirements.txt` install instead (see the main README).
- **MP3 output** uses the ffmpeg bundled from the `imageio-ffmpeg` package
  (x86_64/aarch64), or a system `ffmpeg` on `PATH` if present
  (`Recommends: ffmpeg`).
- **In-app transcription install** uses the pip bundled in the app
  (`falconsdr --pip`) - the same mechanism verified on the Windows build.

## Building it

For a package other people will install, build it in Docker:

```
packaging/rpm/build-in-docker.sh
```

That runs `build.sh` inside Fedora 41, so the bundle needs glibc 2.38+.
That covers current Fedora and openSUSE Tumbleweed; RHEL/Rocky 9
(glibc 2.34) are too old for this build - install from source there
instead (see the main README). `./build.sh` alone is fine for your own
machine.

Builds in a **throwaway venv** (`packaging/rpm/build-venv/`, gitignored) -
never the project's own `.venv`. Output:
`packaging/rpm/out/falconsdr-<version>-1.<arch>.rpm`.

`packaging/rpm/falconsdr.spec` is the source-build spec for COPR/OBS
(reviewers, rebuild-from-tag workflows); `build.sh` generates an
equivalent binary-from-tree spec internally for the local PyInstaller
bundle. Both read the version from `falconsdr/__init__.py` - the one
place the version lives.

## Testing without fooling yourself

Same rule as the .deb README: install it on an account/machine that has
none of your dev environment (no active `.venv`, no `PYTHONPATH`, `cd /`
before running), and confirm `which falconsdr` is `/usr/bin/falconsdr`.
`falconsdr --self-test` runs as part of the build; a real scan needs the
dongle plus the udev rule + replug above.
