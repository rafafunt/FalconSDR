# FalconSDR

<img src="packaging/icons/falconsdr.png" width="96" align="right" alt="">

A wideband radio scanner for SDR receivers that **records only the
transmissions it actually stops on** - no hours of empty noise, no retuning
clicks. Point it at any band (aviation, marine, PMR, ham repeaters, public
services, military UHF…) and get a searchable library of conversations.

It runs in your web browser: on the computer the SDR is plugged into, or as a
headless receiver you listen to from any phone or PC on your network.

**Try it before downloading:** the real interface on a simulated receiver at
[falconsdr.com](https://falconsdr.com).

> Listening to and recording some radio services is restricted or illegal in
> many countries - see [Legal](#legal).

![Listen page: frequency display, live spectrum and waterfall, receiver settings](docs/screenshot-listen.png)


## Features

**Scanning**
- Wideband: the whole capture bandwidth of your SDR is analysed at once, so
  several simultaneous conversations on different frequencies are caught and
  recorded in parallel, not one after another.
- Any number of bands (start, stop, step, modulation; drag to reorder) plus a
  Memory List of named channels in folders. Ready-made presets for common bands.
- Priority weights for bands and channels, and a short "recency boost" that
  revisits a frequency right after it was active, to catch the reply.
- Per-band overrides: trigger level, gain, hold time, squelch, noise reduction.
- Blacklist for carriers and interference, one click from the live view.
- AM, NFM, WFM and SSB; software or hardware gain control.
- Several receivers at once (experimental): the scan plan is split between
  them in proportion to their capture widths, and an optional **antenna range**
  keeps a dongle on a 2 m antenna to 144-146 MHz.
- Digital voice (experimental): DMR and Yaesu System Fusion (C4FM) through
  [dsd-fme](https://github.com/lwvmobile/dsd-fme), installed from Add-ons.
  Encrypted calls stay muted.

**Add-ons** (all optional)
- Decoders for what the scanner already receives: **ACARS**, **APRS** and
  **TETRA** (clear-mode, experimental) built in, plus **VDL2** (dumpvdl2) and
  **rtl_433** sensors (weather stations, thermometers, tyre pressure, meters,
  door and alarm sensors…) as separate programs. Everything decoded lands on
  the **Messages** page with search, filters, a map link and statistics.
- **Notifications** with rules ("a sensor below 0 °C", "a word in an ACARS
  message", "a receiver lost") to ntfy, Telegram, Discord/Slack and other
  webhooks or MQTT. Nothing is sent until you add a channel.
- A **schedule** per receiver: scan some bands in some hours, run an add-on in
  others. Details: [docs/addons.md](docs/addons.md).

**Listening and recording**
- Live audio in the browser with a frequency display, signal level and live
  spectrum; voice is levelled so a weak airband station and a strong fire
  channel sound alike. Works on phones, with lock-screen controls.
- Skip, skip + blacklist, solo a band, listen to a single frequency.
- One file per transmission (WAV or MP3), named by time and frequency, with a
  hold time that keeps call and reply together. Squelch, noise reduction, a cap
  on simultaneous recordings, manual recording and raw IQ (SigMF).
- Automatic cleanup by age or disk space, keeping favorites.

**Recordings library**
- Browse by frequency, band or language; search by frequency, channel name,
  date or transcript; filter by time and quality.
- A quality score that tells speech from noise and data bursts, a waveform,
  click-to-seek playback with auto-advance, favorites, bulk actions, ZIP
  download.
- Flight replay for aviation recordings: one click opens FlightRadar24 or
  ADS-B Exchange at the moment of the recording around your location.
- Statistics: busiest frequencies, activity by hour, weekday and day.
- Optional speech-to-text with [Whisper](https://github.com/SYSTRAN/faster-whisper),
  installed from the app; it can run on a faster computer than the one
  scanning ([what it needs](#transcription-needs-more-than-scanning)).

**Server, updates and help**
- `--server` mode for an always-on Linux receiver; other computers can
  transcribe for it and keep a verified copy of the library.
- Tells you when a new version is out (only if you allow it, asked once); the
  Windows installer and the AppImage update with one click.
- *Settings → Receiver health* shows lost samples and other signs of trouble;
  *Test the receiver* measures it and *Report a problem* opens a GitHub issue
  with the report filled in.
- Optional access password; light and dark theme; English and Polish.

## Hardware

| Receiver | How |
|---|---|
| RTL-SDR (incl. RTL-SDR Blog V3/V4) | built in, every system |
| An RTL-SDR on another computer | over the network through `rtl_tcp` ([below](#network-receiver-rtl_tcp)) |
| HackRF, Airspy | built in (experimental; HackRF One confirmed, Airspy untested) |
| SDRplay RSP1/1A/1B/2/duo/dx/dx-R2 | through the free [SDRplay API](https://www.sdrplay.com/api/) (experimental, untested on a real RSP) |
| LimeSDR, USRP, … | through SoapySDR, Linux source install (untested) |

- **RTL-SDR** is opened through librtlsdr; from source on Linux also through
  SoapySDR (*SDR device → Access method*).
- **HackRF / Airspy** use the bundled `libhackrf` / `libairspy` (from source:
  `sudo apt install libhackrf0 libairspy0`, or SoapySDR). HackRF captures
  2-20 MHz; an Airspy only its board's fixed widths, and FalconSDR picks the
  nearest. A wider capture needs more processor.
- **SDRplay** needs the *SDRplay API* 3.15 or newer, installed once from
  [sdrplay.com/api](https://www.sdrplay.com/api/): it brings the driver (no
  Zadig on Windows) and a service, and FalconSDR loads it from there. If an
  RSP is plugged in without it, the Listen page says so and links to the
  download. A 6 MHz capture width is a good start.

Developed with an **RTL-SDR Blog V4** on Linux and Windows 11; a HackRF One
was checked on Linux by [phutch](https://github.com/phutch)
([#9](https://github.com/rafafunt/FalconSDR/issues/9)) - thank you. For any
other receiver, a report from *Settings → Receiver health → Test the receiver
→ Report a problem* is very welcome (command line: `falconsdr --probe-sdr`).

Any antenna works; one cut for the band you scan works much better.

### Transcription needs more than scanning

Scanning and recording are light - a small mini PC runs them fine. Only the
optional speech-to-text needs more; it can run on another computer
([Headless receiver](#headless-receiver)). Pick the model on the Add-ons page.

| Model | RAM (GPU: VRAM) | Word errors, clean / noisy radio | Time per recording length: mini PC* / 8-thread desktop / Nvidia GPU |
|---|---|---|---|
| `small` | 1.4 GB (0.8 GB) | 30 % / 51 % | 1.0× / 0.5× / 0.03× |
| `medium` - best without a GPU | 3.2 GB (1.4 GB) | 23 % / 44 % | 2.9× / 1.1× / 0.06× |
| `large-v3` - default | 3.9 GB (3.3 GB) | 21 % / 38 % | 6.0× / 2.1× / 0.08× |

A CPU is enough while scanning - it only has to keep up with how much you
record (at 2× that is up to about 30 minutes of audio per hour). A GPU is
5-20 times faster, worth it for a big backlog or a busy band: Nvidia, or AMD
Radeon RX 6600-6950, 7000 and 9000 series and Ryzen 780M/880M/890M/8060S
graphics ("Install with GPU support" fetches AMD's ROCm runtime, several GB;
AMD on Windows is untested). A Radeon RX 9070 did real recordings with
`large-v3` + the English ATC model and language detection in 0.7 s each
(~11× real time; 14× faster than a 24-thread CPU). Intel GPUs are not used. Plan at least 4 CPU threads for scanning plus transcription,
8 is comfortable. `tiny` and `base` are not usable.

\*4-core i5-6500T, short ~6 s recordings, without language detection (adds
~80 %); rough figures, one run each.

## Installation

### One-line installer (Linux)

```
curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash
```

Debian/Ubuntu get the `.deb`, Fedora/openSUSE the `.rpm`, Arch the AUR
package, anything else the AppImage. `--from-source` installs from a git
checkout instead (SoapySDR hardware, other CPU architectures, musl distros);
add `--with-sdr-libs` there for the built-in HackRF/Airspy access.

### Ubuntu / Debian - .deb

```
sudo apt install ./falconsdr_<version>_amd64.deb
```

From [Releases](../../releases). Start **FalconSDR** from the application menu
and replug the dongle once - the package adds a udev rule for USB access and
keeps the DVB-T TV driver off the dongle. Self-contained: no Python needed.

### Fedora / openSUSE - .rpm

```
sudo dnf install ./falconsdr-<version>-1.<arch>.rpm
```

The same bundle and udev handling as the `.deb`. Needs glibc 2.38+ (current
Fedora / openSUSE Tumbleweed); on RHEL/Rocky 9 run from source.

### Arch Linux - AUR

`yay -S falconsdr` (repackages the release build; maintained by
[@9M2PJU](https://github.com/9M2PJU)).

### Any distribution - AppImage

```
chmod +x FalconSDR-*.AppImage
./FalconSDR-*.AppImage
```

No install, no root. On a machine with no RTL-SDR software yet, copy
`packaging/linux-deb/60-rtl-sdr.rules` to `/etc/udev/rules.d/`, reload udev and
replug the dongle.

### Windows

Download `FalconSDR-<version>-windows-x64-setup.exe` (installs for your user,
no administrator rights) or the `…-portable.zip` from [Releases](../../releases).
Both are self-contained. The program is not code-signed yet: if SmartScreen
says "Windows protected your PC", choose **More info → Run anyway**.

**RTL-SDR driver:** Windows needs the WinUSB driver on the dongle, installed
once with [Zadig](https://zadig.akeo.ie/) (already done if SDR++ or SDR# works
with it): plug in the dongle → Zadig → *Options → List All Devices* → **"Bulk-In,
Interface (Interface 0)"** (not Interface 1) → **WinUSB** → *Replace Driver* →
replug. The same steps are in the app (Listen → SDR device). An SDRplay RSP
does not need Zadig - its API installer brings the driver.

FalconSDR runs in the background with its interface in the browser; quit it
in Settings → Advanced, where "Start with the system" is too (off by default).

### macOS - experimental

Download `FalconSDR-<version>-macos-arm64-experimental.zip` (Apple Silicon) or
`…-macos-x86_64-…` (Intel) from [Releases](../../releases), unzip it and move
**FalconSDR** to Applications. These builds pass the same self-test as the
others but haven't been tried on a real Mac with an SDR - feedback in
[Discussions](../../discussions) is very welcome.

The app isn't signed by Apple: the first time, try to open it, then System
Settings → Privacy & Security → **Open Anyway**. If macOS says it "is damaged":
`xattr -dr com.apple.quarantine /Applications/FalconSDR.app`.

### From source (Linux, Windows, macOS)

Requires Python 3.10+.

```
git clone https://github.com/rafafunt/FalconSDR.git
cd FalconSDR
python3 -m venv --system-site-packages .venv       # lets the venv see python3-soapysdr (Linux)
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
.venv/bin/python main.py                          # Windows: .venv\Scripts\python main.py
```

The interface opens at `http://127.0.0.1:8765`.
- **Linux:** for RTL-SDR access without root install the `rtl-sdr` package
  (udev rules). SoapySDR devices need `python3-soapysdr` and their
  `soapysdr-module-*` (hence `--system-site-packages`).
- **MP3** uses the ffmpeg from `imageio-ffmpeg` or a system `ffmpeg`; without
  either (32-bit ARM) recordings stay WAV.

### Updating and removing

FalconSDR tells you about a new version if you allowed it (*Settings →
Updates*): the Windows installer and the AppImage update with one click,
everything else gets the download or the command. By hand: quit FalconSDR and
install the new package over the old one - settings, channels, favorites and
recordings are kept. A source install updates with `git pull` (or the
`--from-source` line again), the headless receiver with its own line.

Remove with `sudo apt remove falconsdr`, `sudo dnf remove falconsdr` or
Windows *Settings → Apps*; recordings and settings stay on disk
([where](#where-things-are-stored)).

### Headless receiver

On a Linux machine with the SDR attached (Debian, Ubuntu, Raspberry Pi OS):

```
curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash -s -- --server
```

It installs the latest release as a service that starts on boot and prints
the address to open from any device on your network
(`http://<receiver-ip>:8765`). Run the same line again to update. HackRF and
Airspy work out of the box; SoapySDR hardware after `sudo apt install
python3-soapysdr soapysdr-module-<receiver>` and `sudo systemctl restart
falconsdr-receiver`. Helper computers for transcription and other details:
[packaging/remote-server/README.md](packaging/remote-server/README.md).

### Network receiver (rtl_tcp)

An RTL-SDR plugged into another computer (a Raspberry Pi at the antenna, a
phone running an `rtl_tcp` app) can feed the scanner: run `rtl_tcp -a 0.0.0.0`
there, then choose *SDR device → Network receiver (rtl_tcp)* on the Listen page
and type its address (`192.168.1.20` or `192.168.1.20:1234`).

- `rtl_tcp` serves **one program at a time** and is **not encrypted** - use
  it on a trusted network.
- 2.4 MHz of capture is about 38 Mbit/s: wired Ethernet is fine, Wi-Fi can be
  marginal.
- A retune takes longer over the network (~180 ms instead of ~100 ms on a
  wired LAN), so scanning many windows is slower.
- If `rtl_tcp` stops, FalconSDR shows *Reconnecting* and resumes once it is back.

## First steps

1. **Listen** page: pick your SDR, add a band (or a preset) and press Start.
2. Raise or lower the **trigger level** until real transmissions open and
   noise doesn't.
3. Switch on **recording** when you're happy with what it stops on.
4. Name the frequencies you hear on the **Channels** page (or straight from a
   recording) - names show up everywhere, including search.

## Tips for good recordings

- **Trigger level** decides what counts as a transmission: lower records
  weaker stations, and more noise.
- **Blacklist** frequencies that are just a carrier or interference (birdies
  from your own computer, a nearby transmitter's harmonics): Listen → *Skip +
  blacklist*. Afterwards you can usually lower the trigger by several dB.
- **Gain** defaults to *Auto (software)*; the tuner's own hardware AGC tends to
  overload on a strong nearby station (false detections on neighbouring
  channels).
- **Hold after a transmission** (5 s) keeps call and answer in one recording.
- Use the channel **step** the band really uses - a narrower one only adds work.
- *This computer can't keep up with the SDR* in the Activity feed means lost
  data: lower the capture width or move off a virtual machine (USB passed to a
  VM is a common cause). *Settings → Receiver health* shows the details.

## Command line

`falconsdr` for the installed packages (Windows: `falconsdr.exe` in its
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
falconsdr --probe-sdr   measure the receiver with the scanner stopped (add a driver: hackrf_native, sdrplay_native, rtl_tcp…)
```

## Where things are stored

| What | Where |
|---|---|
| Settings, channels, blacklist | `~/.config/FalconSDR/` |
| Recordings (default, changeable) | `~/FalconSDR/recordings/`, one folder per day |
| Quality scores, transcripts, favorites | `~/.local/share/FalconSDR/` |
| Add-ons: decoded messages, statistics, installed programs | `~/.config/FalconSDR/addons/` |

(`~` is your home folder, also on Windows; the Windows log is
`~/.config/FalconSDR/falconsdr.log`.)

## Troubleshooting

- **"Failed to connect… retrying"** - the SDR is missing, used by another
  program, lacks USB permissions (Linux: udev rule, replug) or has the wrong
  driver (Windows: Zadig for an RTL-SDR).
- **A receiver is plugged in but not listed** - the Listen page names what is
  missing: the SDRplay API, `libhackrf` / `libairspy`, or SoapySDR. If one way
  to open a receiver fails, try the other under *SDR device → Access method*.
- **Scanning freezes or the receiver keeps disconnecting** (seen with an
  RTL-SDR v3 on Windows) - switch *Settings → Advanced → Continuous RTL-SDR
  reading* off and start again.
- **Nothing gets recorded** - recording is off, the trigger level is too high,
  or the modulation is wrong (aviation is AM, most other VHF/UHF voice is NFM).
- **Constant recordings of noise** - blacklist the offending frequencies or
  raise the trigger level.
- **Distorted audio on strong stations** - gain to *Auto (software)* or lower.

## Feedback and contributing

- **Questions, ideas, your setup:** [Discussions](../../discussions)
- **Bugs:** *Settings → Receiver health → Report a problem* opens an
  [issue](../../issues) with the version, system and a receiver report filled
  in; add what happened and on which band.
- **Translations:** a new language is a copy of `falconsdr/locale/pl.po`
  (see `falconsdr/i18n.py`).
- **Code:** pull requests are welcome; `AGENTS.md` describes the architecture
  and the pitfalls already hit once. Tests: `python -m unittest tests/test_core.py tests/test_addons.py`.

## Legal

The laws on radio monitoring differ a lot between countries. In some places
you may listen to most bands but not record or share what you hear; in
others even listening to non-public services (aviation, police, emergency
services, military) is forbidden. Check what applies where you live before
you scan, and especially before you record or publish anything - you alone
are responsible for how you use this program.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)) - required by `pyrtlsdr` and the
bundled `librtlsdr`. The packages list every bundled component and its license
in `THIRD-PARTY-LICENSES.txt` (ffmpeg there is a GPLv3 build; `libhackrf` and
`libairspy` are BSD-3-Clause, `libusb` LGPL-2.1).

Not bundled, fetched only on the user's request from their publishers:
transcription (faster-whisper, CTranslate2 and the default model
`Systran/faster-whisper-large-v3`, all MIT; the optional English aviation model
`jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper`), the digital
voice decoder ([dsd-fme](https://github.com/lwvmobile/dsd-fme) and
[mbelib](https://github.com/lwvmobile/mbelib)), the add-on programs
(rtl_433, GPL-2.0; dumpvdl2, GPL-3.0, with libacars, MIT) and ETSI's TETRA
speech codec (downloaded from etsi.org and built on the user's computer; its
licence covers the code, not patents). The SDRplay API is installed by the
user from sdrplay.com and only loaded by FalconSDR. FalconSDR's own TETRA
decoder (`falconsdr-tetra`) is GPL-3.0-or-later and ships in every package.
