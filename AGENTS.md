# AGENTS.md — FalconSDR

Guidance for AI coding agents working in this repository.

## What this is

FalconSDR scans a radio band (default: airband ATC 118–136 MHz) via
SoapySDR (Linux) or a native librtlsdr backend (Windows/macOS — gotcha 16)
and records audio **only** for transmissions the scanner actually stops on —
never the retuning noise. `falconsdr/scanner_engine.py`'s state machine is
the authoritative spec.

The UI is a **web app** served by the program itself (Python standard
library HTTP server + vanilla JS/CSS, no framework, no build step). One
program, three modes (`main.py`):

- **local** (default) — this computer scans; UI on `http://127.0.0.1:8765`,
  opened in the browser automatically. A second launch just reopens it.
- **server** (`--server`, systemd `falconsdr-receiver.service`) — same,
  reachable from the whole network (phones too). `sdr-mode` (the owner's
  mode switcher on the receiver) starts/stops exactly this unit — keep the
  unit name and its clean SIGTERM shutdown (gotcha 41).
- **client** — `AppConfig.remote_receiver.enabled`: this computer doesn't
  scan; it runs a `Worker` for a server (local Whisper transcription and/or
  a verified local copy of the recordings) and opens the server's UI.

## Running it

```
python3 main.py                 # local; --server for the network; --local ignores a saved server
python3 -m unittest tests/test_core.py
```

`numpy`, `scipy`, `pyrtlsdr`/`pyrtlsdrlib`, `tzdata` —
`pip install -r requirements.txt`. `SoapySDR` needs system packages on Linux
(see `requirements.txt`). No SDR hardware/library at all, with
`AppConfig.debug_mode` on → `SimulatedBackend` fallback rather than failing
to start; with debug off (the default), a real connection failure stays
visible (retry/backoff state) instead of silently simulating — gotcha 8.

**License: GPL-3.0-or-later** (`LICENSE`) — forced by `pyrtlsdr` (GPL-3.0-or-later)
and the bundled `librtlsdr` (GPL-2.0-or-later); every other dependency is
permissive. A new dependency must be GPLv3-compatible.

