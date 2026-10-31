# FalconSDR — Windows build (installer + portable ZIP)

## What this builds

`build.ps1` freezes the app with [PyInstaller](https://pyinstaller.org/)
(`--onedir`: its own Python, numpy, scipy, pyrtlsdrlib with a statically
linked `librtlsdr`/libusb, tzdata and the web UI) and ships that one folder
two ways:

| File (`out\`) | For |
|---|---|
| `FalconSDR-<date>-windows-x64-setup.exe` | most users: Start menu entry, uninstaller, optional desktop icon |
| `FalconSDR-<date>-windows-x64-portable.zip` | unzip anywhere and run `FalconSDR\falconsdr.exe` — no install, no admin |
| `SHA256SUMS.txt` | checksums to publish next to both |
| `test-in-sandbox.wsb` | clean-machine test (below) |

Both contain exactly the same program. Settings and recordings live in the
user profile (`%USERPROFILE%\.config\FalconSDR`, `%USERPROFILE%\FalconSDR\recordings`),
so switching between the ZIP and the installer, upgrading or uninstalling
never touches them.

The installer (Inno Setup, `falconsdr.iss`):
- installs **per user** by default (`%LOCALAPPDATA%\Programs\FalconSDR`, no
  administrator prompt); "for all users" is offered on its first page;
- English/Polish, shows the GPL license and, after installing, the RTL-SDR
  driver (Zadig) instructions (`DRIVER-en.txt` / `DRIVER-pl.txt`) with an
  optional "open the Zadig website" checkbox on the last page;
- asks a running FalconSDR to quit through its own API before upgrading or
  uninstalling (open recordings get closed properly), Restart Manager as
  fallback;
- upgrades replace `_internal\` completely (no stale libraries); the
  uninstaller also removes the "Start with the system" login entry.

The exe is windowed (no console): the program runs in the background, its UI
is the browser, **Quit** is in Settings → Advanced, and its log goes to
`%USERPROFILE%\.config\FalconSDR\falconsdr.log`.

## Building

Needs: Windows 10/11 x64, Python 3.12 (`py -3.12`; another version with
`-Python 3.13`), and [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the
installer (`winget install JRSoftware.InnoSetup`; `-SkipInstaller` builds the
ZIP only).

```
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

It always starts from a throwaway venv (`build-venv\`, never the project's
`.venv`) and then **tests the built exe** with `PATH` reduced to
`System32` and an empty user profile — so a Python, an SDR program's
`librtlsdr.dll` or this repo's own files on this machine can't make a broken
bundle look fine:

1. `falconsdr.exe --self-test` (`falconsdr/selftest.py`): numpy/scipy,
   time zones, librtlsdr, UI files, translations, band presets, a demod.
2. a smoke run: the UI, a script, `/api/settings` and the Polish catalog are
   served, then `/api/quit` must end the process cleanly.

Any failure stops the build — nothing is packaged.

## Testing on a clean machine (Windows Sandbox)

Windows Sandbox is a disposable Windows with nothing installed — the honest
"someone else's PC" test. Enable it once (Windows 10/11 Pro, administrator
PowerShell, then restart):

```
Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All
```

Then double-click `out\test-in-sandbox.wsb`: the sandbox opens with the
`out\` folder on its desktop (read-only). Run the setup (or unzip the ZIP to
the sandbox desktop), start FalconSDR, and the UI opens in the sandbox's
Edge. USB devices can't be passed into the sandbox, so test the scanner
there with Settings → Advanced → Debug mode → device "Simulation"; test the
real dongle (and the Zadig hint on the Listen page, shown while the scanner
can't open the dongle) on a normal Windows install.

## MP3 and transcription in the packaged build

- **MP3** works out of the box: the bundle carries the ffmpeg from the
  `imageio-ffmpeg` package (a system `ffmpeg` on PATH is preferred if present).
- **Transcription** installs from the app like in a source install
  (Settings → Transcription & computers → Install). The exe has pip inside:
  `falconsdr.exe --pip install ...` puts prebuilt wheels (faster-whisper,
  ctranslate2, PyAV...) into `%USERPROFILE%\.local\share\FalconSDR\addons\cp312`,
  which the app loads without a restart; models download on first use.
  Verified end to end in a clean profile with a stripped PATH (install from
  the UI, then a speech WAV transcribed). Two bundle details this needs, both
  checked by the build: pip's script-launcher templates (`main.py --pip`) and
  `python3.dll` for abi3 wheels like PyAV. The add-ons folder belongs to the
  bundled Python version (`cp312`); a build on another Python version starts
  a new one. GPU (CUDA) libraries install the same way but are untested here.

## Not included

SoapySDR (so SDRplay, LimeSDR, USRP…). HackRF and Airspy are included natively and experimental: `libhackrf` and
`libairspy` (with `libusb`, `libwinpthread`) are fetched at build time by `fetch_native_libs.py` - pinned conda-forge
binaries, checked by SHA-256 - and loaded by `--self-test`. The exe is not code-signed, so
SmartScreen shows "Windows protected your PC" on first run → **More info →
Run anyway**.
