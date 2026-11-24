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
python3 -m unittest tests/test_core.py tests/test_addons.py
python3 main.py --probe-sdr [driver]   # with the scanner stopped: measures a real receiver (gotcha 55)
```

**Running tests without waste** (suite ~25 s): run the class a change touches; the full suite once before a commit, not after
edits that cannot reach Python (docs, `pl.po`, JS, packaging scripts: `node --check` / `bash -n`). Pipe through `tail`. No new test
that needs real time or 2.4 MS/s signals when a small one fails the same way. Keep regression tests, cut duplicates.

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
- `falconsdr/addons/` — optional decoders: built-in APRS/ACARS on a slot's audio, external rtl_433/dumpvdl2
  supervised as processes (gotcha 58, `docs/addons.md`). `AppConfig.addons`; UI `views/addons.js`, `views/messages.js`.
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

**Adding one:** only if breaking it fails silently, the code at the edit site does not show it and no test pins it - then the rule and the
why in <= 5 lines; measurements, history and how it was found go to `docs/gotchas/N.md`. Numbers are referenced from code: never renumber.

Gotchas 1, 2 and 26 are retired (Qt UI, PortAudio playback) and their numbers are kept free: settings are loaded once per view and written only for what a control changes (`state.js` `patchConfig`); live audio streams to the browser (gotcha 40).

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
8. **`SoapyBackend.read()` must raise on a fully empty read** (a dead device returns nothing and never raises), **and
    `create_backend()` falls back to `SimulatedBackend` only for `driver in ("", "auto")` AND `debug_mode` on.** With debug
    off a real open failure propagates (the retry/backoff state shows it); an explicitly picked real driver that fails
    always propagates. Reported case: a USB-permission/busy failure silently simulated and looked like a working scan. Full text: `docs/gotchas/8.md`.
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
16. **Windows/macOS get real RTL-SDR via `RtlSdrBackend`** (librtlsdr/`pyrtlsdrlib`; SoapySDR has no pip wheel there). Same
    contract as `SoapyBackend`: bias-tee as configured (gotcha 47), raises instead of short-reads, CF32 scaling `(i - 127.4)
    / 128`. `open_real_backend()` picks Soapy vs native from the driver string and what is installed; `"auto"` = librtlsdr
    first, SoapySDR if that fails, simulation only in debug if both fail. One device-list entry per way to open a receiver
    (`group`, `method`); a way the user chose that fails raises, never switches silently. **`SoapyBackend.__init__` must
    turn `driver in ("", "auto")` into `{"driver": "rtlsdr"}`, never `{}`**: an unfiltered open measured ~10 dB different
    channel power on the same dongle (overdriving). Full text: `docs/gotchas/16.md`.
17. **Other SoapySDR hardware (HackRF/LimeSDR/Airspy/SDRplay/USRP) already
    works via `SoapyBackend`** with no code changes once the matching
    `soapysdr-module-*` is installed — nothing in it is RTL-SDR-specific.
    HackRF and Airspy can also be opened natively (gotcha 55, experimental).
    The UI's capture-width field accepts `0.25–61.44` MHz (not a hardware limit,
    just a generous bound — raise it if a real device needs more). Windows/
    macOS stay RTL-SDR-only for the zero-install path; conda-forge SoapySDR
    is a documented, unautomated path for other hardware there.
18. **Transcription (faster-whisper) is an in-app-installable optional extra**, not in `requirements.txt`, and runs in a
    `Worker` (gotcha 37) on whichever computer has it on - never assume the scanning machine can run Whisper.
    `is_transcription_installed()` is the one availability check (no second ad-hoc import). `install_transcription()` tries
    plain `pip install`, falling back to a `--system-site-packages` venv only on PEP-668 (only that path needs a restart).
    `generic_model` (large-v3) detects the language and transcribes everything; `english_atc_model` is optional and empty by
    default. **`candidate_languages` is a bias, not a whitelist**: never skip detection because the list has one entry
    (skipping is only the explicit `fixed_language`). `_speech_seconds()` returns `no_speech` before either Whisper pass;
    `_get_models` reloads on `_model_key` only. A worker without Whisper never claims jobs; an `"error"` result is not
    re-queued in the same session (`Library._failed`). Full text: `docs/gotchas/18.md`.
19. **A frequency-less filename (`..._session.wav`, manual recording, gotcha 13) must be recognised by
    `FILENAME_REGEX`/`parse_filename()`** (`playback/config.py`/`organizer.py`) and mapped to `models.MANUAL_FREQ_KHZ` (0),
    or the file is silently skipped forever. `FILENAME_REGEX` is the single source of truth and must match `\.mp3$` too;
    every freq-label site needs a `MANUAL_FREQ_KHZ` case. The stdlib `wave` cannot read mp3: `audio_analysis.py` decodes via
    ffmpeg to a temp WAV in `APP_DATA_DIR` (not /tmp); duration comes from bytes read, not the header (ffmpeg pipes write
    0x7FFFFFFF). Full text: `docs/gotchas/19.md`.
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
23. **"Single File" auto-recording mode was removed, not fixed** (per-slot recorders never made one file).
    `Recorder(single_file=True)` exists only for manual recording (gotcha 13); `AppConfig.load()` pops the stale
    `mode`/`insert_marker` keys. Full text: `docs/gotchas/23.md`.
24. **`RecordingConfig.max_concurrent_recordings`** (default 3, 0 = unlimited) caps simultaneously *recorded* transmissions
    (`_at_recording_capacity()` gates `_open_slot()`); it never caps listening, manual or raw-IQ recording. A capped
    candidate is re-evaluated next tick. Both silent no-file cases (capacity skip, once per channel per 30 s; slot discarded
    below squelch) are logged to the Activity feed, which drops consecutive duplicates (`Controller.log`). Full text: `docs/gotchas/24.md`.
25. **`Demodulator`'s output leveler (`dsp.py`, `_apply_agc`, gotcha 57) is a
    persistent attack-fast/release-slow follower of the voice RMS across `demod()`
    calls** (state in `self._agc_level`, cleared by `reset()`) — never a
    per-chunk peak normalization computed from that chunk alone. The old
    per-chunk version dragged noise-only chunks up to the same loudness as
    voice chunks whenever their own peak cleared the floor, destroying real
    SNR before `analyze_wav()` ever measured it.
27. **Three gain modes (`DeviceConfig.gain_mode`): `"manual"`, `"auto"` (tuner hardware AGC) and `"sw_auto"` = `SoftwareAgc`
    (`agc.py`, the default).** Hardware AGC can overdrive the ADC, so `sw_auto` sends explicit manual values from the IQ
    peak (target ~-6 dBFS, +-3 dB dead zone, >=1 s between steps) using a peak smoothed over ~1 s (`SMOOTH_TAU_S`): every
    step stalls the dongle ~100 ms = an audible gap. Only a peak >= 0.97 of full scale is acted on unsmoothed. Each step
    calls `Demodulator.notify_rf_gain_change` and clears `level_history` (else `_detect_reflections()` reads the common dB
    shift as correlation and discards a real signal; rejected files are renamed `*.rejected`, never deleted). Full text: `docs/gotchas/27.md`.
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
31. **`_extend_edges()`/`extend_boundary_window()` must never fire for a frequency that is not part of a genuine multi-point
    band grid.** A Memory List point or a degenerate band with `start_hz == stop_hz` (the "Single frequency" button) would
    get up to ~28 fabricated candidate frequencies per side. Fixed with a `band_freqs` set (only bands with `start_hz !=
    stop_hz`) threaded through every `_extend_edges()` call in `_rebuild_windows()` (default lane and weighted lane). **Any
    new code path building windows from a `VfoRangeConfig` grid must apply the same check.** Full text: `docs/gotchas/31.md`.
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
35. **Frozen builds (PyInstaller): anything loaded at runtime must be collected explicitly.** `packaging/linux-deb/build.sh`
    (rpm/AppImage use the same flags) builds `--onedir` with `--collect-data tzdata` (zoneinfo's fallback is invisible to
    the analysis), `--collect-data pyrtlsdrlib` (librtlsdr is found via `importlib.resources`; a dev machine with a system
    rtl-sdr hides the failure) and `--add-binary libportaudio.so.2`. `main.py`'s venv re-exec and `transcription_install.py`
    gate on `getattr(sys, "frozen", False)`: a frozen `sys.executable` is the app, so never run `[sys.executable, "-m",
    "pip"]` there (explain through `on_line`). Any subprocess of a system binary uses `audio_transcode.system_env()` (the
    bundle's `LD_LIBRARY_PATH` broke system ffmpeg). `multiprocessing.freeze_support()` runs first when frozen. Build the
    .deb in Debian 11 (`build-in-docker.sh`; old glibc, else "GLIBC_2.42 not found"). The .deb ships `60-rtl-sdr.rules` and
    `blacklist-rtl-dvb.conf`; every `udevadm`/`rmmod` in `postinst` is wrapped in `timeout` (a hang blocks `apt install`). Full text: `docs/gotchas/35.md`.
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
42. **UI design follows the `apple-design` skill (HIG)**: appearance follows the system light/dark only (no in-app switch);
    colours are the semantic tokens in `app.css` (white button text only on `--accent-fill`/`--red-solid`; recompute the
    contrast figures in its header when changing a token); touch targets >=44 px under `@media (pointer: coarse)`, kept
    **last** in the file; icon-only buttons get `title` as `aria-label` (`core.js` `h()`). The amber frequency display +
    spectrum is the one signature element (always dark) - keep the rest quiet. **Never give `.topbar` a `backdrop-filter` on
    phones** (it becomes the containing block of the fixed tab bar). Page-level switches are `tabs()` (large, with counts),
    not `seg()`. Channels lists: checkbox column (shift = range), sticky bulk bar, "Delete all";
    click/Ctrl/Shift/Ctrl+A/Delete keep working. Full text: `docs/gotchas/42.md`.
43. **Windows release = `packaging/windows/build.ps1`** (PyInstaller `--onedir --noconsole` -> portable ZIP + Inno Setup
    `falconsdr.iss`; same `--collect-data` rules as gotcha 35). Every build runs `main.py --self-test` (`selftest.py`; add a
    check for any new runtime-loaded dependency) and a smoke run of the built exe with `PATH` = System32 and an empty
    profile. A windowed exe has `sys.stdout/stderr` = None: `main.py` logs to `CONFIG_DIR/falconsdr.log`. The installer's
    `AppId` must never change and it never touches the user's profile. Packaged builds install transcription too (`main.py
    --pip` runs the bundled pip into `ADDONS_DIR`; `build.ps1` copies `python3.dll` in); never `--upgrade` add-ons already
    imported (locked `.pyd`) and never install over a pending removal (`_REMOVE_MARKER`). MP3: system ffmpeg, else
    `imageio-ffmpeg`; spawn console tools with `NO_WINDOW`; always `encoding="utf-8"` for text files. **The installer's
    "Launch FalconSDR" goes through `explorer.exe`** (a process Inno Setup starts inherits a mitigation that breaks the
    Hugging Face model-cache symlinks). A model that cannot load raises `ModelUnavailable`: back to the queue, never an
    `"error"` result. Full text: `docs/gotchas/43.md`.
44. **"Start with the system" (`falconsdr/autostart.py`) is off by default
    and the OS login item is its only state** (HKCU `Run` / XDG autostart
    `.desktop` / macOS LaunchAgent - the last untested on a real Mac), no
    config flag that could disagree with it. Always `--no-browser`. Local
    mode only (`/api/autostart` refuses in server mode - systemd owns that).
    Windows-only UI help for the WinUSB/Zadig driver (`listen.js`
    `driverHelp()`, shown while `RECONNECTING` and in the SDR device card)
    keys off `capabilities.platform`.
45. **Digital voice (DMR, C4FM/YSF) is an optional add-on, never bundled** (`digital_voice.py`): dsd-fme + mbelib
    (patent-encumbered AMBE/IMBE) are fetched from their authors on the user's machine (`dsd_executable()` is the one
    availability check). It installs on the *scanning* machine (decoding is live). A digital slot runs its own dsd-fme
    process fed by `Demodulator.discriminate()` (carrier offset removed, 6.5 kHz channel filter, `StreamResampler` - never
    per-block `resample_poly`); `DsdDecoder.read(n)` always returns n samples; only decoded audio is `real` for the
    Recorder; a digital slot ignores squelch. **`DsdDecoder`'s argument list stays input/output/mode only - never add a key
    or privacy option: no decryption, ever.** Without the add-on DMR/YSF play as NFM (`modOptions()` greys them out).
    **Never tested against a real dsd-fme/DMR signal.** Full text: `docs/gotchas/45.md`.
46. **Several dongles (experimental) = one `ScannerEngine` per dongle** (`config.device` + `device.extra_devices`), each
    with its own `AcquisitionThread`, slots and gain/AGC. Every engine builds the same plan and keeps every n-th window by
    frequency (`_partition_windows`, applied at the end of `_rebuild_windows` - any new lane type must survive it);
    `SharedFrequencies` records a transmission once (plain lock, never touched by an `AcquisitionThread`, gotcha 11).
    `Controller` keeps `_engines`/`_lives`; the UI status is merged, the speaker and spectrum follow one dongle
    (`_audible`); `Controller._devices()` indexes must stay aligned with `extra_devices`. **Antenna range**
    (`antenna_range.py`, `range_start_hz`/`range_stop_hz`): with any range set `_rebuild_windows` uses `split_by_range`;
    each frequency goes to one dongle that reaches it, the rest show as `live["uncovered"]`. `demo/site/sw.js` mirrors
    `split_by_range`/`uncovered`: change both, then `demo/build.py` + `demo/deploy.sh`. **Receivers of different widths:**
    the capture width is per engine (`ScannerEngine.sample_rate_hz`, never `config.device.sample_rate_hz` directly); engines
    plan for the narrowest width and hand out windows in proportion (`split_by_range(weights=...)`, `widen_windows`); edge
    roll-off is learned per receiver kind (`_calibration_key`). Practical limit ~10 MS/s per engine. Full text: `docs/gotchas/46.md`.
47. **Bias-T is off by default and per dongle** (`DeviceConfig.bias_tee`, an extra dongle's `bias_tee`). Both backends open
    with it off, switch it on only as the last step of a successful open, re-assert it on every retune and switch it off in
    `close()`. Soapy "on" writes only `biastee`/`bias_tee` (never `offset_tuning`); `device_args` can never turn it on. The
    UI asks for confirmation (Cancel is the highlighted default - owner's call) and resets it to off when a different dongle
    is picked. Full text: `docs/gotchas/47.md`.
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
49. **A band's overrides are read through `_live_band(config, window)`, never
    `window.band` directly.** `merge_patch` replaces `config.bands` with new
    objects, so a window (held by the loop and by open slots) kept the old
    trigger/squelch/linger until the scan moved on - editing a band's own
    trigger during a detection did nothing. Related: `_rebuild_windows()` runs
    on the HTTP thread and swaps `_channel_bw_map`, so
    `channel_powers_from_spectrum` must survive a frequency missing from it
    (it crashed the engine - auto-reconnect hid it as a 3 s gap); and
    `patch_config` skips the rebuild for `_READ_LIVE_KEYS` (global
    trigger/squelch), which the engine reads every tick.
50. **Anything that holds the GIL for ~100 ms in the receiver process is an audible gap in live audio.** Fixed culprits:
    `json.dumps` of the big quality/transcription caches (use `config.dumps_chunked()`, save outside `Library._lock`; never
    `json.dumps()` a big structure in a background thread) and per-file `Path.resolve()` in `rescan()`; caches write only
    when their change counter moved, the derived quality cache at most every 5 min. **Measure CPU with `py-spy record
    --format raw` (throwaway venv) before optimizing** - the first guess (the FFT) was a fifth of it.
    `FALCONSDR_DEBUG_LIVE_AUDIO=1` prints GC pauses >= 20 ms and `[stall ...]` samples; measure gaps client-side (>=120 ms
    arrival gaps of the `/api/audio` stream). Full text: `docs/gotchas/50.md`.
51. **Test a `live-audio.js` change in a real browser, never only `node --check`**: `tests/live_audio/run.sh <live-audio.js>
    <scenario> <seconds> <tag>` plays a test-server tone through headless Chrome into a temporary PipeWire null sink and
    `analyze.py` counts silences, waveform jumps, streams opened and console lines. The loop must always `abort()` its
    response before a reconnect (a leaked response filled the browser's 6 connections and froze the page). Jitter buffer:
    resync only when really dry (`this.t <= now`), otherwise <=1 % speed change keeps the 0.4 s lead (3 % bent the pitch). Full text: `docs/gotchas/51.md`.
52. **Never load a whole recording into memory** (`audio_analysis._stream_samples`
    reads one `BLOCK_FRAMES` block at a time; `analyze_wav` keeps only an RMS per
    50 ms, `compute_waveform_peaks` a peak per small window). A WFM station that
    never falls below the trigger is *one* transmission: the 88.0 MHz recording
    ran 9.5 h = 850 MB, and when it closed (a restart) the library thread loaded
    it as float64 + `frames ** 2` = ~8 GB. The 6 GB container was OOM-killed
    ~20 s after every start (systemd `Restart=always` -> a loop of 25 restarts;
    the UI froze, "lost connection", the frequency vanished). Found by
    `py-spy dump` in a throwaway venv while the RSS grew. Any new code that
    reads recordings (stats, export, ...) must also work block by block.
53. **A view's `destroy()` must really run** (`recordings.js` `freshTimer`): a mid-line `//` comment (`audio.load();  //
    note io.disconnect(); clearInterval(t);`) silently commented out the cleanup and left a 4 s `/api/library` poll running
    on every page (GIL stalls = audible gaps). `StaticJsTest` catches the pattern. To see such a stall: the `health:` line
    in debug mode and `py-spy record --gil --threads`. Full text: `docs/gotchas/53.md`.
54. **`RtlSdrBackend` (librtlsdr: Windows, macOS, the .deb, the receiver) switches from blocking reads to an async stream
    after 8 reads on one frequency** (`_ASYNC_AFTER_READS`; `DeviceConfig.async_read`, default on, Settings -> Advanced).
    Blocking reads leave no transfer pending and the dongle drops samples (measured 0.78-2.16 %: a splice every 30 ms = the
    buzz of issue #6, voice pitched up); the stream lost 0.00 %. It is stopped at every retune and gain step.
    `AcquisitionThread._note_read_start` is blind while `backend.streaming`; `stream_stats()` is the loss figure then.
    `SoapyBackend` asks for 3.4 ms transfers x 64 and flushes after every `set_frequency` (stale blocks read as ghost
    peaks). A stream that ends before delivering a byte makes `read_raw` fall back to blocking reads for good; `close()`
    leaves the device open if the stream thread will not stop. **A crash inside librtlsdr is a `StreamFault`, not a
    disconnect** (issue #14): the engine sets `async_read = False`, emits `async_read_disabled` and reconnects at once; a
    marker file `CONFIG_DIR/rtlsdr_stream.active` (pid) left by a dead process keeps the stream off for one run. Not
    verified on a real affected dongle (cause inferred). Full text: `docs/gotchas/54.md`.
55. **HackRF/Airspy natively (`sdr_native.py`, drivers `hackrf_native`/`airspy_native`) is experimental** (only a HackRF One
    has run, issue #9; Airspy never). SoapySDR stays the default; native is the way out when Soapy is missing
    (`open_real_backend`) and a comparison. `libhackrf`/`libairspy` are loaded by `_find_library` (bundle, system,
    `FALCONSDR_LIBHACKRF`/`FALCONSDR_LIBAIRSPY`) and bundled in every package (BSD-3-Clause, notice appended to
    THIRD-PARTY-LICENSES; Windows DLLs pinned by SHA-256 in `fetch_native_libs.py`; build scripts set
    `FALCONSDR_REQUIRE_NATIVE_LIBS=1`). Do not copy SoapyHackRF/hackrf-tools code. What `main.py --probe-sdr` must confirm
    on a board: stale blocks after a retune (`CallbackStream.begin_retune`), gain steps (`hackrf_gain_split`), the DC spike
    at the window centre (no notch), losses. Airspy captures only its listed rates; HackRF 2-20 MS/s. Antenna power follows
    gotcha 47. `hackrf_init()` is never undone. **libusb:** without `_hold_default_usb_context()` a `libusb_exit(NULL)`
    frees libhackrf's implicit context and `hackrf_device_list()` segfaulted the whole server; the device listing therefore
    runs in a child process (`--list-native-sdr`). Full text: `docs/gotchas/55.md`.
56. **`rtl_tcp` (`RtlTcpBackend`, driver `rtl_tcp`, `device_args` `host=...,port=...`) is a network source, not a way to
    open a local dongle**: one fixed list entry, the address typed in the Listen card (the entry's `args` must follow it),
    any number as extra dongles. Written from rtl_tcp's public behaviour (stdlib only): 12-byte greeting `RTL0`, then raw
    8-bit IQ; 5-byte commands (0x01 freq, 0x02 rate, 0x03 gain mode, 0x04 gain, 0x0E bias-tee). **The stream does not stop
    at a retune**: `set_frequency` drains the socket and `read_raw` drops by the clock for `_RTLTCP_STALE_S` (not by byte
    count). The rate is checked against librtlsdr's ranges first (rtl_tcp silently ignores a refused one). Bias-T only when
    the user turned it on. rtl_tcp serves one client (a second is never greeted: "another one connected?"). `streaming` is
    always True. Verified on the owner's V4 over a LAN; **not verified:** command 0x0E, Wi-Fi, Windows/macOS, Android
    servers. The online demo lists it too (`demo/site/sw.js` `DEVICES`). Tests: `RtlTcpTest`. Full text: `docs/gotchas/56.md`.
57. **Loudness is levelled in three places; keep them apart.** (1) **AM is divided by its carrier** (`_track_carrier`, rise
    0.1 s / fall 0.6 s); `Demodulator.carrier_floor` is set by the engine to trigger -6 dB so noise is never scaled to voice
    loudness. (2) **`_apply_agc` levels the voice RMS to `_AGC_TARGET_RMS` (0.12 ~ -18 dBFS)** within -20..+21 dB and
    soft-limits above 0.8; a block below `_AGC_PAUSE_RATIO` is a pause: the level is held (tau 8 s). NFM/WFM need no
    per-modulation calibration; `notify_rf_gain_change` scales AM carrier and SSB level only. Tests: `LevelingTest`. (3)
    **Browser**: `live-audio.js` source -> boost (0/6/12/18 dB) -> `DynamicsCompressorNode` -> volume; the Recordings player
    uses the same chain (`makeLoudnessChain`) plus a per-file gain from `item["level"]` to -18 dBFS (limited to -12..+30 dB,
    "Even out volume"). Files are never modified, so no cache needs invalidating. A `createMediaElementSource` element is
    silent while its `AudioContext` is suspended: the player resumes it on `play`. Full text: `docs/gotchas/57.md`.
58. **Add-ons (`falconsdr/addons/`, design in `docs/addons.md`).** Built-in decoders (APRS, ACARS) are *data slots*:
    `AddonManager.claim(freq)` (enabled add-on, within `CLAIM_TOLERANCE_HZ`) makes `_open_slot` force the needed
    demodulation and attach `slot.data_decoder`; `_process_one_slot` feeds it the demod audio and returns before
    `_play_and_record_slot` unless `listen_to_data`. A data slot has primary weight -1 (never takes the speaker from voice).
    A decoder exception drops that decoder, never the scanner. They work only while the scanner visits the window (a bonus,
    not full reception). **ACARS data is NRZI**: the 1200 Hz tone flips the bit, 2400 Hz keeps it (bits = running XOR of the
    tones, both start levels tried). The old direct mappings decoded nothing for months and the synthetic tests had been
    written to match them: keep `tests/data/acars_real_131825.wav` (a real block). Bursts with a cut-off start (the scanner
    stops after the onset) cannot decode. **External programs (rtl_433, dumpvdl2):** `Controller.run_addon` stops the
    scanner, `ExternalRunner` takes the dongle, `stop_addon` resumes it; **start a program only after the reopen cooldown**
    (`SoapyBackend._wait_for_reopen_cooldown`; a program dying with `usb_claim_interface -6` within 10 s is retried 3
    times). **Never bundle or link them, never port their code** (rtl_433 is GPL-2.0). Installing (`addons/install.py`):
    rtl_433 = the author's release zip, dumpvdl2 = pinned source built locally, both checked against a SHA-256 (bump version
    and hash together). **A program path and extra arguments are a command line**: `/api/addons/configure` takes
    `path`/`extra_args` only from this computer or with the access password (`may_change_command_line`), and the generic
    `/api/config` patch drops them. With several receivers `Controller.run_addon(id, dongle)` pauses only that engine; a
    held receiver goes into `Controller._held` (an empty range in `antenna_range.device_ranges`, the other engines take its
    share); use `Controller._join`, never `Thread.join`, on engines. Schedule with receivers/add-ons:
    `ScheduleEntry.receivers`/`.addon`; `schedule.assignments()` gives every receiver a `Plan` (every engine computes all
    plans from the same config); the `schedule-addons` thread (`reconcile_schedule`, every 5 s) starts/stops add-ons, and
    one that exits with an error goes into `schedule_failed`. Events go to `EventHub` (ring + `events-DATE.jsonl`,
    `keep_days`; VDL2 link frames are counted but never stored); statistics in `addons/stats.py`. **The online demo
    simulates the add-ons** (`demo/site/sw.js`): a change to an add-on endpoint, event field or statistics bucket needs the
    demo changed, then `demo/build.py` + `demo/deploy.sh`. Everything but the Linux build of dumpvdl2 is unrun on
    Windows/macOS. Full text: `docs/gotchas/58.md`.
59. **Notifications (`notifier.py`, `views/notify.js`) and the rtl_433 catalog (`addons/sensors.py`).** Rules (source
    message/recording/transcript/system, filters, `within_km`) go to channels (ntfy, Telegram, webhook, MQTT QoS 0 - stdlib
    only). Nothing leaves the machine until the user adds a channel. Cooldown per (rule, who) >= `MIN_COOLDOWN_S` (30 s,
    there is no "no limit"), 6/min per rule; swallowed matches are counted in the next notice; sends run on the notifier's
    own thread, never on a decoder, engine or HTTP thread. **Channels hold secrets**: `token`/`password`/`header_value` are
    masked (`MASK`) on the way out and an unchanged mask keeps the stored value, so `notify` is not patchable through
    `/api/config` or `/api/settings` (own `/api/notify*`). Discord/Slack webhooks get their own JSON (`webhook_body`).
    rtl_433: `sensors.py` classifies devices and names readings so Messages, Statistics and the rule editor need no per-type
    code. **Remote controls and key fobs are dropped unless `remotes` is on, and their codes (`SECRET`) are never stored.**
    `ALIASES`: Nexus-T is read as `Rubicson-Temperature` (one device). `-R` means "only these", so the defaults are named
    again; wM-Bus needs `-s 1200k`. Tested with synthetic JSON lines only. Full text: `docs/gotchas/59.md`.
60. **TETRA ships in every package; its speech codec never does** (`addons/tetra.py`, `addons/tetra_codec.py`, `tetra-rs/`).
    Our own `falconsdr-tetra` and `tetra-rs/codec/falconsdr_sdecoder.c` are bundled laid out as in the repo (`--add-data`, see
    `linux-tetra.sh`, `build.ps1`, `macos.yml`; `--self-test` fails a build without them). The ACELP codec is ETSI's: one click
    downloads `en_30039502v010301p0.zip` from etsi.org on the user's computer (pinned SHA-256, needs a User-Agent), builds it with
    our front end (cc/gcc/clang, else TinyCC on Windows). ETSI's licence covers the code, not patents: never bundle or redistribute
    it. Linux builds only; Windows/macOS and real-air speech untested.

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
