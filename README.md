# FalconSDR

<img src="packaging/icons/falconsdr.png" width="96" align="right" alt="">

A wideband radio scanner for SDR receivers that **records only the
transmissions it actually stops on** - no hours of empty noise, no retuning
clicks. Point it at any band (aviation, marine, PMR, ham repeaters, public
services, military UHF…) and get a searchable library of conversations.

It runs in your web browser: on the computer the SDR is plugged into, or as a
headless receiver you listen to from any phone or PC on your network.

> Listening to and recording some radio services is restricted or illegal in
> many countries - see [Legal](#legal).

![Listen page: frequency display, live spectrum and waterfall, receiver settings](docs/screenshot-listen.png)


## Features

**Scanning**
- Wideband: the whole capture bandwidth of your SDR is analysed at once, so
  several simultaneous conversations on different frequencies are caught and
  recorded in parallel, not one after another.
- Any number of bands (start, stop, step, modulation) plus a Memory List of
  named single channels in folders. Ready-made presets for common bands.
- Priority weights for bands and channels, and a short "recency boost" that
  revisits a frequency right after it was active, to catch the reply.
- Per-band overrides: trigger level, gain, hold time, squelch, noise
  reduction.
- Blacklist for carriers and interference, one click from the live view.
- AM, NFM, WFM and SSB demodulation; software or hardware gain control.

**Listening**
- Live audio in the browser, with a frequency display, signal level and a
  live spectrum.
- Skip, skip + blacklist, solo a single band, listen to a single frequency.
- Works on phones, with play/pause/next controls on the lock screen and
  headset.

**Recording**
- One file per transmission (WAV or MP3), named by time and
  frequency, with a configurable hold time to keep call and reply together.
- Squelch, spectral noise reduction, a cap on simultaneous recordings.
- Manual continuous recording and raw IQ capture (SigMF) when you need them.
- Automatic cleanup by age or disk space, keeping favorites.

**Recordings library**
- Browse by frequency, band or language; search by frequency, channel name,
  date or transcript text; filter by time and quality.
- Every recording gets a quality score (signal-to-noise), a waveform and
  click-to-seek playback with auto-advance.
- Favorites, bulk delete/transcribe, keyboard shortcuts.
- Flight replay for aviation recordings (118-138 MHz and military UHF
  225-400 MHz): one click opens FlightRadar24 or ADS-B Exchange replaying
  the traffic around you at the moment of the recording - to see who was
  talking. Set your approximate location once in Settings → Location.
- Statistics: busiest frequencies, activity by hour, weekday and day.
- Optional speech-to-text with [Whisper](https://github.com/SYSTRAN/faster-whisper)
  (installed from the app). It can run on a different, faster computer than
  the one scanning.

**Server and multiple computers**
- `--server` mode for a small always-on Linux receiver (mini PC, home
  server, container).
- Other computers connect to it to transcribe recordings with their GPU
  and/or keep a verified local copy of the library.
- Optional access password; English and Polish interface (more languages are
  one translation file away).

## Hardware

| Receiver | How |
|---|---|
| RTL-SDR (incl. RTL-SDR Blog V3/V4) | built in on Linux, Windows and macOS |
| HackRF, Airspy, SDRplay, LimeSDR, USRP, … | through [SoapySDR](https://github.com/pothosware/SoapySDR) with the matching module installed |

Developed and tested with an **RTL-SDR Blog V4** on Linux and Windows 11.
Other receivers and macOS (experimental builds) should work but haven't been
tested yet - reports are welcome.

Any antenna works; one cut or tuned for the band you scan works much better.

## Installation

### One-line installer (Linux)

Detects your distribution and installs the matching package:

```
curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash
```

Debian/Ubuntu get the `.deb`, Fedora/openSUSE the `.rpm`, Arch the
AUR package, anything else (glibc-based, x86_64/aarch64) the AppImage.
`packaging/install.sh
--from-source` installs from a git checkout instead (the SoapySDR-capable
path for HackRF/LimeSDR/Airspy/... - not in the packaged builds; also
the fallback for other CPU architectures, musl-based distros such as
Alpine, and releases whose asset for your format isn't published yet).

### Ubuntu / Debian - .deb package

Download the `.deb` from [Releases](../../releases) and install it:

```
sudo apt install ./falconsdr_<version>_amd64.deb
```

Start **FalconSDR** from the application menu. Unplug and replug the dongle
once after installing - the package adds a udev rule for USB access and stops
the DVB-T TV driver from taking the dongle.

This package is self-contained (RTL-SDR only, no Python needed). For SoapySDR
devices, run from source instead.

### Fedora / openSUSE - .rpm package

Download the `.rpm` from [Releases](../../releases) and install it:

```
sudo dnf install ./falconsdr-<version>-1.<arch>.rpm
```

Same self-contained bundle and the same udev/DVB-T handling as the `.deb`;
replug the dongle once after installing. Needs glibc 2.38+ (current
Fedora / openSUSE Tumbleweed); RHEL/Rocky 9 are too old for this build -
run from source there instead.

### Arch Linux - AUR

```
yay -S falconsdr
```

(repackages the official release build; maintained by
[@9M2PJU](https://github.com/9M2PJU)).

### Any distribution - AppImage

Download `FalconSDR-<version>-<arch>.AppImage` from
[Releases](../../releases), make it executable and run it - no install, no
root:

```
chmod +x FalconSDR-*.AppImage
./FalconSDR-*.AppImage
```

One manual step on a machine with no RTL-SDR software installed yet: copy
`packaging/linux-deb/60-rtl-sdr.rules` to `/etc/udev/rules.d/`, reload udev
and replug the dongle (the `.deb`/`.rpm` do this for you).

### Windows - installer or portable ZIP

Download from [Releases](../../releases) either
`FalconSDR-<version>-windows-x64-setup.exe` (installs for your user, no
administrator rights needed) or `FalconSDR-<version>-windows-x64-portable.zip`
(unzip anywhere and run `falconsdr.exe`). Both are self-contained - no Python
needed; MP3 recording works out of the box and transcription installs from
Settings like in any other install. The program is not code-signed yet: if
SmartScreen says "Windows protected your PC", choose **More info → Run
anyway**.

**RTL-SDR driver:** Windows needs the WinUSB driver on the dongle, installed
once with [Zadig](https://zadig.akeo.ie/). If SDR++, SDR# or another SDR
program already works with your dongle, it's already done. Otherwise: plug in
the dongle → run Zadig → *Options → List All Devices* → pick
**"Bulk-In, Interface (Interface 0)"** (or "RTL2838UHIDIR"; not Interface 1)
→ target driver **WinUSB** → *Replace Driver* → replug the dongle. The same
steps are in the app (Listen → SDR device).

FalconSDR runs in the background with its interface in the browser; quit it
in Settings → Advanced. "Start with the system" is there too (off by default).

### macOS - experimental

Download `FalconSDR-<version>-macos-arm64-experimental.zip` (Apple Silicon:
M1 and newer) or `…-macos-x86_64-…` (Intel Macs) from
[Releases](../../releases), unzip it and move **FalconSDR** to Applications.
These builds are made automatically and pass the same self-test as the other
packages, but haven't been tried on a real Mac with an SDR yet - feedback in
[Discussions](../../discussions) is very welcome.

The app isn't signed by Apple. The first time, right-click it → **Open**
(or System Settings → Privacy & Security → **Open Anyway**). If macOS says
the app "is damaged", that's the same missing signature; clear the download
flag in Terminal:

```
xattr -dr com.apple.quarantine /Applications/FalconSDR.app
```

An RTL-SDR needs no extra driver on macOS.

### From source (Linux, Windows, macOS)

Requires Python 3.10+.

```
git clone https://github.com/rafafunt/FalconSDR.git
cd FalconSDR
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
.venv/bin/python main.py                          # Windows: .venv\Scripts\python main.py
```

The interface opens at `http://127.0.0.1:8765`.

- **Linux:** for RTL-SDR access without root, install your distribution's
  `rtl-sdr` package (udev rules) or copy `packaging/linux-deb/60-rtl-sdr.rules`
  to `/etc/udev/rules.d/`. For SoapySDR devices install `python3-soapysdr` and
  the device's `soapysdr-module-*`.
- **Windows:** the dongle needs the WinUSB driver - see *RTL-SDR driver*
  above.
- **MP3 recordings** use the ffmpeg that comes with the `imageio-ffmpeg`
  package (or a system `ffmpeg` on PATH); on platforms without a wheel
  (32-bit ARM) install ffmpeg from the system, otherwise recordings stay WAV.

### Updating and removing

Install the newer `.deb` or Windows installer over the old one - settings,
channels and recordings are kept. Remove with `sudo apt remove falconsdr` or
Windows *Settings → Apps*; your recordings and settings stay on disk (see
[Where things are stored](#where-things-are-stored)).

### Headless receiver

On a Linux machine with the SDR attached:

```
sudo packaging/remote-server/install.sh
sudo systemctl enable --now falconsdr-receiver
```

Then open `http://<receiver-ip>:8765` from any device. See
[packaging/remote-server/README.md](packaging/remote-server/README.md).

## First steps

1. **Listen** page: pick your SDR, then add a band (or a preset) and press
   Start.
2. Raise or lower the **trigger level** until real transmissions open and
   noise doesn't.
3. Switch on **recording** when you're happy with what it stops on.
4. Name the frequencies you hear in the **Channels** page (or straight from a
   recording) - names show up everywhere, including search.

## Tips for good recordings

- **Trigger level** decides what counts as a transmission. Lower = weaker
  stations get recorded, but more noise too.
- **Some frequencies are just a constant carrier or interference** (birdies
  from your own computer or USB hub, a nearby transmitter's harmonics, a dead
  carrier). They trip the trigger all the time and keep you from lowering it.
  Add them to the **blacklist** (Listen → *Skip + blacklist*, or Channels →
  Blacklist). After that you can usually lower the trigger level by several
  dB and pick up the weak stations you were missing.
- **Gain** defaults to *Auto (software)*. The tuner's own hardware AGC tends
  to overload on a strong nearby station, which shows up as false detections
  on neighbouring channels.
- **Hold after a transmission** (5 s by default) keeps a recording open
  through the pause between a call and its answer.
- A narrower **step** in a band means more channels to check per sweep; use
  the channel spacing the band really uses.
- If the Activity feed says *this computer can't keep up with the SDR*, data
  is being lost (short gaps in audio and recordings): lower the capture
  bandwidth, or move off a virtual machine - USB passed through to a VM is a
  common cause.

## Command line

`falconsdr` for the installed packages (Windows: `falconsdr.exe` in its install
folder), `python main.py` from source:

```
falconsdr               local mode: scan on this computer, UI on 127.0.0.1:8765
falconsdr --server      reachable from the whole network
falconsdr --start       start scanning right away
falconsdr --local       scan locally even if this computer is set up as a client of a server
falconsdr --port 9000   another port
falconsdr --no-browser  don't open the browser
falconsdr --version     show the version
falconsdr --self-test   check this install has everything it needs
```

## Where things are stored

| What | Where |
|---|---|
| Settings, channels, blacklist | `~/.config/FalconSDR/` |
| Recordings (default, changeable) | `~/FalconSDR/recordings/`, one folder per day |
| Quality scores, transcripts, favorites | `~/.local/share/FalconSDR/` |

(`~` is your home folder, also on Windows.)

## Troubleshooting

- **"Failed to connect… retrying"** - the SDR is missing, in use by another
  program, lacks USB permissions (Linux: udev rule, replug) or has the wrong
  driver (Windows: Zadig).
- **Nothing gets recorded** - recording is off, the trigger level is too high,
  or the band's modulation is wrong (aviation is AM, most other VHF/UHF voice
  is NFM).
- **Constant recordings of noise** - blacklist the offending frequencies (see
  above) or raise the trigger level.
- **Distorted audio on strong stations** - switch gain to *Auto (software)* or
  lower the manual gain.

## Feedback and contributing

- **Questions, ideas, your setup:** [Discussions](../../discussions)
- **Bugs:** [Issues](../../issues) - please include the version (Settings →
  Advanced, at the bottom), your OS, how you installed it, your SDR and the
  band.
- **Translations:** the interface is English with a Polish translation in
  `falconsdr/locale/pl.po`; a new language is a copy of that file (see
  `falconsdr/i18n.py`).
- **Code:** pull requests are welcome; `AGENTS.md` describes the architecture
  and the pitfalls already hit once. Tests: `python -m unittest tests/test_core.py`.

## Legal

The laws on radio monitoring differ a lot between countries. In some places
you may listen to most bands but not record or share what you hear; in
others even listening to non-public services (aviation, police, emergency
services, military) is forbidden. Check what applies where you live before
you scan, and especially before you record or publish anything - you alone
are responsible for how you use this program.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)) - required by `pyrtlsdr` and the
bundled `librtlsdr`. The Windows/Linux packages list every bundled component
and its license in `THIRD-PARTY-LICENSES.txt` (ffmpeg in them is a GPLv3
build). Transcription software and Whisper models are not bundled: they are
downloaded on the user's own request from their publishers (faster-whisper,
CTranslate2 and the default models `Systran/faster-whisper-large-v3` and
`jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper` are MIT-licensed).