**i18n**: Python strings go through `tr()`, browser strings through `t()`
(`static/js/core.js`) — both use the English text as the key into the same
`.po` files under `falconsdr/locale/` (hand-rolled reader, no `gettext`/`.mo`;
`GET /api/i18n` hands the browser its catalog — "auto" language = the
browser's). Every new UI string needs a `pl.po` entry. **Never build a
module-level constant from `tr()`/`t()` calls** — they'd freeze before the
catalog loads; wrap them in a function called at point of use
(`military_presets.py`'s labels are plain English, translated in the UI).

## Architecture

- `main.py` — CLI/modes, single-instance check, clean shutdown on
  SIGTERM/SIGINT, mode switches (gotcha 41).
- `falconsdr/web/server.py` — `ThreadingHTTPServer` + JSON API + live-audio
  stream + recording files (HTTP Range) + worker protocol; auth/CSRF in
  `_route` (gotcha 38). `web/static/` — `index.html`, `app.css`,
  `js/core.js` (DOM/i18n/dialogs), `js/state.js` (settings, live polling,
  legal notices), `js/live-audio.js` (gotcha 40), `js/views/*.js` (Listen,
  Recordings, Channels, Statistics, Settings).
- `falconsdr/controller.py` — `Controller`: owns `AppConfig`, Memory List,
  blacklist, the one `ScannerEngine`, its live snapshot, audio fan-out and
  the `Library`; config edits arrive as JSON patches (`merge_patch`).
- `falconsdr/playback/index.py` — `Library`: in-memory index of closed
  recordings + quality/transcript/favorite/mirrored state, queries, facets,
  stats, retention, and the server side of the worker protocol (gotcha 37).
- `falconsdr/worker.py` — `Worker`: transcription jobs (leased), mirroring,
  one-shot upload/import of a client's own library, install command.
- `falconsdr/models.py` — `ScannerState`, `Channel`, `BlacklistEntry`.
- `falconsdr/config.py` — `AppConfig`, persisted (atomically) to
  `~/.config/FalconSDR/config.json`; unknown keys ignored on load.
  `device.sample_rate_hz` is the wideband **capture bandwidth**;
  `memory_channel_bandwidth_hz` is the per-channel power-window width for
  Memory List channels (a band uses its own `step_hz` instead).
- `falconsdr/storage.py` — CSV read/write for `channels.csv`/`blacklist.csv`.
- `falconsdr/schedule.py` — time-of-day schedule (`ScheduleConfig`): which
  bands and whether the Memory List are scanned now. Overlapping entries
  scan the union; between entries `outside` = "off" (nothing) or "normal"
  (the bands' own switches). An entry crossing midnight belongs to its start
  day. Entries refer to bands by `VfoRangeConfig.id` - the browser creates
  the id (`views/bands.js` `newBand`), so it is stable before any reload.
  `ScannerEngine._maybe_follow_schedule()` rebuilds the windows when the
  active set changes (checked every 5 s).
- `falconsdr/channelizer.py` — `plan_windows()` groups channel frequencies
  into the fewest capture windows; `channel_powers_dbfs()`/
  `channel_powers_from_spectrum()` do one FFT per block and sum power per
  channel's bins; `spectrum_snapshot()` feeds the UI spectrum;
  `extend_boundary_window()` probes just past a *swept band's* edge (gotcha
  31/32 — never a single point).
- `falconsdr/dsp.py` — `Demodulator` (AM/NFM/WFM/SSB → mono audio, gotcha
  7/25) and `NoiseReducer` (spectral-gate, minimum-statistics noise floor).
- `falconsdr/sdr_backend.py` — `SoapyBackend`, `RtlSdrBackend` (gotcha 16),
  `SimulatedBackend`, `AcquisitionThread` (gotcha 11 — this is what
  `ScannerEngine._backend` actually is). `open_real_backend()`/
  `create_backend()` pick/fall back (gotcha 8/9/10).
- `falconsdr/agc.py` — `SoftwareAgc`, software-driven RF gain control
  (gotcha 27).
- `falconsdr/recorder.py` — WAV writer only (gotcha 14: mp3 is a post-close
  transcode, not a `Recorder` concern). `single_file` flag: gotcha 13/23.
- `falconsdr/scanner_engine.py` — `ScannerEngine(threading.Thread)`:
  `CALCULATE_NEXT_FREQ → TUNING → SCANNING → SIGNAL_DETECTED ⇄ LINGER`.
  Retunes to a whole capture window, not a single channel; `SCANNING` picks
  the strongest non-blacklisted candidate per tick, `_service_active_slots()`
  advances every already-open slot (gotcha 12/28). Reports through
  `ScannerSignals` (plain callbacks — gotcha 36).

## Known gotchas (hit these once already — don't reintroduce)

1. **(Retired with the Qt UI.)** The web UI loads settings once per view
   and writes only what a control changes (`state.js`'s `patchConfig`) — no
   half-initialized widget state can reach disk.

2. **(Retired with the Qt UI.)**

3. **`SimulatedBackend.read()` must sleep for `num_samples/sample_rate`** or
   the engine spins the SCANNING dwell thousands of times/sec. Burst timing
   is a deterministic function of wall-clock time + a frequency hash, not
   persisted state.
4. **`SoapyBackend.enumerate_devices()` must filter out `driver == "audio"`**
   — `soapysdr-module-audio` enumerates sound cards as SDR receivers.
5. **ATC airband is AM, not FM** — `Modulation.AM` is the correct default
   for 118–136 MHz.
6. **Squelch (`config.squelch_enabled`/`squelch_offset_db`) must always be an
   offset from `trigger_level_dbfs` (clamped ≤ 0), never an independent
   threshold** — `_effective_squelch_dbfs()` = trigger + offset (a band's
   own `squelch_enabled`/`squelch_offset_db` win over the global ones), so it can
   never fire during SIGNAL_DETECTED, only in the LINGER tail. `trigger_level_dbfs`
   alone still drives SIGNAL_DETECTED/LINGER. A squelched block writes
   *silence* to the recording (`Recorder.write_audio(audio, real=False)`,
   held in `_pending_silence`) rather than being skipped — flushed if real
   audio follows (genuine mid-transmission pause), discarded on close if not
   (trailing tail trimmed). `analyze_wav()` excludes exact-zero-RMS windows
   from its noise-floor/SNR calc for this reason. Quality-score caches
   (`QualityCache`/`TranscriptionCache`, keyed by path+size+mtime) don't
   auto-invalidate when scoring logic changes — invalidate them by hand.
7. **In `Demodulator.demod()`, mix-to-baseband → decimate → AM/FM-demod, in
   that order** — `resample_poly`'s anti-alias filter is what rejects
   neighbouring channels in a wideband capture.
8. **`SoapyBackend.read()` must raise on a fully empty read** (a dead/
   unplugged device just returns nothing, never raises on its own), **and
   `create_backend()` only ever auto-falls-back to `SimulatedBackend` for
   `driver in ("", "auto")` AND only when `debug_mode` is on** (its new
   `debug_mode` param) — a real open failure with debug off propagates
   instead (the existing `_open_backend_with_retry()` reconnect/backoff
   loop then shows it as a visible "Failed to connect... retrying" state).
   Real, reported case: a real device that merely failed to open (USB
   permission, already in use, DVB-T driver grabbed it first) looked
   identical to "no hardware at all" and silently simulated instead -
   indistinguishable from a genuinely working scan. An explicitly-picked
   real driver that fails must always propagate the error regardless of
   debug_mode, never quietly simulate.
9. **`SimulatedBackend` must inject its fake signal at its offset from the
   *currently tuned* frequency** (`abs(active_freq - freq_hz) <= half_bw`),
   not only on an exact match — the tuner sits on a window center, nudged
   off exact channel frequencies.
10. **Auto-reconnect is deliberate.** `ScannerEngine.run()` retries a backend
    `RuntimeError` via `_open_backend_with_retry()` (backoff 2/5/10/20/30s,
    `RECONNECTING` state) instead of stopping. Once real hardware has been
    proven present (`_had_real_hardware`), reconnects call the real backend
    directly, bypassing `create_backend()`'s auto-simulate fallback (gotcha
    8). Any open recording is closed via `_end_transmission()` first.
11. **`ScannerEngine._backend` is an `AcquisitionThread`, not the raw
    backend — never call `backend.read()` from the main loop thread.** It
    reads back-to-back on its own SCHED_FIFO thread into a bounded FIFO
    queue (`max_queued`) so slow consumer work can never delay the actual
    hardware read (which otherwise silently drops/splices samples at the
    driver level). **Single-owner**: the only thread that ever touches the
    real backend, including `set_frequency()`/`set_gain()` — those hand a
    command via `_pending_command`/`_cond` instead of touching the device
    from the caller's thread. Never share a lock between this thread and
    any other — a lock shared with this SCHED_FIFO thread is a real-time
    priority-inversion hazard (measured: 11.7s starvation of a normal-
    priority caller).
12. **`_service_active_slots()` demodulates every active slot via
    `self._slot_pool` (`ThreadPoolExecutor`)** — sequential per-slot demod/
    noise-reduction/write blows the per-tick budget with several
    simultaneous slots, backing up `AcquisitionThread`'s queue. Each
    `_Slot` owns its own `Demodulator`/`NoiseReducer`/`Recorder` — no shared
    mutable state between pooled tasks. LINGER/slot-close bookkeeping
    happens on the main thread after every future resolves, never inside
    the pooled task (`_close_slot()` mutates `self._slots`).
13. **Manual "Record" is a second, independent, continuous `Recorder`**
    (`self._manual_recorder`, `single_file=True`), unrelated to the trigger
    state machine and to `config.recording.enabled` (auto-recording). Fed
    real audio only when the primary slot is active
    (`slot.freq_hz == self._primary_freq`) and literal zeros on idle ticks —
    continuous including silence. Never dropped as a "phantom" (always
    `write_audio(real=True)`). Never persisted — off again on next launch.
    A recording that captures a real frequency gets renamed from
    `..._session` to a frequency-tagged name
    (`Recorder.rename_session_file_with_frequency()`, first-wins latch in
    `_manual_recording_first_freq`).
14. **MP3 (`RecordingConfig.output_format == "mp3"`) is a transcode run
    strictly after a WAV is closed AND its metadata chunk written**
    (`append_metadata_chunk()` first, transcode second — an mp3 container
    can't hold the RIFF chunk). `Recorder` is WAV-only and knows nothing
    about `output_format`; both close sites (`_close_slot`,
    `stop_manual_recording`) hand the file to `_finish_recording()`, which
    runs ffmpeg on `self._slot_pool` — never on the scan-loop thread (a slow
    transcode outlasted `AcquisitionThread`'s ~1.9s queue and dropped IQ for
    other open slots) — and emits `recording_finished` only afterwards.
    Missing `ffmpeg` silently keeps the WAV.
15. **Raw IQ recording (`iq_recorder.py`) is a third, independent
    mechanism** — not audio, not `Recorder`-based. Writes SigMF from the
    *shared* window capture (never a second hardware read). Manual/
    momentary only (uncompressed IQ ≈ 19 MB/s at 2.4 MSPS). Doesn't go
    through `recording_finished` (that feeds the recording library, which
    expects WAV/MP3) — separate `raw_iq_recording_changed` signal, files in
    a `raw_iq/` subfolder, excluded from the audio library.
16. **Windows/macOS get real RTL-SDR via `RtlSdrBackend`** (native
    librtlsdr/`pyrtlsdrlib` — SoapySDR has no pip wheel there). Matches
    `SoapyBackend`'s contract: bias-tee as configured (off by default - gotcha 47), raises instead of short-reads,
    same CF32 scaling `(i - 127.4) / 128` (so a threshold means the same
    dBFS cross-platform). `open_real_backend()` picks Soapy vs native based
    on driver string/what's installed; `"auto"` tries Soapy then native,
    only simulates if both fail. **`SoapyBackend.__init__` must turn `driver
    in ("", "auto")` into an explicit `{"driver": "rtlsdr"}` args filter,
    never pass `{}` to `SoapySDR.Device()`** — a real, measured bug, not just
    the theoretical "could match the `audio` driver" risk (gotcha 4) it was
    first flagged as: on a real dev machine, opening the *same* physical
    RTL-SDR dongle via unfiltered `{}` vs explicit `{"driver": "rtlsdr"}`
    produced **~10dB different channel power** for the identical gain_mode
    ("auto"/hardware AGC) - one path was overdriving (peak +1dBFS), the
    other wasn't (peak -9dBFS). Confirmed by direct A/B measurement, fixed
    by always filtering explicitly - `driver="auto"` only ever means "let
    the app pick an RTL-SDR, don't ask the user" here anyway (gotcha 16/35:
    this app's device list is RTL-SDR-first), so there's no real ambiguity
    being preserved by leaving the filter open.
17. **Other SoapySDR hardware (HackRF/LimeSDR/Airspy/SDRplay/USRP) already
    works via `SoapyBackend`** with no code changes once the matching
    `soapysdr-module-*` is installed — nothing in it is RTL-SDR-specific.
    The UI's capture-width field accepts `0.25–61.44` MHz (not a hardware limit,
    just a generous bound — raise it if a real device needs more). Windows/
    macOS stay RTL-SDR-only for the zero-install path; conda-forge SoapySDR
    is a documented, unautomated path for other hardware there.
18. **Transcription (faster-whisper) is an in-app-installable optional
    extra** (packaged builds: see gotcha 43), not in `requirements.txt`, and runs in a `Worker` (gotcha 37) on
    whichever computer has it switched on — never assume the scanning
    machine can run Whisper. `is_transcription_installed()`
    (`transcription_availability.py` — `importlib.invalidate_caches()` then
    `find_spec`) is the one source of truth; don't grow a second ad-hoc
    `import faster_whisper` try/except elsewhere. `install_transcription()`
    tries a plain `pip install` into the running interpreter first; only
    falls back to a `--system-site-packages` venv (`venv_paths.VENV_DIR`) on
    a PEP-668 `externally-managed-environment` error — that fallback is the
    only path needing a restart. A worker without Whisper never claims
    jobs, and an `"error"` result isn't re-queued automatically within the
    same server session (`Library._failed`), or a broken install would
    retry forever.

19. **A frequency-less filename (`..._session.wav` — manual recording,
    gotcha 13) must be recognized by `FILENAME_REGEX`/`parse_filename()`**
    (`playback/config.py`/`organizer.py`), mapped to `models.MANUAL_FREQ_KHZ`
    (0) rather than `None` — otherwise `organize()`/`scan_recordings()`
    silently skip the file forever (a real, playable recording invisible in
    Playback). Every freq-label display site needs its own
    `MANUAL_FREQ_KHZ` case ("Manual recording", not "0.000 MHz").
    `FILENAME_REGEX` is the single source of truth for every consumer.
    MP3 files need the same regex to also match `\.mp3$`, and the stdlib
    `wave` module can't decode mp3 at all — `audio_analysis.py` decodes via
    ffmpeg to an in-memory WAV first (`_mp3_to_wav_bytes()`). Duration must
    come from actual bytes read, not the WAV header's declared frame count —
    ffmpeg's non-seekable-pipe muxer writes a sentinel `0x7FFFFFFF` there.
20. **Legal disclaimers are tracked per UI language**
    (`AppConfig.legal_disclaimer`'s two seen-language lists), not once-ever
    — a language switch means seeing them again, by design. In the browser
    (`state.js`): `maybeShowLaunchNotice()` is informational, once per
    language at startup; `confirmRecording()` is a real gate (Continue/
    Cancel) shared by *every* recording-starting action (auto-recording on
    Listen and in Settings, manual Record, raw IQ) — a decline must revert
    the triggering toggle/button and have zero side effects.

21. **Band presets (`military_presets.py`) are JSON, not a hardcoded list**:
    bundled `falconsdr/presets/band_presets.json` + user's
    `CONFIG_DIR/band_presets.json` (edits/additions/hidden-builtin-ids),
    merged live via `get_all_presets()` — **never cache this** (same class
    of bug as the i18n module-constant trap). Editing a built-in shadows it
    (same id saved into the user file); deleting a pure built-in adds it to
    `hidden_builtin_ids`; the shipped JSON is never modified — the two cases
    take different code paths since only a shadowed one has a real user-list
    entry to remove. Import never overwrites on id collision (generates a
    new id). `upsert_preset()`/`delete_preset()`/`import_presets()` hold
    these rules — the UI only calls the API.
22. **"Reset to defaults" (`Controller.reset_to_defaults`) keeps the
    recordings folder, the access password, connected computers and the
    legal-notice state** — resetting `output_dir` would orphan the library
    from the UI, resetting the password could lock the user out of a
    headless server. Never touches the recordings themselves.

23. **"Single File" auto-recording mode was removed, not fixed** — the
    per-slot fresh-`Recorder`-per-transmission design (gotcha 12) meant it
    never produced one continuous file. `Recorder` has a real `single_file:
    bool` constructor flag now, used only by manual recording (gotcha 13),
    the one legitimate "one continuous file" case. `AppConfig.load()` pops
    the stale `mode`/`insert_marker` keys for backward compat.
24. **`RecordingConfig.max_concurrent_recordings`** (default 3, 0 = unlimited)
    caps simultaneously-recorded transmissions — checked in
    `ScannerEngine._at_recording_capacity()`, gating `_open_slot()`. Only
    gates auto-recording (`recording.enabled` must be `True`); never caps
    live scanning/listening, and doesn't affect manual/raw-IQ recording
    (separate mechanisms outside `self._slots`). A capped candidate isn't
    lost — it's simply re-evaluated next tick, opening automatically once a
    LINGER expiry frees capacity. Both silent ways a detected channel can end
    up with no file are logged to the Activity feed: capacity skips (once
    per channel per 30 s) and a slot discarded for having nothing above the
    squelch level. The feed drops consecutive duplicate lines
    (`Controller.log`) - the per-rebuild "wideband scanning" line once
    flooded every real event out of its 100-entry buffer.
25. **`Demodulator`'s output-level AGC (`dsp.py`, `_apply_agc`) is a
    persistent attack-fast/release-slow envelope follower across `demod()`
    calls** (state in `self._agc_envelope`, cleared by `reset()`) — never a
    per-chunk peak normalization computed from that chunk alone. The old
    per-chunk version dragged noise-only chunks up to the same loudness as
    voice chunks whenever their own peak cleared the floor, destroying real
    SNR before `analyze_wav()` ever measured it.
26. **(Retired.)** Live audio no longer plays through PortAudio on the
   machine running the scanner — it streams to the browser (gotcha 40).

27. **Three gain modes (`DeviceConfig.gain_mode`): `"manual"`, `"auto"` =
    the tuner's own hardware AGC (passed straight to `backend.set_gain`),
    `"sw_auto"` = `SoftwareAgc` (`falconsdr/agc.py`, the default).**
    Hardware AGC can overdrive the ADC (measured +0.7/+1 dBFS peaks, extra
    spurious neighbour detections), so `"sw_auto"` is the default: `_maybe_apply_gain()`/`_maybe_run_software_agc()` send explicit
    `"manual"` values computed from each tick's IQ peak (target ≈ −6 dBFS,
    ±3 dB dead zone, ≥1s between steps) and never touch the hardware AGC.
    Each step pre-scales every slot's output AGC
    (`Demodulator.notify_rf_gain_change`) and clears its `level_history` —
    a gain step moves all channels' dB levels together, which
    `_detect_reflections()` would otherwise read as correlation and discard
    a real signal (rejected files are renamed `*.rejected`, never deleted).
28. **`_service_active_slots()` processes the primary slot (the only one
    that reaches the live speaker) synchronously, first** — before any
    non-primary slot is submitted to `self._slot_pool`. The old design gave
    primary no priority over recording-only slots in the same pool batch,
    so more simultaneously-recording slots directly degraded live audio.
    Non-primary slots are still parallelized among themselves as before.
    Related UI lesson: don't reflexively disable a live-tick-read setting
    while scanning — only lock a control during a scan if a live change
    could actually corrupt something (gotcha 33).
29. **The acquisition thread's real-time-priority boost (gotcha 11)
    prints its outcome** (`OK`/`NOT granted`, in debug mode — gotcha 32) —
    check this line first for any scan-speed report; a silently-ungranted
    boost is otherwise indistinguishable from "granted but didn't help".

30. **The live spectrum must stay rate-limited and cheap, or it can cause
    real dropped IQ chunks.** Submitting a frame every SCANNING tick (~30/s)
    with heavy work attached once overflowed `AcquisitionThread`'s bounded
    queue for real (a recording can then run measurably short of real
    elapsed time). `ScannerEngine._submit_spectrum()` throttles to
    `_SPECTRUM_FPS` (10 Hz) on its own `_SpectrumWorker`; the controller
    only downsamples to ≤600 points and stores the latest frame; browsers
    poll it (4/s on the Listen page). `AcquisitionThread` also has always-on
    health counters (`chunks_read`/`chunks_DROPPED`/`slow_reads`/
    `read_max_s`, printed every `_HEALTH_REPORT_INTERVAL_S` in debug mode) —
    `chunks_DROPPED` is the only place that can distinguish genuine data
    loss from mere timing jitter.

31. **`_extend_edges()`/`extend_boundary_window()` (probe past a *swept
    band's* true edge) must never fire for a frequency that isn't part of a
    genuine multi-point band grid.** A standalone Memory List point channel
    or a degenerate "band" with `start_hz == stop_hz` (exactly what the
    Listen page's "Single frequency" button creates) each form a one-frequency window whose "edge" is that same Hz
    on both sides — the old code couldn't tell that apart from a real band
    edge and fabricated up to ~28 fake candidate frequencies per side,
    which then get treated as real simultaneous detections/recordings.
    Fixed via a `band_freqs` set (only frequencies from a `VfoRangeConfig`
    with `start_hz != stop_hz`) threaded through every `_extend_edges()`
    call site in `_rebuild_windows()` — the default-lane pool AND the
    weighted-band priority lane (which had no filter at all before). A real
    multi-point band still extends normally. **Any new code path that
    builds windows from a `VfoRangeConfig`'s own grid must apply this same
    `start_hz != stop_hz` check before ever calling `_extend_edges()`.**

32. **`AppConfig.debug_mode` (default off) gates the "Simulation" entry in
    the SDR device list** (`Controller.devices()`) — real users shouldn't be
    able to pick it by accident instead of their actual hardware. Also
    gates the RT-priority outcome print (gotcha 29) and `AcquisitionThread`'s
    periodic health summary (gotcha 30) — `debug=self.config.debug_mode`
    passed in at construction in `ScannerEngine._open_backend()`. The actual
    `sched_setscheduler()` call always still happens regardless; only the
    `print()`s are conditional.

33. **The SDR device and capture width are locked while the scanner is
    running** (Listen page `lockDevice()`) — unlike most settings, changing
    SDR hardware or capture bandwidth genuinely requires closing and
    reopening the device stream, which isn't supported live. Don't unlock
    these; if this confuses users again, improve the hint, not the lock.

34. **`compute_waveform_peaks()` (recording waveform, `/api/recording/peaks`) normalizes so
    the recording's own loudest bucket reaches full bar height**, not its
    raw fraction of digital full scale — a weak/distant station's real
    peaks can sit at 5-15% of full scale even where speech is clearly
    audible, which drew a waveform too flat to tell speech from silence at
    a glance. Only the *drawn* peaks are rescaled; playback level and
    `analyze_wav()`'s SNR scoring (already relative to the file's own
    content) are untouched. Guards divide-by-zero for a genuinely all-
    silent file (stays all-zero).
35. **`packaging/linux-deb/build.sh`** builds a self-contained PyInstaller
    `--onedir` bundle wrapped in a .deb (RTL-SDR only - no SoapySDR - plus
    `falconsdr/web/static` as data; see its own header comment and
    README.md for the full rationale/known limitations). Several real,
    clean-container-verified gotchas any other frozen build (Windows .exe,
    AppImage) will also hit - the first two are the same underlying
    failure mode (a native library loaded dynamically at *runtime*, not
    linked at import time, so PyInstaller's static analysis can't see it
    needs bundling) with three different fix shapes depending on how each
    library does its own loading:
    - **`tzdata` needs `--collect-data tzdata` explicitly** - it ships only
      data files, so PyInstaller's static import analysis never sees
      `zoneinfo`'s own runtime fallback to it and silently drops it,
      reopening the exact `ZoneInfoNotFoundError` this package was added to
      requirements.txt to prevent in the first place.
    - **`pyrtlsdrlib` needs `--collect-data pyrtlsdrlib` explicitly** -
      `pyrtlsdrlib.lib.load_librtlsdr()` locates its own bundled
      `librtlsdr.so*` via `importlib.resources.files()` at runtime, which
      this fix makes visible to the bundle at its correct package-relative
      path. Without it, the build can appear to work on a dev machine that
      happens to also have a *system* `rtl-sdr` package installed (pyrtlsdr's
      own fallback chain silently finds that instead) while failing on a
      genuinely clean machine with neither - `HAVE_RTLSDR_NATIVE` ends up
      False there, and the app falls through to the SoapySDR path this build
      deliberately doesn't ship, surfacing as "the python3-soapysdr module is
      not installed" instead of ever finding the real dongle.
    - **`main.py`'s venv re-exec check and `transcription_install.py`'s
      installer both gate on `getattr(sys, "frozen", False)`** - a frozen
      build's own `sys.executable` *is* the app itself, not a real Python;
      calling `[sys.executable, "-m", "pip", ...]` would try to re-launch
      the whole app a second time instead of running pip. The transcription
      installer fails with a clear message in a frozen build instead. Any
      future code path that shells out to `sys.executable` expecting a real
      interpreter needs the same guard. **That message must go through
      `on_line`** - the worker's log (shown per computer in Settings) is the
      only place the user sees why an install failed. Likewise `_restart()`
      in `main.py` re-execs `sys.executable` directly when frozen.
    - **A fresh install has no udev rule granting a plain user access to an
      RTL-SDR dongle's USB device, and nothing stops the kernel's own DVB-T
      driver from claiming it first** - the `rtl-sdr` apt package normally
      provides both, but this self-contained build intentionally depends on
      no system package. Real, reported symptom: the device "isn't
      detected" and the app silently fell back to `Simulated` (its "auto"
      driver mode did that on *any* open failure, including a plain
      permission error) - indistinguishable from a genuine hardware
      problem. Gotcha 8's `debug_mode` gate (added later) means a non-debug
      install now surfaces this as a visible "Failed to connect... retrying"
      state instead, but the real udev/DVB-T fix below is still needed so
      it connects at all. Fixed by shipping `60-rtl-sdr.rules` (udev,
      0bda:2832/2838 → group `plugdev`) and `blacklist-rtl-dvb.conf`
      (modprobe blacklist for `dvb_usb_rtl28xxu`/`rtl2832`/`rtl2830`) via
      `postinst`. **Every `udevadm`/`rmmod` call in `postinst` is wrapped in
      `timeout`** - `udevadm control --reload-rules` can hang indefinitely
      (not just fail) with no running udev daemon (confirmed hanging the
      whole `apt install` in a bare Docker container) - a postinst must
      never be able to block an install.
    - **Build the published .deb in an old distro**
      (`packaging/linux-deb/build-in-docker.sh`, Debian 11): a bundle built
      on a new glibc half-works on older systems - the app starts, then a
      lazily imported module (`termios`, hit by deleting Whisper models)
      fails with "GLIBC_2.42 not found".
    - **A system program started from the frozen Linux app must not inherit
      its `LD_LIBRARY_PATH`** (PyInstaller points it at the bundle) - a
      system ffmpeg loaded the bundle's libraries and failed every MP3.
      `audio_transcode.system_env()` restores the user's value; use it for
      any new subprocess of a system binary.
    - **`multiprocessing.freeze_support()` runs first in `main.py` when
      frozen** - Whisper's libraries start multiprocessing's resource
      tracker, which re-runs the app's own executable with `-c ...`.

36. **Engine signals are plain synchronous callbacks** (`scanner_engine.Signal`,
    no Qt): handlers run on whichever engine thread emits (main loop, slot
    pool, spectrum worker). Every handler must be thread-safe and quick —
    take a lock, store a value, return (`Controller._set_live`). A handler
    exception is printed, never allowed to kill the engine thread.
37. **The library only ever lists *closed* recordings.** `Library.rescan()`
    skips `ScannerEngine.open_recording_paths()` (open slots + manual
    recording), and `transcode_to_mp3()` writes `*.mp3.part` and renames —
    a listed file may be downloaded, mirrored, transcribed or deleted, which
    must never happen mid-write (real, earlier bug: a sync deleted a
    recording still being written). New files join via
    `recording_finished` → `Library.add_file`. Worker protocol: jobs are
    leased (`_LEASE_S`) so two computers never transcribe the same file; a
    result whose `key` (path|size|mtime) no longer matches is dropped; a
    copy counts as mirrored only after a size check; imported metadata
    (`import_metadata`) only fills gaps, never overwrites.
38. **Access: the password is optional and off by default** (owner's
    explicit choice — it's a home-LAN tool). When set (`/api/access`), every
    `/api/` call except session/login/i18n needs the HttpOnly cookie or
    `Authorization: Bearer`. Independently, every POST/PUT must carry
    `X-FalconSDR: 1` (a non-simple header → CORS preflight this server never
    answers → other websites can't trigger actions), and local mode with no
    password refuses non-loopback `Host` headers (DNS rebinding). Keep both
    when adding endpoints; `api()` in `core.js` and `worker.Api` add the
    header.
39. **Settings are patched, not re-sent whole** (`state.js` `patchConfig` →
    `POST /api/config` → `merge_patch`, which coerces to the field's current
    type); only `bands` goes as a whole list. `access_token` and `workers`
    are never patchable — they have their own endpoints.
40. **Live audio in the browser = one long `fetch()` of raw PCM16 +
    back-to-back `AudioBufferSourceNode`s** (`live-audio.js`, same approach
    as the owner's SW listener). Not `AudioWorklet`: that needs a secure
    context (HTTPS/localhost), and the UI is reached by plain `http://IP`.
    Idle streams get 10 ms of silence every 5 s (keeps proxies/sockets
    alive and lets the server notice dead listeners).
41. **Mode switches and stops shut down cleanly first.** SIGTERM/SIGINT,
    the UI's Quit, connect/disconnect all set `stop` in `main.py`, which
    then runs `controller.shutdown()` (closes every open recording) before
    exiting or re-exec'ing — `sdr-mode` stops the receiver unit on every
    mode switch, and an `os.execv()` straight from a request handler would
    leave truncated recordings.

42. **UI design follows the `apple-design` skill's HIG rules** (owner's
    choice): appearance follows the system light/dark setting only — no
    in-app theme switch (`dark-mode.md`: "Avoid offering an app-specific
    appearance setting"); colors are the semantic tokens in `app.css`
    (contrast figures in its header — recompute when changing one); touch
    targets ≥44 px under `@media (pointer: coarse)`, kept **last** in the
    file or component rules override it; icon-only buttons get their
    `title` as `aria-label` (`core.js` `h()`). The amber COM-style
    frequency display + spectrum ("instruments", always dark) is the one
    signature element — keep the rest quiet. **Never give `.topbar` a
    `backdrop-filter` on phones**: it becomes the containing block of the
    `position: fixed` tab bar inside it, which then renders at the top.

43. **Windows release = `packaging/windows/build.ps1`** (PyInstaller
    `--onedir --noconsole` → portable ZIP + Inno Setup installer,
    `falconsdr.iss`; see its README). Same `--collect-data` rules as gotcha
    35. Every build runs `main.py --self-test` (`falconsdr/selftest.py`) and
    a smoke run on the *built* exe with `PATH` = System32 and an empty
    profile - a dependency the bundle lacks must fail the build, never a
    user's first launch (`build.sh` runs the self-test too). Add a check to
    `selftest.py` for any new runtime-loaded dependency. A windowed exe has
    `sys.stdout`/`stderr` = None - `main.py` points them at
    `CONFIG_DIR/falconsdr.log`. The installer never touches the user's
    profile; its `AppId` must never change (upgrades/uninstall find the
    install by it). **Packaged builds install transcription too**: `main.py
    --pip` runs the bundled pip (`--collect-all pip`, plus a distlib finder
    registration for its launcher templates) into `ADDONS_DIR`
    (`transcription_install.py`, appended to sys.path by `enable_addons()`).
    The Windows bundle needs `python3.dll` copied in by `build.ps1` - abi3
    wheels (PyAV) link it and PyInstaller omits it. Never `--upgrade` add-ons
    already imported in the process (Windows locks loaded `.pyd`), and never
    install over a pending removal (`_REMOVE_MARKER`): `rmtree` skips locked
    `.pyd` and pip `--target` skips existing folders - together they left
    half-deleted `tokenizers`/`av` and every transcription failed. MP3 in
    every install: `audio_transcode.ffmpeg_exe()` = system ffmpeg, else
    `imageio-ffmpeg`'s binary; spawn console tools with `NO_WINDOW` (the
    windowed exe would flash a console per call). Never assume text files
    are read in UTF-8 on Windows - always pass `encoding="utf-8"`. **The
    installer's "Launch FalconSDR" goes through `explorer.exe`, never the exe
    directly**: a process Inno Setup starts inherits its RedirectionTrust
    mitigation and can't follow the (non-admin) symlinks of the Hugging Face
    model cache - every model load failed with "Unable to open file
    'model.bin'" until the app was restarted from the Start menu. A model
    that can't load raises `ModelUnavailable` - the recording goes back to
    the queue (worker retries in `_MODEL_RETRY_S`), never an "error" result.
44. **"Start with the system" (`falconsdr/autostart.py`) is off by default
    and the OS login item is its only state** (HKCU `Run` / XDG autostart
    `.desktop` / macOS LaunchAgent - the last untested on a real Mac), no
    config flag that could disagree with it. Always `--no-browser`. Local
    mode only (`/api/autostart` refuses in server mode - systemd owns that).
    Windows-only UI help for the WinUSB/Zadig driver (`listen.js`
    `driverHelp()`, shown while `RECONNECTING` and in the SDR device card)
    keys off `capabilities.platform`.

45. **Digital voice (DMR, C4FM/YSF) is an optional add-on, never bundled**
    (`digital_voice.py`): dsd-fme + mbelib (AMBE/IMBE vocoder - patent-
    encumbered, so FalconSDR must never distribute it) are fetched from their
    authors on the user's machine - the authors' Windows zip, pinned source
    built into `DSD_DIR` elsewhere (`install_hint()` gives the root command for
    the -dev packages; the app never asks for a password). `dsd_executable()`
    is the one availability check. It installs on the *scanning* machine
    (`/api/digital`), unlike Whisper (gotcha 18): decoding is live. A digital
    slot runs its own dsd-fme process (`-i - -o -`), fed by
    `Demodulator.discriminate()` - raw 48 kHz discriminator through a
    `StreamResampler`, because per-block `resample_poly` puts an edge glitch
    every 30 ms = a lost DMR symbol per burst. `DsdDecoder.read(n)` always
    returns exactly n samples (zeros while nothing is decoded) so the slot's
    timeline never drifts. Only decoded audio is `real` for the Recorder - an
    encrypted call is muted by dsd-fme and must not leave a silent file.
    **`DsdDecoder`'s argument list stays input/output/mode only - never add a
    key or privacy option** (`-b`, `-H`, `-1`, `-k`/`-K`, `-4`, ... - a dozen,
    see `dsd-fme -h`): no decryption, ever. Without the add-on, DMR/YSF channels play as NFM. In the
    UI, `modOptions()` (bands.js) greys them out and `select()`'s onpick sends
    the user to `#/settings/digital`; the "experimental, please report bugs"
    note appears only in the Install dialog - no banners.

46. **Several dongles (experimental) = one `ScannerEngine` per dongle**
    (`config.device` + `device.extra_devices`, each `{driver, device_args}`),
    not one engine juggling several streams - each keeps its own
    `AcquisitionThread`, slots, gain/AGC, hold and recency boosts. Every
    engine builds the *same* plan from the same config and keeps every n-th
    window by frequency (`_partition_windows`, applied at the end of
    `_rebuild_windows` - any new lane type must survive it); a dongle left
    with one window parks there. `SharedFrequencies` makes sure one
    transmission is recorded once even if two dongles' windows overlap (edge
    probes) - plain lock, never touched by an AcquisitionThread (gotcha 11).
    `Controller` keeps `_engines`/`_lives` lists: the UI status is merged
    (`_merged_live_locked`, plus per-dongle `dongles`), the speaker and the
    spectrum follow one dongle (`_audible`) - it keeps them while its
    transmission lasts - and Skip/Hold/Record act on that one. Measured on a
    virtual busy airband (36 random 1-4 s transmissions in 45 s): 1/2/4
    dongles recorded 14/28/50 % of them, no duplicates - the default 5 s
    linger is what holds a single dongle back.

47. **Bias-T (DC on the antenna input) is off by default and per dongle**
    (verified on a real RTL-SDR Blog V4: its LED follows the switch) (`DeviceConfig.bias_tee`, an extra dongle's `bias_tee` key).
    Both backends open with it off, switch it on only as the very last step
    of a successful open (a failed open never leaves the antenna powered),
    re-assert it on every retune, and switch it off in `close()` - the V4
    keeps the state across opens. Soapy "on" writes only `biastee`/
    `bias_tee`, never `offset_tuning` (that changes tuning on other tuners);
    "off" writes all three. `device_args` can never turn it on (filtered).
    The UI asks for confirmation (damage warning; Cancel is the highlighted
    primary, focused button and Turn on a plain one - owner's call: people
    click the highlighted button) and resets it to off when a different
    dongle is picked.

48. **`Demodulator._decimate()` (AM/NFM/SSB) and WFM stage 1 are streamed
    (`StreamResampler`), never a per-block `resample_poly`.** On a 30 ms
    block the anti-alias filter sags the signal at both edges; AM's envelope
    detector turns that into a 33 Hz pulse train = a "motorbike idle" buzz
    (issue #6; measured +38 dB lines at 33.3 Hz harmonics on a clean carrier,
    gone after the fix). WFM's audio-rate stage is streamed too. Also
    `_apply_agc` ramps the gain across the block from the previous block's
    (a scalar per block = a 33 Hz click train on NFM/WFM speech: -46 -> -62
    dB vs the tone; a smoothstep ramp measured worse). Not a cause: strong
    out-of-band signals. Remaining: NR adds ~-72 dB sidebands, a per-sample
    AGC envelope would remove the rest. The output of the first block is 10
    samples (0.8 ms filter delay) short.

## Testing without touching the user's real config/recordings

Unit tests: `python -m unittest tests/test_core.py` (sets its own scratch
`HOME` *and* `USERPROFILE` - `Path.home()` reads the latter on Windows; with
`HOME` alone the tests once overwrote the real config there). To smoke-test end to end, run `HOME=/tmp/x python3 main.py
--no-browser --port 18810` with a config there that has
`"debug_mode": true, "device": {"driver": "simulated"}` and a scratch
`recording.output_dir` — otherwise
config edits during a manual test run land in the real
`~/.config/FalconSDR/config.json`. When constructing `Controller`/
`ScannerEngine` directly in a script, set config overrides *before*
construction. UI changes: check every page in a headless Chrome (DevTools
protocol: console errors + screenshots, desktop and ~390 px wide). Verified
working combos: real RTL-SDR via `SoapyBackend`/`RtlSdrBackend`, and full
state-machine + WAV recording + browser UI + worker (transcription, mirror,
upload) via `SimulatedBackend`.
