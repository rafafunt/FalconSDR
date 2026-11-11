# FalconSDR

<img src="packaging/icons/falconsdr.png" width="96" align="right" alt="">

A wideband radio scanner for SDR receivers that **records only the
transmissions it actually stops on** - no hours of empty noise, no retuning
clicks. Point it at any band (aviation, marine, PMR, ham repeaters, public
services, military UHF…) and get a searchable library of conversations.

It runs in your web browser: on the computer the SDR is plugged into, or as a
headless receiver you listen to from any phone or PC on your network.

**You can try UI and UX also in virtual ether (simulated transmissions) before downloading:** the real interface on a simulated
receiver at [falconsdr.com](https://falconsdr.com).

> Listening to and recording some radio services is restricted or illegal in
> many countries - see [Legal](#legal).

![Listen page: frequency display, live spectrum and waterfall, receiver settings](docs/screenshot-listen.png)


## Features

**Scanning**
- Wideband: the whole capture bandwidth of your SDR is analysed at once, so
  several simultaneous conversations on different frequencies are caught and
  recorded in parallel, not one after another.
- Any number of bands (start, stop, step, modulation) plus a Memory List of
  named single channels in folders - sortable by any column, like the
  blacklist (click a header, click again to reverse). Ready-made presets for
  common bands.
- Priority weights for bands and channels, and a short "recency boost" that
  revisits a frequency right after it was active, to catch the reply.
- Per-band overrides: trigger level, gain, hold time, squelch, noise
  reduction.
- Blacklist for carriers and interference, one click from the live view.
- AM, NFM, WFM and SSB demodulation; software or hardware gain control.
- Experimental: several receivers at once - the scan plan is split between
  them, so each band is revisited that many times more often. They can be of
  different kinds and widths (say an RTL-SDR at 2.4 MHz next to a HackRF at
  10 MHz); the plan is shared in proportion to their capture widths.
- Optional **antenna range** per dongle (SDR device card): one on a 2 m
  antenna scans only 144-146 MHz, one on a 70 cm antenna only 430-440 MHz.
  What no range reaches is listed on the Listen page.
- Optional, experimental digital voice: DMR and Yaesu System Fusion (C4FM)
  through [dsd-fme](https://github.com/lwvmobile/dsd-fme), installed from
  Settings → Digital voice. Encrypted calls stay muted.

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
- Favorites, bulk favorite/delete/transcribe, download a whole selection as
  one ZIP, keyboard shortcuts.
- Flight replay for aviation recordings (118-138 MHz and military UHF
  225-400 MHz): one click opens FlightRadar24 or ADS-B Exchange replaying
  the traffic around you at the moment of the recording - to see who was
  talking. Set your approximate location once in Settings → Location.
- Statistics: busiest frequencies, activity by hour, weekday and day.
- Optional speech-to-text with [Whisper](https://github.com/SYSTRAN/faster-whisper)
  (installed from the app; one model, `large-v3`, for every language - an
  English aviation model can be added in the settings, and fixing the language
  skips its detection). It can run on a
  different, faster computer than the one scanning; see
  [what it needs](#transcription-needs-more-than-scanning).

**Server and multiple computers**
- `--server` mode for a small always-on Linux receiver (mini PC, home
  server, container).
- Other computers connect to it to transcribe recordings with their GPU
  and/or keep a verified local copy of the library.
- An RTL-SDR on another computer works as a source too, through its `rtl_tcp`
  server ([network receiver](#network-receiver-rtl_tcp)).
- Optional access password; English and Polish interface (more languages are
  one translation file away).

## Hardware

| Receiver | How |
|---|---|
| RTL-SDR (incl. RTL-SDR Blog V3/V4) | built in on Linux, Windows and macOS |
| HackRF, Airspy | built in on Windows, `.deb`, `.rpm`, AppImage and (since 1.4.1) macOS (experimental) |
| An RTL-SDR on another computer | over the network through its `rtl_tcp` server (since 1.5.0, see [Network receiver](#network-receiver-rtl_tcp)) |
| SDRplay, LimeSDR, USRP, … | through SoapySDR, Linux source install (not tested) |

An RTL-SDR is opened through librtlsdr; on Linux from source also through
SoapySDR - choose under *SDR device → Access method*. HackRF and Airspy work
through the bundled `libhackrf` / `libairspy` (experimental: confirmed on a
real HackRF One, **untested on an Airspy**; pick *Access method → libhackrf /
libairspy*); on Linux from source they work
through [SoapySDR](https://github.com/pothosware/SoapySDR) with the matching
module, or through the same built-in access once `libhackrf0` / `libairspy0`
are installed.

HackRF captures 2-20 MHz (the default 2.4 MHz is the safest start); an Airspy
only the fixed widths its board has (for instance 2.5 and 10 MHz on an Airspy
R2), and FalconSDR picks the nearest. A wider capture needs more processor -
on a 4-core mini PC about 1, 8 and 19 ms of every 30 ms block go to the
spectrum at 2.4, 10 and 20 MHz, plus 2, 9 and 17 ms for each transmission being
recorded. The macOS builds carry the HackRF/Airspy libraries since 1.4.1
(untested on a real Mac).

Developed and tested with an **RTL-SDR Blog V4** on Linux and Windows 11.
A **HackRF One** has been checked on Linux (10 MHz capture; SoapySDR and
libhackrf both scan, libhackrf reported no lost samples) by
[phutch](https://github.com/phutch) in
[issue #9](https://github.com/rafafunt/FalconSDR/issues/9) - thank you.
Other receivers and macOS (experimental builds) should work but haven't been
tested yet - reports are welcome.

**Testing a receiver:** stop the scanner and run `falconsdr --probe-sdr`
(from source: `python main.py --probe-sdr`; add `hackrf_native` / `hackrf` /
`airspy_native` / `airspy` to pick the way it is opened, or `rtl_tcp` to
measure the server whose address is saved on the Listen page; on Windows the output
goes to `%USERPROFILE%\.config\FalconSDR\falconsdr.log`). It measures the
gain steps, the DC spike, how many blocks after a retune still hold the old
frequency, and sample losses, and prints one `RESULT {...}`
line - please paste it into an issue together with your receiver's model.

Any antenna works; one cut or tuned for the band you scan works much better.

### Transcription needs more than scanning

Scanning and recording are light - a small mini PC runs them fine. The
figures below are only for the optional speech-to-text (it can also run on
another computer, see [Headless receiver](#headless-receiver)). Pick the
model in Settings → Whisper models.

| Model | RAM (GPU: VRAM) | Word errors, clean / noisy radio |
|---|---|---|
| `small` - smallest usable | 1.4 GB (0.8 GB) | 30 % / 51 % |
| `medium` - best without a GPU | 3.2 GB (1.4 GB) | 23 % / 44 % |
| `large-v3` - default | 3.9 GB (3.3 GB) | 21 % / 38 % |

How long transcribing takes, as a multiple of the recording's length, for
short ~6 s recordings (at 2× it keeps up as long as you record less than
about 30 minutes of audio per hour):

| Model | Mini PC, 4 threads* | Desktop, 8 threads† | Desktop, 24 threads† | Nvidia GPU‡ |
|---|---|---|---|---|
| `small` | 1.0× | 0.5× | 0.3× | 0.03× |
| `medium` | 2.9× | 1.1× | 0.7× | 0.06× |
| `large-v3` | 6.0× | 2.1× | 1.2× | 0.08× |

- A CPU alone is enough to transcribe while scanning: it only has to keep up
  with how much you record, and a queue that builds up in a busy spell empties
  afterwards. An Nvidia GPU makes it 5-20 times faster, which pays off for
  transcribing hundreds of existing recordings quickly or a very busy band.
  AMD/Intel GPUs are not used.
- Scanning plus transcription: at least 4 CPU threads; transcription uses
  the ones beyond 4 (1, 4 and 20 in the columns above; at least one), so 8 is
  comfortable.
- `tiny` and `base` are not usable (45-90 % errors). On a CPU `turbo` is no
  faster than `large-v3`.

\*ThinkCentre mini PC, 4-core i5-6500T, 1 transcription thread. †Ryzen 9
3900X limited to 4 and 20 transcription threads; a slower CPU will be slower.
‡RTX 2070 SUPER. Without language detection (adds ~80 %). Short recordings
are the slower case per second of audio: nine longer ones (13 s to 7 min)
ran up to 2× faster (the mini PC: `small` 0.44×, `medium` 1.0×), but needed
more memory (`medium` peaked at 3.2 GB on the 7-minute one). Rough figures,
one run each.

## Installation

### One-line installer (Linux)

Detects your distribution and installs the matching package:

```
curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash
```

Debian/Ubuntu get the `.deb`, Fedora/openSUSE the `.rpm`, Arch the AUR
package, anything else the AppImage. Add `--from-source` to install from a
git checkout instead: the way to get SoapySDR support (LimeSDR, SDRplay,
USRP, …, and HackRF/Airspy through SoapySDR instead of the built-in access)
and the fallback for other CPU architectures or musl distros such as Alpine.
The built-in HackRF/Airspy access needs their system libraries, which a source
install does not bring along: add `--with-sdr-libs` (installs `libhackrf0` and
`libairspy0` with apt, or `hackrf` and `airspyone_host` with dnf, using sudo):
`curl -fsSL … | bash -s -- --from-source --with-sdr-libs`.

### Ubuntu / Debian - .deb package

Download the `.deb` from [Releases](../../releases) and install it:

```
sudo apt install ./falconsdr_<version>_amd64.deb
```

Start **FalconSDR** from the application menu. Unplug and replug the dongle
once after installing - the package adds a udev rule for USB access and stops
the DVB-T TV driver from taking the dongle.

This package is self-contained (RTL-SDR, plus HackRF and Airspy natively -
experimental; no Python needed). For SoapySDR devices, run from source instead.

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
steps are in the app (Listen → SDR device). A HackRF or Airspy may need the
same driver step; the built-in access to them is untested on Windows with real
hardware.

FalconSDR runs in the background with its interface in the browser; quit it
in Settings → Advanced. "Start with the system" is there too (off by default).

### macOS - experimental

Download `FalconSDR-<version>-macos-arm64-experimental.zip` (Apple Silicon:
M1 and newer) or `…-macos-x86_64-…` (Intel Macs) from
[Releases](../../releases), unzip it and move **FalconSDR** to Applications.
These builds are made automatically and pass the same self-test as the other
packages, but haven't been tried on a real Mac with an SDR yet - feedback in
[Discussions](../../discussions) is very welcome.

The app isn't signed by Apple. The first time, try to open it, then go to
System Settings → Privacy & Security → **Open Anyway** (since macOS 15 Sequoia
right-click → Open no longer skips this check; on older macOS it does). If
macOS says the app "is damaged", clear the download flag in Terminal:

```
xattr -dr com.apple.quarantine /Applications/FalconSDR.app
```

Builds before 1.5.0 had a broken signature seal (a licence file was added
after the app was signed), which is a likely reason macOS calls a downloaded
app "damaged". From 1.5.0 the build seals it again and checks the signature,
so the message should no longer appear; if it does, the command above is the
fix (and please tell us in [Discussions](../../discussions)).

An RTL-SDR needs no extra driver on macOS. Builds since 1.4.1 also carry the
HackRF and Airspy libraries (built-in access, experimental); they only
check that the libraries load - no real Mac with a HackRF or Airspy has tried them.

### From source (Linux, Windows, macOS)

Requires Python 3.10+.

```
git clone https://github.com/rafafunt/FalconSDR.git
cd FalconSDR
python3 -m venv --system-site-packages .venv       # lets the venv see python3-soapysdr (Linux)
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
.venv/bin/python main.py                          # Windows: .venv\Scripts\python main.py
```

The interface opens at `http://127.0.0.1:8765`. To update: `git pull`, run the
`pip install` line again and restart (or run the one-liner with
`--from-source` again from the folder above `FalconSDR/`).

- **Linux:** for RTL-SDR access without root, install your distribution's
  `rtl-sdr` package (udev rules) or copy `packaging/linux-deb/60-rtl-sdr.rules`
  to `/etc/udev/rules.d/`. For SoapySDR devices install `python3-soapysdr` and
  the device's `soapysdr-module-*` (e.g. `sudo apt install python3-soapysdr
  soapysdr-module-hackrf`); the venv must be created with
  `--system-site-packages`, as above, or it cannot see them. HackRF and Airspy
  can also be opened without SoapySDR (experimental, *SDR device → Access
  method*; the Windows, `.deb`, `.rpm` and AppImage builds already carry the libraries):
  `sudo apt install libhackrf0 libairspy0`, or run the one-line installer with
  `--with-sdr-libs`.
- **Windows:** the dongle needs the WinUSB driver - see *RTL-SDR driver*
  above.
- **MP3 recordings** use the ffmpeg that comes with the `imageio-ffmpeg`
  package (or a system `ffmpeg` on PATH); on platforms without a wheel
  (32-bit ARM) install ffmpeg from the system, otherwise recordings stay WAV.

### Updating and removing

Quit FalconSDR, then install the newer `.deb`, `.rpm` or Windows installer over
the old one - settings, channels, favorites and recordings are kept. Start it
again afterwards (a copy left running through the update shows errors). An
AppImage is replaced by downloading the new one. A source install is updated
by running the `--from-source` line again from the folder that holds
`FalconSDR/` (it pulls the checkout and refreshes the venv), or by `git pull`
in the checkout and the `pip install` line; the headless receiver, by its own
line again (see below). Remove with `sudo apt remove
falconsdr`, `sudo dnf remove falconsdr` or Windows *Settings → Apps*; your
recordings and settings stay on disk (see
[Where things are stored](#where-things-are-stored)).

### Headless receiver

On a Linux machine with the SDR attached (Debian, Ubuntu, Raspberry Pi OS):

```
curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash -s -- --server
```

It installs the latest release as a service that starts on boot and ends
with the address to open from any device on your network
(`http://<receiver-ip>:8765`). Run the same line again to update - settings
and recordings are kept. Details, other versions and helper computers for
transcription: [packaging/remote-server/README.md](packaging/remote-server/README.md).
It also installs `libhackrf0` and `libairspy0` (small), so a HackRF or Airspy
works through the built-in access without extra steps. The service sees the system's SoapySDR: `sudo apt install python3-soapysdr
soapysdr-module-<your receiver>` and restart it (`sudo systemctl restart
falconsdr-receiver`), then pick the access method under *SDR device*.

### Network receiver (rtl_tcp)

New in 1.5.0. An RTL-SDR plugged into another computer (a Raspberry Pi at the antenna, a
box in the attic, a phone running an `rtl_tcp` app) can be the receiver of
the scanner: on that computer run `rtl_tcp -a 0.0.0.0` (package `rtl-sdr`),
then on the Listen page choose *SDR device → Network receiver (rtl_tcp)* and
type its address (`192.168.1.20` or `192.168.1.20:1234`). Scanning, recording,
transcription and the web interface stay on the computer running FalconSDR.

Good to know:
- `rtl_tcp` serves **one program at a time** and the stream is **not
  encrypted** - use it on a trusted network only.
- 2.4 MHz of capture is about 38 Mbit/s: wired Ethernet is fine, Wi-Fi can be
  marginal (pick a smaller capture width if blocks are lost).
- A retune is sent over the network and takes longer than with a dongle on
  USB: on a wired LAN with an RTL-SDR Blog V4 the first clean block came
  after ~180 ms instead of ~100 ms, and a full cycle of 9 capture windows
  took 2.5 s instead of 1.8 s. Scanning many windows is slower; one frequency
  or a few windows are not affected.
- If `rtl_tcp` stops, FalconSDR shows *Reconnecting* and resumes by itself
  once the server is back (run `rtl_tcp` as a service that restarts).
- It does not make a weak USB host stronger: the server still reads the
  dongle over its own USB.
- Bias-T is only switched on when you turn it on here (and off again when the
  scanner stops cleanly - if the connection is lost it stays as it was until
  the next session); the server's own setting is otherwise left alone.

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
  bandwidth (a wide HackRF capture costs much more than an RTL-SDR's 2.4 MHz),
  or move off a virtual machine - USB passed through to a VM is a common cause.

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
falconsdr --probe-sdr   measure the receiver with the scanner stopped (see Hardware)
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
- **A HackRF or Airspy is plugged in but not listed** - on Linux the Listen
  page names what is missing: SoapySDR (source install; see above) or the
  `libhackrf` / `libairspy` library (`sudo apt install libhackrf0 libairspy0`, Fedora:
  `sudo dnf install hackrf airspyone_host`, or the installer's `--with-sdr-libs`). If one way to open a receiver
  fails, try the other under *SDR device → Access method*.
- **Scanning freezes or the receiver keeps disconnecting** (reported with an
  RTL-SDR v3 on Windows) - switch *Settings → Advanced → Continuous RTL-SDR
  reading* off and start the scan again. It is read as a continuous stream
  while the scanner stays on a frequency; with it off the buzz that 1.3.0
  fixed may come back. Please tell us in an issue whether it helped.
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
bundled `librtlsdr`. The Windows, Linux and (since 1.4.1) macOS packages list every bundled component
and its license in `THIRD-PARTY-LICENSES.txt` (ffmpeg in them is a GPLv3
build; `libhackrf` and `libairspy` are BSD-3-Clause, `libusb` LGPL-2.1).
Transcription software and Whisper models are not bundled: they are
downloaded on the user's own request from their publishers (faster-whisper,
CTranslate2 and the default model `Systran/faster-whisper-large-v3` are MIT-licensed, as
is the optional English aviation model `jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper`).
Neither is the digital voice decoder: on request, the app downloads
[dsd-fme](https://github.com/lwvmobile/dsd-fme) and
[mbelib](https://github.com/lwvmobile/mbelib) from their authors (the
authors' Windows build; source compiled on the user's machine elsewhere) and
runs dsd-fme as a separate program.
