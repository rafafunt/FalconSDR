# FalconSDR — self-contained .deb (test build)

## What this is

A "fat" Debian package: [PyInstaller](https://pyinstaller.org/) freezes the
app together with its own Python interpreter and every pip-installable
dependency (numpy, scipy, pyrtlsdrlib) plus the web UI's static files into
one self-contained folder at `/opt/falconsdr`. A user installs it with
`sudo apt install ./falconsdr_*.deb` and gets a working scanner with
**zero** extra `pip install`/system-package steps for RTL-SDR. Starting it
(menu or `falconsdr`) runs the app in the background and opens its UI in
the default browser (http://127.0.0.1:8765); live audio plays in the
browser, so no audio library is bundled at all.

This trades size for reliability: it doesn't depend on whatever numpy/scipy
version happens to already be on the user's system, which is exactly the
"works on my machine, not on theirs" gap a "thin" package is exposed to
across different Ubuntu/Debian releases.

Also installs (`postinst`, `60-rtl-sdr.rules`, `blacklist-rtl-dvb.conf`):
a udev rule granting a plain user access to an RTL2832U dongle's USB device
(0bda:2832/2838), and a modprobe blacklist for the kernel's own DVB-T TV-
tuner driver, which otherwise claims the dongle before this app ever gets a
chance to. A real, reported case without these: on a fresh install with no
other RTL-SDR software ever installed to provide them, the device silently
looked "not detected" (this app's "auto" driver mode falls back to
Simulated on *any* open failure, including a plain permission error) -
matching neither a hardware problem nor a debug-mode one.

## Known limitations of this build (by design, not bugs)

- **RTL-SDR, plus HackRF and Airspy natively (experimental).** `libhackrf` and
  `libairspy` are bundled. SoapySDR-based hardware (LimeSDR,
  SDRplay, USRP/UHD) isn't available — SoapySDR is a system C library with
  its own plugin/driver-module architecture that doesn't freeze into a
  single bundle the way a normal pip package does. Someone with that
  hardware needs the regular `pip install -r requirements.txt` + system
  `python3-soapysdr` install instead (see the main README).
- **In-app transcription install** uses the pip bundled in the app
  (`falconsdr --pip`, installing wheels into
  `~/.local/share/FalconSDR/addons/cp3XX`) - the same mechanism verified on
  the Windows build (`packaging/windows/README.md`); not yet verified in a
  .deb build.
- **MP3 output** uses the ffmpeg bundled from the `imageio-ffmpeg` package
  (amd64/arm64), or a system `ffmpeg` on `PATH` if present.

None of this is a defect in the .deb itself — it's the honest tradeoff of
"self-contained enough that RTL-SDR needs nothing extra" vs.
"everything under the sun works out of the box", and matches how the app
already degrades gracefully when these are missing (see AGENTS.md).

## Building it

For a package other people will install, build it in Docker:

```
packaging/linux-deb/build-in-docker.sh
```

That runs `build.sh` inside Debian 11 (glibc 2.31), so the package works on
Ubuntu 20.04 and newer. A PyInstaller bundle only runs where glibc is the same
or newer than on the build machine: built straight on a current desktop
(`./build.sh`) it starts on older systems but some bundled modules fail with
"GLIBC_2.xx not found". `./build.sh` alone is fine for your own machine.

Builds in a **throwaway venv** (`packaging/linux-deb/build-venv/`, gitignored)
— never the project's own `.venv` — specifically so nothing from your dev
setup can leak into the frozen bundle by accident. Output:
`packaging/linux-deb/out/falconsdr_<date>_<arch>.deb`.

## Testing in a VirtualBox VM

- **Can't fullscreen / window won't resize with the VM window** — install
  the VirtualBox Guest Additions inside the guest (`Devices` menu → "Insert
  Guest Additions CD image…", then run the installer from the mounted CD).
  This is a VM display-integration thing, unrelated to this app.
- **RTL-SDR passthrough**: the host needs a VirtualBox Extension Pack
  version that *exactly* matches the installed VirtualBox version (USB
  2.0/3.0 passthrough silently doesn't work otherwise), the host user needs
  to be in the `vboxusers` group, and the VM needs a USB device filter for
  the dongle (Settings → USB → add filter, or
  `VBoxManage usbfilter add 0 --target "<vm name>" --name "RTL-SDR" --vendorid 0bda --productid 2838`).
- Once attached, `lsusb` inside the guest should show the dongle. If the app
  still doesn't see it, this package's own udev rule/DVB blacklist (see
  above) should already handle the two most common causes - if it's still
  simulated after that, check `dmesg` in the guest for a USB error, and
  confirm the device wasn't grabbed by the DVB-T driver anyway
  (`lsmod | grep dvb`).

## Testing it without fooling yourself

The whole point of a "fat" package is that it works on a machine that has
**none** of your dev environment. Installing it on this same machine and
running it from this same terminal is *not* a real test of that — your
shell may still have the project's `.venv` activated, `PYTHONPATH` pointing
at it, or you might launch it while `cd`'d into the repo (where Python would
happily import the *source* `falconsdr/` package instead of the installed
one, silently passing a test that would fail for anyone else). Concretely:

1. **Install it:**
   ```
   sudo apt install ./packaging/linux-deb/out/falconsdr_*.deb
   ```

2. **Make a throwaway test user** — a real second account on this same
   machine that has never touched this project, so there's no dev-venv,
   `PYTHONPATH`, or repo checkout it could accidentally fall back on:
   ```
   sudo useradd -m -s /bin/bash falconsdr-test
   sudo passwd falconsdr-test          # set a temporary password
   ```
   Switch to it with a full login shell (not plain `su`, which can still
   inherit some of your environment):
   ```
   sudo machinectl shell falconsdr-test@   # or: su - falconsdr-test
   ```
   This account still runs on your real hardware (RTL-SDR, sound card), so
   real scanning/audio testing still works — it just guarantees nothing
   from *your* environment is silently propping the app up.

3. **From that account**, confirm you're actually running the installed
   copy, not the dev one, before doing anything else:
   ```
   which falconsdr        # must print /usr/bin/falconsdr
   cd /                      # anywhere outside the repo - rules out
                              # accidentally importing source falconsdr/
                              # via a cwd-relative sys.path
   falconsdr
   ```
   Launch it from the applications menu too (GNOME/KDE app grid) if you
   want to check the `.desktop`/icon integration, not just the CLI path.

4. **Exercise the real things a first-time user would do**: the browser
   opens by itself; pick the real RTL-SDR device (not Simulation — that
   needs Debug mode in Settings → Advanced, off by default), start a scan,
   confirm live audio in the browser, record something, open it on the
   Recordings page, check the waveform/quality score render correctly, try
   MP3 output both with and without `ffmpeg` installed on that account, and
   click Install under Settings → Transcription & computers to confirm it
   fails with the clear message above instead of doing something strange.
   Settings → Advanced → Quit stops the background program.

5. **Uninstall when done testing:**
   ```
   sudo apt remove falconsdr
   sudo userdel -r falconsdr-test
   ```

If you'd rather not create a second user account, the next-best (weaker)
check is at least: `deactivate` any active venv, `unset PYTHONPATH`, `cd /`,
and run `falconsdr` — this catches the single most common false-positive
(accidentally still running against the dev `.venv`/source tree) but won't
catch "some system package your dev machine happens to already have
installed that a fresh Ubuntu install wouldn't."

## What a clean-room import/startup check already found

Before handing this to you, a bare Ubuntu 24.04 Docker container (`apt
install ./the.deb` with no other packages preinstalled, no GUI, no dev
tools) was used to catch missing shared-library dependencies early (before
the move to the web UI - rerun it for a new build). It cannot exercise real
USB hardware paths (no dongle in a container), which is exactly why step 2
above still matters on real hardware.
