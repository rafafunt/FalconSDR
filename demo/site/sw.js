// FalconSDR online demo: a virtual receiver living in this service worker.
// It answers the unchanged web UI's /api/* calls the way falconsdr/web/server.py
// does - scanner state machine, spectrum, live audio stream, recordings
// library, statistics - from synthetic transmissions (demo/audio.py).
// State stays in this browser (Cache Storage), nothing is sent anywhere.

const RATE = 12500;
const STATE_KEY = '/demo/state.json';
const STATE_VERSION = 4;  // bump when saved state stops matching the code (clip ids, defaults)
// Two virtual receivers; picking one sets its capture width (demo.js).
const DEVICES = [
  { driver: 'demo-rtl', label: 'Demo RTL-SDR (simulated, 2.4 MHz)', rate: 2.4e6, gain: [0, 49.6] },
  { driver: 'demo-wide', label: 'Demo wideband SDR (simulated, 10 MHz)', rate: 10e6, gain: [0, 59] },
];
const KEEPALIVE_S = 5;

const DEMO_TEXT = {
  pl: {
    'Demo RTL-SDR (simulated, 2.4 MHz)': 'Demo RTL-SDR (symulowany, 2,4 MHz)',
    'Demo wideband SDR (simulated, 10 MHz)': 'Demo SDR szerokopasmowy (symulowany, 10 MHz)',
    'Not available in the online demo - download FalconSDR to use it.': 'Niedostępne w demo online - pobierz FalconSDR, żeby z tego skorzystać.',
  },
};

// Fictional stations (demo/audio.py). level = dBFS at the default gain.
const SOURCES = [
  { f: 118100000, conv: 'tower', level: -17, gap: [5, 12] },
  { f: 125350000, conv: 'approach', level: -24, gap: [8, 20] },
  { f: 132850000, conv: 'radar', level: -37, gap: [8, 18] },
  { f: 446031250, conv: 'pmr3', level: -21, gap: [12, 30] },
  { f: 446081250, conv: 'pmr7', level: -28, gap: [15, 35] },
  { f: 145500000, conv: 'ham', level: -22, gap: [15, 35] },
  { f: 138775000, conv: 'milvhf', level: -26, gap: [15, 35] },
  { f: 251150000, conv: 'miluhf', level: -23, gap: [12, 30] },
  { f: 156800000, conv: 'marine16', level: -20, gap: [25, 50] },
  { f: 156625000, conv: 'marine72', level: -22, after: 'marine16' },  // they move to 72 when told so on 16
  { f: 438725000, conv: 'rpt', level: -24, gap: [15, 35] },
  { f: 99900000, loop: 'music', level: -12 },
  { f: 103700000, loop: 'talk', level: -16 },
  { f: 124800000, birdie: true, level: -27 },
  { f: 144000000, birdie: true, level: -25 },
];

let FX = null;      // fixtures.json (built from the real app by demo/build.py)
let CLIPS = null;   // audio/clips.json
const pcm = {};     // clip id -> Int16Array
let st = null;      // persisted state
let uiLang = 'en';
const log = [];
let logId = 0;

// One tuner per dongle (config.device + device.extra_devices), like the app's
// one ScannerEngine per dongle: each scans every n-th window. Slots are shared,
// so a transmission is recorded once; the speaker follows sim.primary.
const sim = {
  running: false, windows: [], tuners: [],
  slots: new Map(), primary: null, firstStart: null, solo: null, manual: null, rawIq: false, schedSig: '', schedAt: 0,
  capLogged: new Map(),
};
const newTuner = (k) => ({ k, windows: [], win: -1, winUntil: 0, lastVisit: new Map(), cycle: null, hold: false, ignored: new Set(), on: new Set(), state: 'SCANNING' });
const STATE_RANK = ['SIGNAL_DETECTED', 'LINGER', 'SCANNING'];
const sdrPrefix = (T) => (sim.tuners.length > 1 ? `SDR ${T.k + 1}: ` : '');
// The dongle whose status, spectrum, Skip and Hold the UI means: the one being heard.
const shownTuner = () => (sim.slots.get(sim.primary) || {}).tuner || sim.tuners[0];
const listeners = new Set();
let audioT = 0, keepaliveT = 0;

// ------------------------------------------------------------------ utils
const json = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers } });
const fail = (status, error) => json({ error }, status);
const nowS = () => performance.now() / 1000;
const rand = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const clone = (o) => JSON.parse(JSON.stringify(o));
const pad = (n) => String(n).padStart(2, '0');

function currentLang() {
  const l = st && st.config.language;
  return l && l !== 'auto' ? l : uiLang;
}
// Same catalog as the app; Python-style {name} / {name:.4f} placeholders.
function tr(text, vals = {}) {
  const lang = currentLang();
  const s = (FX.i18n[lang] || {})[text] || (DEMO_TEXT[lang] || {})[text] || text;
  return s.replace(/\{(\w+)(?::\.(\d)f)?\}/g, (m, k, d) => (k in vals ? (d ? Number(vals[k]).toFixed(+d) : String(vals[k])) : m));
}

function addLog(text) {
  if (log.length && log[log.length - 1].text === text) return;
  log.push({ id: ++logId, time: Date.now() / 1000, text });
  if (log.length > 100) log.shift();
}

let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    st.running = sim.running;
    try {
      const cache = await caches.open('falconsdr-demo');
      await cache.put(STATE_KEY, new Response(JSON.stringify(st), { headers: { 'Content-Type': 'application/json' } }));
    } catch { /* no Cache Storage (some in-app browsers): the demo just starts fresh next time */ }
  }, 400);
}

// ------------------------------------------------------------------ init
let ready = null;
function init() {
  ready ||= (async () => {
    [FX, CLIPS] = await Promise.all(['/demo/fixtures.json', '/demo/audio/clips.json'].map((u) => fetch(u, { cache: 'no-cache' }).then((r) => r.json())));
    let saved = null;
    try { saved = await (await caches.open('falconsdr-demo')).match(STATE_KEY); } catch { /* see save() */ }
    st = saved ? await saved.json() : null;
    if (!st || st.version !== `${FX.version}/${STATE_VERSION}`) st = freshState(st);
    addLog(tr('Ready.'));
    setInterval(tick, 50);
    if (st.running) startScanner();
  })();
  return ready;
}

// Fetched when first needed (a source loads its next line ahead), not all upfront.
const loading = {};
function loadClip(id) {
  return (loading[id] ||= fetch(`/demo/audio/${id}.pcm`).then((r) => r.arrayBuffer()).then((b) => (pcm[id] = new Int16Array(b)))
    .catch((e) => { delete loading[id]; throw e; }));
}

const preset = (id) => FX.presets.find((p) => p.id === id);
const bandFrom = (id, enabled) => {
  const p = preset(id);
  return {
    id: Math.random().toString(36).slice(2, 10), start_hz: p.start_hz, stop_hz: p.stop_hz, step_hz: p.step_hz, label: tr(p.label),
    enabled, modulation: p.modulation, weight: 0, trigger_level_dbfs: null, tuning_time_ms: null, linger_time_ms: null,
    gain_mode: null, gain_db: null, noise_reduction_enabled: null, noise_reduction_strength: null, squelch_enabled: null, squelch_offset_db: null,
  };
};

function defaultConfig() {
  const c = clone(FX.config);
  delete c.access_token;
  c.device.driver = DEVICES[0].driver;
  c.device.sample_rate_hz = DEVICES[0].rate;
  c.recording.output_dir = '/home/demo/FalconSDR/recordings';
  c.bands = [bandFrom('civil_air', true), bandFrom('pmr446', true), bandFrom('ham_2m', true), bandFrom('fm_broadcast', false)];
  c.workers = {};
  // Nothing real can be heard or recorded here - the app shows its legal notices once it's installed.
  const langs = FX.languages.map((l) => l.code);
  c.legal_disclaimer = { launch_seen_languages: langs, recording_seen_languages: langs };
  c.squelch_enabled = true;
  c.squelch_offset_db = -3;
  return c;
}

// Off by default: an enabled channel is scanned even with every band switched off.
const channel = (mhz, name, modulation, enabled = false) => ({ freq_hz: Math.round(mhz * 1e6), name, modulation, enabled, folder: '', weight: 0, ch833: false });

function freshState(old) {
  const s = {
    version: `${FX.version}/${STATE_VERSION}`,
    config: defaultConfig(),
    channels: [
      channel(121.5, 'Guard', 'AM'), channel(118.1, 'Falcon Tower', 'AM'), channel(125.35, 'Falcon Approach', 'AM'),
      channel(132.85, 'Falcon Radar', 'AM'), channel(446.03125, 'PMR 3', 'NFM'), channel(145.5, '2 m calling', 'NFM'),
      channel(99.9, 'Radio Demo FM', 'WFM'), channel(103.7, 'Falcon FM', 'WFM'),
      channel(138.775, 'Range Control', 'AM'), channel(251.15, 'Falcon Ops', 'AM'),
      channel(156.8, 'Marine 16', 'NFM'), channel(156.625, 'Marine 72', 'NFM'), channel(438.725, 'SR0DEMO repeater', 'NFM'),
    ],
    blacklist: [],
    presets: clone(FX.presets),
    worker: { transcribe: true, mirror: false, mirror_dir: '' },
    library: [],
    running: false,
  };
  seedLibrary(s);
  return s;
}

// A week of past traffic so Recordings and Statistics have something to show.
function seedLibrary(s) {
  const weights = [['tower', 5], ['approach', 4], ['radar', 1], ['pmr3', 2], ['pmr7', 1], ['ham', 1], ['milvhf', 1], ['miluhf', 1], ['marine16', 1], ['marine72', 1], ['rpt', 1], ['music', 1], ['talk', 1]];
  const total = weights.reduce((a, [, w]) => a + w, 0);
  const now = Date.now();
  for (let d = 6; d >= 0; d--) {
    for (let k = 0, n = 4 + Math.floor(Math.random() * 5); k < n; k++) {
      let r = Math.random() * total, conv = 'tower';
      for (const [c, w] of weights) { if ((r -= w) < 0) { conv = c; break; } }
      const src = SOURCES.find((x) => x.conv === conv || x.loop === conv);
      const day = new Date(now - d * 86400e3);
      day.setHours(Math.floor(rand(6, 22)), Math.floor(rand(0, 60)), Math.floor(rand(0, 60)), 0);
      if (day.getTime() > now - 120e3) continue;
      const lines = src.loop ? [] : convLines(conv);
      const from = Math.floor(Math.random() * Math.max(1, lines.length - 1));
      const parts = lines.slice(from, from + 2 + Math.floor(Math.random() * 2));
      const e = makeEntry(src, src.f, day.getTime(), parts, src.loop ? rand(8, 25) : 0, rand(-4, 3));
      if (Math.random() < 0.12) e.favorite = true;
      if (!src.loop || Math.random() < 0.5) e.transcript = transcriptFor(e);
      s.library.push(e);
    }
  }
}

// PMR and ham chatter in the UI language when there is a version in it; airband is English only.
function convLines(conv) {
  const ids = Object.keys(CLIPS.clips).filter((id) => CLIPS.clips[id].conv === conv);
  const lang = [currentLang(), 'en'].find((l) => ids.some((id) => CLIPS.clips[id].lang === l));
  return lang ? ids.filter((id) => CLIPS.clips[id].lang === lang) : ids;
}

// ------------------------------------------------------------------ sources
const sourceState = new Map();  // source -> {line, playing: {clip, t0, len}, nextAt}
function srcState(src) {
  let s = sourceState.get(src);
  if (!s) sourceState.set(src, (s = { line: 0, playing: null, nextAt: src.after ? Infinity : nowS() + rand(1, 8) }));
  return s;
}

function advanceSources(t) {
  for (const src of SOURCES) {
    if (!src.conv) continue;
    const s = srcState(src);
    if (s.playing && t >= s.playing.t0 + s.playing.len) {
      s.playing = null;
      const lines = convLines(src.conv);
      s.line++;
      if (s.line < lines.length) s.nextAt = t + rand(0.7, 1.6);
      else {
        s.line = 0;
        s.nextAt = src.after ? Infinity : t + rand(...src.gap);
        for (const next of SOURCES) if (next.after === src.conv) srcState(next).nextAt = t + rand(2, 4);
      }
    }
    if (!s.playing) loadClip(convLines(src.conv)[s.line] || convLines(src.conv)[0]).catch(() => {});
    if (!s.playing && t >= s.nextAt) {
      const lines = convLines(src.conv);
      if (s.line >= lines.length) s.line = 0;  // the language switched to a shorter version
      const clip = lines[s.line];
      s.playing = { clip, t0: t, len: CLIPS.clips[clip].samples / RATE };
      for (const slot of sim.slots.values()) if (slot.src === src) addPart(slot, s.playing);
      if (sim.manual && sim.primary && sim.slots.get(sim.primary)?.src === src) { addPart(sim.manual, s.playing); sim.manual.firstF ||= sim.primary; }
    }
  }
}

function addPart(target, playing) {
  const key = `${playing.clip}@${playing.t0}`;
  if (target.keys.has(key)) return;
  target.keys.add(key);
  target.parts.push(playing.clip);
}

// Birdies show up only a minute after the first Start, so a newcomer hears real traffic first.
const BIRDIE_DELAY_S = 60;
const onAir = (src) => !!(src.loop || (src.birdie ? sim.firstStart !== null && nowS() - sim.firstStart >= BIRDIE_DELAY_S : srcState(src).playing));
const loopClip = (src) => convLines(src.loop)[0];

// ------------------------------------------------------------------ schedule (falconsdr/schedule.py)
const minutes = (hhmm) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m; };
function isActive(e, d) {
  const start = minutes(e.start), end = minutes(e.end), t = d.getHours() * 60 + d.getMinutes(), day = (d.getDay() + 6) % 7;
  if (start === end) return e.days.includes(day);
  if (start < end) return e.days.includes(day) && start <= t && t < end;
  return (t >= start && e.days.includes(day)) || (t < end && e.days.includes((day + 6) % 7));
}
const activeIndices = (c) => c.schedule.entries.map((e, i) => (isActive(e, new Date()) ? i : -1)).filter((i) => i >= 0);
function scanSelection(c) {
  const entries = c.schedule.enabled ? activeIndices(c).map((i) => c.schedule.entries[i]) : [];
  if (!entries.length && (!c.schedule.enabled || c.schedule.outside === 'normal')) return [c.bands.filter((b) => b.enabled), c.memory_list_enabled];
  const ids = new Set(entries.flatMap((e) => e.band_ids));
  return [c.bands.filter((b) => ids.has(b.id)), entries.some((e) => e.memory_list)];
}

function scheduleCheck(t) {
  if (t - sim.schedAt < 5) return;
  sim.schedAt = t;
  const c = st.config;
  const sig = c.schedule.enabled ? `${c.schedule.outside}|${activeIndices(c)}` : '';
  if (sig === sim.schedSig) return;
  sim.schedSig = sig;
  if (!sim.running) return;
  if (c.schedule.enabled) {
    const names = activeIndices(c).map((i) => { const e = c.schedule.entries[i]; return e.name || `${e.start}–${e.end}`; });
    addLog(names.length ? tr('Schedule: now {names}.', { names: names.join(', ') })
      : c.schedule.outside === 'normal' ? tr('Schedule: outside the time ranges - scanning as usual.') : tr('Schedule: nothing planned right now - scanning paused.'));
  }
  rebuild(false);
}

// ------------------------------------------------------------------ scanner
const blacklisted = (f) => st.blacklist.some((b) => Math.abs(b.freq_hz - f) <= b.margin_hz);
const opt = (band, key, global) => (band && band[key] !== null && band[key] !== undefined ? band[key] : global);

function scanList() {
  const c = st.config;
  let [bands, ml] = scanSelection(c);
  if (sim.solo !== null) { bands = c.bands[sim.solo] ? [c.bands[sim.solo]] : []; ml = false; }
  const out = [];
  for (const b of bands) {
    if (b.start_hz === b.stop_hz) out.push({ f: b.start_hz, half: 6250, band: b });
    else for (let f = b.start_hz; f <= b.stop_hz; f += b.step_hz || 25000) out.push({ f, half: (b.step_hz || 25000) / 2, band: b });
  }
  if (ml) for (const ch of st.channels) if (ch.enabled) out.push({ f: ch.freq_hz, half: c.memory_channel_bandwidth_hz / 2, band: null });
  // A Memory List channel inside a scanned band is the same channel.
  return out.filter((x) => !blacklisted(x.f)).sort((a, b) => a.f - b.f).filter((x, i, a) => !i || x.f - a[i - 1].f > 1000);
}

function rebuild(logIt = true) {
  const chans = scanList();
  const width = st.config.device.sample_rate_hz, usable = width * 0.9;
  const windows = [];
  for (const ch of chans) {
    const w = windows[windows.length - 1];
    if (w && ch.f - w.lo <= usable) { w.chans.push(ch); w.hi = ch.f; } else windows.push({ lo: ch.f, hi: ch.f, chans: [ch] });
  }
  for (const w of windows) {
    w.center = (w.lo + w.hi) / 2;
    w.band = w.chans[0].band;
    w.matches = [];
    for (const src of SOURCES) {
      const ch = w.chans.find((x) => Math.abs(x.f - src.f) <= x.half);
      if (ch) w.matches.push({ src, ch });
    }
  }
  closeAllSlots();
  sim.windows = windows;
  const n = 1 + (st.config.device.extra_devices || []).length;
  sim.tuners = Array.from({ length: n }, (_, k) => newTuner(k));
  windows.forEach((w, i) => sim.tuners[i % n].windows.push(w));  // every n-th window, like the app
  if (logIt || sim.running) {
    for (const T of sim.tuners) addLog(sdrPrefix(T) + tr('Wideband scanning: {channels} channels in {windows} capture window(s) ({mhz:.2f} MHz each){extra}.', { channels: chans.length, windows: T.windows.length, mhz: width / 1e6, extra: '' }));
  }
}

function level(src, ch) {
  const c = st.config, band = ch.band;
  const mode = opt(band, 'gain_mode', c.device.gain_mode);
  const gain = opt(band, 'gain_db', c.device.gain_db);
  const off = mode === 'manual' ? (gain - 30) * 0.8 : mode === 'auto' ? 3 : 0;
  return src.level + off + Math.sin(nowS() * 1.7 + src.f % 97) * 1.2;
}
const trigger = (ch) => opt(ch.band, 'trigger_level_dbfs', st.config.trigger_level_dbfs);

function actives(T, w) {
  return w.matches.filter(({ src, ch }) => !T.ignored.has(ch.f) && onAir(src) && level(src, ch) >= trigger(ch));
}

function nextWindow(T, t) {
  T.ignored.clear();
  if (!T.windows.length) { T.win = -1; return; }
  T.win = (T.win + 1) % T.windows.length;
  const last = T.lastVisit.get(T.win);
  T.cycle = last ? t - last : null;
  T.lastVisit.set(T.win, t);
  const w = T.windows[T.win];
  T.winUntil = t + (opt(w.band, 'tuning_time_ms', st.config.timing.tuning_time_ms) + st.config.timing.scanning_dwell_ms) / 1000;
  T.state = 'SCANNING';
}

function openSlot(m, t, T) {
  const c = st.config;
  const recording = c.recording.enabled && (c.recording.max_concurrent_recordings === 0 || [...sim.slots.values()].filter((s) => s.rec).length < c.recording.max_concurrent_recordings);
  if (c.recording.enabled && !recording && Date.now() - (sim.capLogged.get(m.ch.f) || 0) > 30e3) {
    sim.capLogged.set(m.ch.f, Date.now());
    addLog(tr('Limit of {n} simultaneous recordings reached - {freq:.4f} MHz not recorded', { n: c.recording.max_concurrent_recordings, freq: m.ch.f / 1e6 }));
  }
  const slot = { tuner: T, f: m.ch.f, src: m.src, ch: m.ch, rec: recording, start: Date.now(), startT: t, lastOn: t, parts: [], keys: new Set(), lvl: level(m.src, m.ch) };
  const p = m.src.conv && srcState(m.src).playing;
  if (p) addPart(slot, p);
  sim.slots.set(slot.f, slot);
  return slot;
}

function closeSlot(slot) {
  sim.slots.delete(slot.f);
  if (sim.primary === slot.f) sim.primary = null;
  if (!slot.rec) return;
  const dur = (Date.now() - slot.start) / 1000;
  if (slot.src.conv && !slot.parts.length) return;
  if (dur < 0.5) return;
  const e = makeEntry(slot.src, slot.src.f, slot.start, slot.parts, Math.min(dur, slot.src.loop ? 60 : 20), slot.lvl - slot.src.level);
  if (slot.src.loop) e.offset = Math.floor((slot.startT * RATE) % CLIPS.clips[e.loopClip].samples);
  st.library.push(e);
  queueTranscript(e);
  save();
}

function closeAllSlots() { [...sim.slots.values()].forEach(closeSlot); }

function stepTuner(T, t) {
  T.on = new Set();
  if (!T.windows.length) { T.state = 'SCANNING'; return; }  // more dongles than windows: this one idles
  if (T.win < 0) nextWindow(T, t);
  const w = T.windows[T.win];
  const mine = () => [...sim.slots.values()].filter((s) => s.tuner === T);
  const found = actives(T, w).filter((m) => !sim.slots.has(m.ch.f));  // never a second dongle on the same transmission
  if (!mine().length) {
    if (t < T.winUntil) return;
    if (!found.length) { if (!T.hold) nextWindow(T, t); return; }
  }
  found.forEach((m) => openSlot(m, t, T));
  T.on = new Set(actives(T, w).map((m) => m.ch.f));
  for (const slot of mine()) {
    if (T.on.has(slot.f)) { slot.lastOn = t; slot.lvl = level(slot.src, slot.ch); }
    else if (t - slot.lastOn > opt(slot.ch.band, 'linger_time_ms', st.config.timing.linger_time_ms) / 1000) closeSlot(slot);
  }
  if (!mine().length) { if (T.hold) T.state = 'SCANNING'; else nextWindow(T, t); return; }
  T.state = mine().some((s) => T.on.has(s.f)) ? 'SIGNAL_DETECTED' : 'LINGER';
}

function step(t) {
  for (const T of sim.tuners) stepTuner(T, t);
  // The speaker keeps its transmission while it lasts, then goes to whatever is on air - on any dongle.
  const slots = [...sim.slots.values()];
  if (!slots.length) return;
  const onAirNow = (s) => s.tuner.on.has(s.f);
  const cur = sim.slots.get(sim.primary);
  if (!cur || (!onAirNow(cur) && slots.some(onAirNow))) {
    const best = slots.sort((a, b) => (onAirNow(b) - onAirNow(a)) || (b.lvl - a.lvl))[0];
    sim.primary = best.f;
    const p = best.src.conv && srcState(best.src).playing;
    if (sim.manual && p) { addPart(sim.manual, p); sim.manual.firstF ||= best.f; }
  }
}

function simState() {
  if (!sim.running) return 'IDLE';
  return sim.tuners.map((T) => T.state).sort((a, b) => STATE_RANK.indexOf(a) - STATE_RANK.indexOf(b))[0] || 'SCANNING';
}

function startScanner() {
  if (sim.running) return;
  sim.running = true;
  sim.firstStart ??= nowS();
  rebuild(true);
  const devs = [st.config.device, ...(st.config.device.extra_devices || [])];
  sim.tuners.forEach((T) => {
    if (devs[T.k] && devs[T.k].bias_tee) addLog(sdrPrefix(T) + tr('Bias-T is on: the dongle powers its antenna input (until scanning stops).'));
  });
  save();
}

function stopScanner() {
  closeAllSlots();
  if (sim.manual) stopManual();
  sim.running = false;
  sim.tuners.forEach((T) => { T.win = -1; T.hold = false; });
  sim.rawIq = false;
  save();
}

// Skip acts on the channel being listened to; other transmissions in the window
// carry on. A skipped channel is ignored until the scanner leaves the window.
function skip(lockout) {
  if (!sim.running) throw new Error(tr('Scanner is not running'));
  const slot = sim.slots.get(sim.primary);
  const T = slot ? slot.tuner : sim.tuners[0];
  if (!T) return;
  T.hold = false;  // Skip means "leave", as in the app
  if (!slot) { nextWindow(T, nowS()); return; }
  closeSlot(slot);
  T.ignored.add(slot.f);
  if (lockout) {
    const today = new Date();
    st.blacklist = [...st.blacklist, { freq_hz: slot.f, margin_hz: st.config.default_blacklist_margin_hz, label: 'Auto-lockout (SKIP)', date_added: `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}` }];
    for (const w of sim.windows) w.matches = w.matches.filter((m) => !blacklisted(m.ch.f));
    save();
  }
  if (sim.slots.size) sim.primary = [...sim.slots.values()].sort((a, b) => b.lvl - a.lvl)[0].f;
  if (![...sim.slots.values()].some((s) => s.tuner === T)) nextWindow(T, nowS());
}

function stopManual() {
  const m = sim.manual;
  sim.manual = null;
  const dur = (Date.now() - m.start) / 1000;
  const src = SOURCES.find((s) => s.f === m.firstF) || null;
  const e = makeEntry(src, m.firstF || 0, m.start, m.parts, m.parts.length ? 0 : Math.min(dur, 30), 0);
  st.library.push(e);
  queueTranscript(e);
  save();
}

// ------------------------------------------------------------------ audio
function noiseAmp(lvl) { return clamp(0.25 * 10 ** ((-45 - lvl) / 20), 0.015, 0.35); }

// Live audio of the primary channel for [t0, t0 + n/RATE).
function renderLive(t0, n) {
  const slot = sim.primary && sim.slots.get(sim.primary);
  if (!sim.running || !slot) return null;
  const c = st.config, band = slot.ch.band;
  const nr = opt(band, 'noise_reduction_enabled', c.noise_reduction.enabled) ? 1 - 0.85 * opt(band, 'noise_reduction_strength', c.noise_reduction.strength) : 1;
  const squelch = opt(band, 'squelch_enabled', c.squelch_enabled);
  const out = new Float32Array(n);
  const src = slot.src, s = src.conv ? srcState(src) : null;
  const amp = noiseAmp(slot.lvl) * nr;
  const loopPcm = src.loop && pcm[loopClip(src)];
  if (src.loop && !loopPcm) loadClip(loopClip(src)).catch(() => {});
  for (let i = 0; i < n; i++) {
    const t = t0 + i / RATE;
    let v = 0, live = true;
    if (src.loop) v = loopPcm ? loopPcm[Math.floor(t * RATE) % loopPcm.length] / 32768 : 0;
    else if (src.birdie) v = 0.05 * Math.sin(2 * Math.PI * 740 * t) + 0.02 * Math.sin(2 * Math.PI * 1480 * t);
    else if (s.playing && t >= s.playing.t0) {
      const d = pcm[s.playing.clip], k = Math.floor((t - s.playing.t0) * RATE);
      v = d && k < d.length ? d[k] / 32768 : 0;
    } else live = false;
    if (!live && squelch) { out[i] = 0; continue; }
    out[i] = v + (Math.random() * 2 - 1) * (live ? amp : 0.18 * nr);
  }
  return out;
}

function toPcm16(f) {
  const out = new Int16Array(f.length);
  for (let i = 0; i < f.length; i++) out[i] = clamp(f[i], -1, 1) * 32767;
  return new Uint8Array(out.buffer);
}

function pumpAudio(t) {
  if (t - audioT > 0.5) audioT = t - 0.05;
  const n = Math.floor((t - audioT) * RATE);
  if (n <= 0) return;
  const start = audioT;
  audioT += n / RATE;
  if (!listeners.size) { audioT = t; return; }
  let chunk = null;
  const live = renderLive(start, n);
  if (live) { chunk = toPcm16(live); keepaliveT = t; }
  else if (t - keepaliveT > KEEPALIVE_S) { chunk = new Uint8Array(Math.round(RATE * 0.01) * 2); keepaliveT = t; }
  if (!chunk) return;
  for (const ctrl of listeners) {
    try { ctrl.enqueue(chunk); } catch { listeners.delete(ctrl); }
  }
}

function audioStream() {
  let me;
  const body = new ReadableStream({
    start(ctrl) { me = ctrl; listeners.add(ctrl); },
    cancel() { listeners.delete(me); },
  });
  return new Response(body, { headers: { 'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Sample-Rate': String(RATE) } });
}

function tick() {
  const t = nowS();
  advanceSources(t);
  scheduleCheck(t);
  if (sim.running) step(t);
  pumpAudio(t);
  finishTranscripts();
}

// ------------------------------------------------------------------ library
function makeEntry(src, f, ts, parts, seconds, dLevel) {
  const d = new Date(ts);
  const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
  const name = f ? `${(f / 1e6).toFixed(4)}MHz_${stamp}_${time}` : `${stamp}_${time}_session`;
  const samples = parts.reduce((a, id) => a + CLIPS.clips[id].samples + 0.5 * RATE, 0) + Math.round(seconds * RATE);
  const lvl = (src ? src.level : -40) + dLevel;
  return {
    id: `${stamp}/${name}.wav`, f, ts, parts, seconds, offset: 0, kind: src && src.loop ? 'loop' : src && src.birdie ? 'tone' : 'clips', loopClip: src && src.loop ? loopClip(src) : null,
    lvl, samples, quality: Math.round(clamp(42 + (lvl + 45) * 1.6 + rand(-3, 3), 18, 97) * 10) / 10,
    snr: Math.round(clamp(lvl + 62 + rand(-2, 2), 3, 45) * 10) / 10, favorite: false, transcript: null, queuedAt: 0, lang: null,
  };
}

function transcriptFor(e) {
  const ids = e.loopClip ? [e.loopClip] : e.parts;
  const text = ids.map((id) => CLIPS.clips[id].text).join(' ').trim();
  const lang = e.lang || (ids.length ? CLIPS.clips[ids[0]].lang : '');
  return {
    text, language: text ? lang : '', language_probability: text ? 0.97 : 0, status: text ? 'ok' : 'no_speech',
    engine: 'demo', transcribed_at: new Date().toISOString(),
  };
}

function queueTranscript(e) {
  if (st.config.transcription.enabled && st.worker.transcribe) e.queuedAt = Date.now();
}

function finishTranscripts() {
  let changed = false;
  for (const e of st.library) {
    if (e.queuedAt && Date.now() - e.queuedAt > 3500) { e.queuedAt = 0; e.transcript = transcriptFor(e); changed = true; }
  }
  if (changed) save();
}

const names = () => new Map(st.channels.map((c) => [Math.round(c.freq_hz / 1000), c.name]));
const khz = (e) => Math.round(e.f / 1000);
const iso = (ts) => new Date(ts).toISOString().replace('Z', '+00:00');
const sizeOf = (e) => 44 + e.samples * 2;

function item(e, nm) {
  return {
    id: e.id, freq_khz: khz(e), name: nm.get(khz(e)) || '', manual: e.f === 0, time: iso(e.ts), size: sizeOf(e),
    duration: Math.round((e.samples / RATE) * 100) / 100, quality: e.quality, snr: e.snr, clipping: 0,
    transcript: e.transcript, queued: !!e.queuedAt, favorite: e.favorite, mirrored: false,
  };
}

function select(q) {
  const sel = q.getAll('sel').filter((s) => s && s !== 'all');
  const bands = sel.filter((s) => s.startsWith('band:')).map((s) => s.slice(5).split('-').map(Number));
  const freqs = new Set(sel.filter((s) => s.startsWith('freq:')).map((s) => +s.slice(5)));
  const langs = new Set(sel.filter((s) => s.startsWith('lang:')).map((s) => s.slice(5)));
  const fav = sel.includes('fav');
  const text = (q.get('q') || '').trim().toLowerCase();
  const since = q.get('since') ? +q.get('since') : null, until = q.get('until') ? +q.get('until') : null;
  const minq = +(q.get('minq') || 0);
  const nm = names();
  return st.library.filter((e) => {
    if (sel.length) {
      const lang = (e.transcript && e.transcript.language) || '';
      if (!((fav && e.favorite) || freqs.has(khz(e)) || langs.has(lang) || bands.some(([lo, hi]) => lo <= khz(e) * 1000 && khz(e) * 1000 <= hi))) return false;
    }
    if (since !== null && e.ts / 1000 < since) return false;
    if (until !== null && e.ts / 1000 > until) return false;
    if (minq > 0 && e.quality < minq) return false;
    if (text && !`${e.id} ${(e.f / 1e6).toFixed(3)} ${nm.get(khz(e)) || ''} ${(e.transcript && e.transcript.text) || ''}`.toLowerCase().includes(text)) return false;
    return true;
  });
}

function query(q) {
  const rows = select(q);
  const sort = q.get('sort') || 'new';
  const by = { quality: (a, b) => b.quality - a.quality, old: (a, b) => a.ts - b.ts, long: (a, b) => b.samples - a.samples }[sort] || ((a, b) => b.ts - a.ts);
  rows.sort(by);
  const offset = +(q.get('offset') || 0), limit = Math.min(+(q.get('limit') || 100), 500), nm = names();
  return {
    total: rows.length, total_bytes: rows.reduce((a, e) => a + sizeOf(e), 0),
    items: rows.slice(offset, offset + limit).map((e) => item(e, nm)),
    pending: st.library.filter((e) => e.queuedAt).length,
  };
}

function agg(group) {
  const q = group.map((e) => e.quality);
  return { count: group.length, quality: q.length ? Math.round((q.reduce((a, b) => a + b, 0) / q.length) * 10) / 10 : null, last: iso(Math.max(...group.map((e) => e.ts))) };
}

function groupBy(list, key) {
  const m = new Map();
  for (const e of list) { const k = key(e); if (!m.has(k)) m.set(k, []); m.get(k).push(e); }
  return m;
}

function facets() {
  const nm = names(), lib = st.library;
  return {
    total: lib.length,
    favorites: lib.filter((e) => e.favorite).length,
    frequencies: [...groupBy(lib, khz)].map(([f, g]) => ({ key: `freq:${f}`, freq_khz: f, name: nm.get(f) || '', ...agg(g) })),
    bands: st.presets.map((p) => [p, lib.filter((e) => p.start_hz <= khz(e) * 1000 && khz(e) * 1000 <= p.stop_hz)]).filter(([, g]) => g.length)
      .map(([p, g]) => ({ key: `band:${p.start_hz}-${p.stop_hz}`, label: p.label, start_hz: p.start_hz, ...agg(g) })),
    languages: [...groupBy(lib, (e) => (e.transcript && e.transcript.language) || '')].map(([l, g]) => ({ key: `lang:${l}`, language: l, ...agg(g) })),
  };
}

function stats(q) {
  const recs = select(q), utc = q.get('utc') === '1', nm = names();
  const parts = (e) => {
    const d = new Date(e.ts);
    return utc ? [d.toISOString().slice(0, 10), d.getUTCHours(), (d.getUTCDay() + 6) % 7]
      : [`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, d.getHours(), (d.getDay() + 6) % 7];
  };
  const dur = (e) => e.samples / RATE;
  const freqs = [...groupBy(recs, khz)].map(([f, g]) => ({
    freq_khz: f, name: nm.get(f) || '', count: g.length, quality: agg(g).quality, duration: g.reduce((a, e) => a + dur(e), 0), last: agg(g).last,
  })).sort((a, b) => b.count - a.count);
  const perDay = [...groupBy(recs, (e) => parts(e)[0])].map(([d, g]) => [d, g.length]).sort();
  const perHour = Array.from({ length: 24 }, (_, h) => [h, recs.filter((e) => parts(e)[1] === h).length]);
  const perWeekday = Array.from({ length: 7 }, (_, d) => recs.filter((e) => parts(e)[2] === d).length);
  const ts = recs.map((e) => e.ts);
  return {
    summary: {
      count: recs.length, duration: recs.reduce((a, e) => a + dur(e), 0), from: ts.length ? iso(Math.min(...ts)) : null, to: ts.length ? iso(Math.max(...ts)) : null,
      quality: recs.length ? Math.round((recs.reduce((a, e) => a + e.quality, 0) / recs.length) * 10) / 10 : 0,
      frequencies: freqs.length, busiest: freqs.length ? freqs[0].freq_khz : null,
    },
    frequencies: freqs, per_day: perDay, per_hour: perHour, per_weekday: perWeekday,
  };
}

// The recording's audio, rebuilt from its clips (nothing is stored).
async function samplesOf(e) {
  const out = new Float32Array(e.samples);
  const amp = noiseAmp(e.lvl);
  let k = 0;
  for (const id of e.parts) {
    const d = await loadClip(id);
    for (let i = 0; i < d.length && k < out.length; i++) out[k++] = d[i] / 32768;
    k += Math.round(0.5 * RATE);
  }
  if (e.kind === 'loop') {
    const m = await loadClip(e.loopClip);
    for (let i = 0; k < out.length; i++) out[k++] = m[(e.offset + i) % m.length] / 32768;
  } else if (e.kind === 'tone') {
    for (let i = 0; k < out.length; i++, k++) out[k] = 0.05 * Math.sin((2 * Math.PI * 740 * i) / RATE);
  }
  if (e.f) for (let i = 0; i < out.length; i++) out[i] += (Math.random() * 2 - 1) * amp;
  return out;
}

const wavCache = new Map();
async function wavOf(e) {
  if (wavCache.has(e.id)) return wavCache.get(e.id);
  const data = toPcm16(await samplesOf(e));
  const buf = new ArrayBuffer(44 + data.length), v = new DataView(buf);
  const str = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, 'RIFF'); v.setUint32(4, 36 + data.length, true); str(8, 'WAVEfmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, data.length, true);
  new Uint8Array(buf, 44).set(data);
  const bytes = new Uint8Array(buf);
  if (wavCache.size > 20) wavCache.clear();
  wavCache.set(e.id, bytes);
  return bytes;
}

async function recording(req, q) {
  const e = st.library.find((x) => x.id === q.get('id'));
  if (!e) return fail(404, 'Not found');
  const bytes = await wavOf(e), size = bytes.length;
  const headers = { 'Content-Type': 'audio/wav', 'Accept-Ranges': 'bytes' };
  if (q.get('download')) headers['Content-Disposition'] = `attachment; filename="${e.id.split('/').pop()}"`;
  const m = (req.headers.get('Range') || '').match(/^bytes=(\d*)-(\d*)/);
  if (!m) return new Response(bytes, { headers: { ...headers, 'Content-Length': String(size) } });
  let start = m[1] ? +m[1] : Math.max(0, size - +m[2]);
  let end = m[1] && m[2] ? Math.min(+m[2], size - 1) : size - 1;
  if (start > end) return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${size}` } });
  return new Response(bytes.slice(start, end + 1), { status: 206, headers: { ...headers, 'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': String(end - start + 1) } });
}

async function peaks(q) {
  const e = st.library.find((x) => x.id === q.get('id'));
  if (!e) return [];
  const s = await samplesOf(e), n = 600, step = Math.max(1, Math.floor(s.length / n)), out = [];
  for (let b = 0; b < n && b * step < s.length; b++) {
    let m = 0;
    for (let i = b * step; i < Math.min(s.length, (b + 1) * step); i++) m = Math.max(m, Math.abs(s[i]));
    out.push(m);
  }
  const top = Math.max(...out, 1e-9);
  return out.map((v) => Math.round((v / top) * 1000) / 1000);
}

// ------------------------------------------------------------------ live snapshot
function spectrum() {
  const T = shownTuner();
  const w = T && T.windows[T.win];
  if (!sim.running || !w) return null;
  const sr = st.config.device.sample_rate_hz, f0 = w.center - sr / 2, f1 = w.center + sr / 2, n = 512;
  const p = new Float64Array(n);
  for (let i = 0; i < n; i++) p[i] = 10 ** ((-62 + (Math.random() - 0.5) * 4 + 1.5 * Math.cos((i / n) * Math.PI * 2)) / 10);
  for (const src of SOURCES) {
    if (src.f < f0 || src.f > f1 || !onAir(src)) continue;
    const ch = w.matches.find((m) => m.src === src)?.ch || { band: w.band };
    const lvl = level(src, ch), x = ((src.f - f0) / (f1 - f0)) * (n - 1);
    const width = Math.max(0.4, (src.loop ? 85e3 : src.birdie ? 1e3 : 6.5e3) / ((f1 - f0) / n));  // half-width in bins
    for (let i = Math.max(0, Math.floor(x - width * 4)); i <= Math.min(n - 1, Math.ceil(x + width * 4)); i++) {
      const shape = src.loop ? (Math.abs(i - x) < width ? 0 : -((Math.abs(i - x) - width) * 3)) : -((i - x) ** 2) / (2 * width * width) * 4.3;
      p[i] += 10 ** ((lvl + shape + (Math.random() - 0.5) * 2) / 10);
    }
  }
  const db = Array.from(p, (v) => Math.round(10 * Math.log10(v) * 10) / 10);
  const trig = opt(w.band, 'trigger_level_dbfs', st.config.trigger_level_dbfs);
  return { f0, f1, db, trigger: trig, custom: !!(w.band && ['trigger_level_dbfs', 'tuning_time_ms', 'linger_time_ms', 'gain_mode', 'gain_db', 'noise_reduction_enabled', 'noise_reduction_strength', 'squelch_enabled', 'squelch_offset_db'].some((k) => w.band[k] !== null && w.band[k] !== undefined)) };
}

function live(since = Infinity) {
  const nm = new Map(st.channels.map((c) => [c.freq_hz, c.name]));
  const nameOf = (f) => nm.get(f) || [...nm].find(([cf]) => Math.abs(cf - f) < 2000)?.[1] || '';
  const slots = [...sim.slots.values()].sort((a, b) => (b.f === sim.primary) - (a.f === sim.primary));
  const T = shownTuner();
  const w = T && T.windows[T.win];
  const placeholder = (W) => [[W.chans[0].f, W.chans.length > 1 ? tr('{count} channels in window', { count: W.chans.length }) : nameOf(W.chans[0].f)]];
  const primary = sim.slots.get(sim.primary);
  let lvl = -60 + Math.random() * 2;
  if (primary) lvl = onAir(primary.src) ? primary.lvl : lvl;  // the hold after a transmission shows the noise
  else if (w) for (const { src, ch } of w.matches) if (onAir(src)) lvl = Math.max(lvl, level(src, ch));
  const d = new Date();
  return {
    state: simState(), level: Math.round(lvl * 10) / 10,
    frequencies: sim.running ? (slots.length ? slots.map((s) => [s.f, nameOf(s.f)]) : w ? placeholder(w) : []) : [],
    recording: slots.some((s) => s.rec), manual_recording: !!sim.manual, raw_iq: sim.rawIq, hold: sim.tuners.some((x) => x.hold),
    ...(sim.running && sim.tuners.length > 1 ? { dongles: sim.tuners.map((x) => {
      const own = slots.filter((s) => s.tuner === x);
      const W = x.windows[x.win];
      return { state: x.state, frequencies: own.length ? own.map((s) => [s.f, nameOf(s.f)]) : W ? placeholder(W) : [] };
    }) } : {}),
    cycle_time: sim.running && T ? T.cycle : null, gain_range: (DEVICES.find((d) => d.driver === st.config.device.driver) || DEVICES[0]).gain, running: sim.running, stopping: false,
    solo_band: sim.solo, schedule_active: activeIndices(st.config), clock: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
    spectrum: spectrum(), log: log.filter((e) => e.id > since), audio_rate: RATE,
    demo_birdie: !!(primary && primary.src.birdie),
  };
}

// ------------------------------------------------------------------ settings
function settings() {
  return {
    version: FX.version, config: st.config, channels: st.channels, blacklist: st.blacklist, presets: st.presets,
    devices: DEVICES.map((d) => ({ driver: d.driver, args: '', label: tr(d.label), rate: d.rate })),
    languages: FX.languages,
    capabilities: { ffmpeg: true, transcription_installed: true, digital: false, platform: 'linux', autostart: null },
    curated: FX.curated, mode: 'local', password_set: false,
  };
}

function mergePatch(target, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (k === 'access_token' || k === 'workers' || !(k in target)) continue;
    if (v && typeof v === 'object' && !Array.isArray(v) && target[k] && typeof target[k] === 'object' && !Array.isArray(target[k])) mergePatch(target[k], v);
    else target[k] = v;
  }
}

function presetId(label) {
  const base = (label || 'preset').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'preset';
  let id = base, i = 2;
  while (st.presets.some((p) => p.id === id)) id = `${base}_${i++}`;
  return id;
}

const notInDemo = () => { throw new Error(tr('Not available in the online demo - download FalconSDR to use it.')); };

// ------------------------------------------------------------------ routes
async function body(req) { try { return await req.json(); } catch { return {}; } }

const ROUTES = {
  'GET /api/session': () => ({ authenticated: true, mode: 'local', remote: '' }),
  'GET /api/i18n': (req, q) => {
    const want = (q.get('lang') || 'en').slice(0, 2).toLowerCase();
    uiLang = FX.i18n[want] ? want : 'en';
    const lang = currentLang();
    // Band names were stored translated (like the app does) - follow a language switch.
    for (const b of st.config.bands) {
      const p = FX.presets.find((x) => [x.label, ...Object.values(FX.i18n).map((c) => c[x.label])].includes(b.label));
      if (p) b.label = tr(p.label);
    }
    return { lang, catalog: FX.i18n[lang] || {} };
  },
  'POST /api/login': () => ({ ok: true }),
  'POST /api/logout': () => ({ ok: true }),
  'GET /api/live': (req, q) => live(+(q.get('since_log') || 0)),
  'GET /api/audio': () => audioStream(),
  'POST /api/scanner/start': () => { startScanner(); return live(); },
  'POST /api/scanner/stop': () => { stopScanner(); return live(); },
  'POST /api/scanner/skip': () => { skip(false); return live(); },
  'POST /api/scanner/blacklist': () => { skip(true); return live(); },
  'POST /api/scanner/hold': async (req) => {
    if (!sim.running) throw new Error(tr('Scanner is not running'));
    const on = !!(await body(req)).enabled;
    if (on) { const T = shownTuner(); if (T) T.hold = true; } else sim.tuners.forEach((T) => { T.hold = false; });  // like the app
    return live();
  },
  'POST /api/scanner/manual_record': async (req) => {
    if (!sim.running) throw new Error(tr('Scanner is not running'));
    const on = !!(await body(req)).enabled;
    if (on && !sim.manual) {
      sim.manual = { start: Date.now(), parts: [], keys: new Set(), firstF: 0 };
      const slot = sim.slots.get(sim.primary), p = slot && slot.src.conv && srcState(slot.src).playing;
      if (p) { addPart(sim.manual, p); sim.manual.firstF = slot.f; }
    }
    else if (!on && sim.manual) stopManual();
    return live();
  },
  'POST /api/scanner/raw_iq': async (req) => {
    if (!sim.running) throw new Error(tr('Scanner is not running'));
    sim.rawIq = !!(await body(req)).enabled;
    return live();
  },
  'POST /api/scanner/solo': async (req) => {
    const b = (await body(req)).band;
    sim.solo = b === null || b === undefined ? null : +b;
    if (sim.running) rebuild(false);
    return live();
  },
  'GET /api/settings': () => settings(),
  'POST /api/config': async (req) => {
    const patch = await body(req);
    mergePatch(st.config, patch);
    if (sim.running && ['bands', 'memory_list_enabled', 'schedule', 'device', 'memory_channel_bandwidth_hz'].some((k) => k in patch)) rebuild(false);
    save();
    return { ok: true };
  },
  'PUT /api/channels': async (req) => { st.channels = await body(req); if (sim.running) rebuild(false); save(); return { ok: true }; },
  'PUT /api/blacklist': async (req) => {
    st.blacklist = (await body(req)).map((d) => ({ freq_hz: +d.freq_hz, margin_hz: +(d.margin_hz ?? 5000), label: String(d.label || ''), date_added: String(d.date_added || '') }));
    if (sim.running) rebuild(false);
    save();
    return { ok: true };
  },
  'POST /api/presets/save': async (req) => {
    const d = await body(req);
    const p = { id: d.id || presetId(d.label), label: d.label, tooltip: d.tooltip || '', start_hz: +d.start_hz, stop_hz: +d.stop_hz, step_hz: +d.step_hz, modulation: d.modulation || 'AM', user: true };
    const i = st.presets.findIndex((x) => x.id === p.id);
    if (i >= 0) st.presets[i] = p; else st.presets.push(p);
    save();
    return p;
  },
  'POST /api/presets/delete': async (req) => { const { id } = await body(req); st.presets = st.presets.filter((p) => p.id !== id); save(); return { ok: true }; },
  'POST /api/presets/import': async (req) => {
    const list = await body(req);
    if (!Array.isArray(list)) throw new Error('Expected a list of presets');
    for (const d of list) st.presets.push({ ...d, id: st.presets.some((p) => p.id === d.id) || !d.id ? presetId(d.label) : d.id, user: true });
    save();
    return { count: list.length };
  },
  'POST /api/disclaimer': async (req) => {
    const { kind, lang } = await body(req);
    const seen = st.config.legal_disclaimer[`${kind}_seen_languages`];
    if (seen && !seen.includes(lang)) seen.push(lang);
    save();
    return { ok: true };
  },
  'POST /api/reset': () => {
    const keep = st.config.legal_disclaimer;
    st.config = defaultConfig();
    st.config.legal_disclaimer = keep;
    if (sim.running) rebuild(false);
    save();
    return { ok: true };
  },
  'POST /api/access': notInDemo,
  'POST /api/quit': notInDemo,
  'POST /api/autostart': notInDemo,
  'GET /api/digital': () => ({ installed: false, running: false, log: [], error: '', hint: '' }),
  'POST /api/digital/install': notInDemo,
  'POST /api/digital/uninstall': notInDemo,
  'POST /api/client/connect': notInDemo,
  'GET /api/library': (req, q) => query(q),
  'GET /api/library/ids': (req, q) => select(q).map((e) => e.id),
  'GET /api/library/facets': () => facets(),
  'GET /api/library/stats': (req, q) => stats(q),
  'GET /api/library/storage': () => {
    const lib = st.library;
    return {
      root: st.config.recording.output_dir, count: lib.length, bytes: lib.reduce((a, e) => a + sizeOf(e), 0),
      oldest: lib.length ? iso(Math.min(...lib.map((e) => e.ts))) : null, mirrored: 0, favorites: lib.filter((e) => e.favorite).length,
      total: 256e9, free: 180e9, retention_pending: 0, retention_bytes: 0,
    };
  },
  'GET /api/dirs': (req, q) => {
    const path = q.get('path') || '/home/demo';
    const kids = { '/': ['home'], '/home': ['demo'], '/home/demo': ['FalconSDR', 'Music'], '/home/demo/FalconSDR': ['recordings'] }[path] || [];
    return { path, parent: path === '/' ? null : path.replace(/\/[^/]+$/, '') || '/', dirs: kids.map((n) => ({ name: n, path: `${path === '/' ? '' : path}/${n}` })), roots: ['/'] };
  },
  'POST /api/library/delete': async (req) => {
    const ids = new Set((await body(req)).ids);
    const before = st.library.length;
    st.library = st.library.filter((e) => !ids.has(e.id));
    save();
    return { deleted: before - st.library.length };
  },
  'POST /api/library/retention': () => ({ deleted: 0 }),
  'POST /api/library/favorite': async (req) => {
    const b = await body(req), ids = new Set(b.ids);
    st.library.forEach((e) => { if (ids.has(e.id)) e.favorite = b.favorite !== false; });
    save();
    return { ok: true };
  },
  'POST /api/library/transcript': async (req) => {
    const b = await body(req), e = st.library.find((x) => x.id === b.id);
    if (e) { e.transcript = { ...(e.transcript || transcriptFor(e)), text: String(b.text || ''), status: 'ok' }; save(); }
    return { ok: true };
  },
  'POST /api/library/transcribe': async (req) => {
    const b = await body(req), ids = new Set(b.ids);
    st.library.forEach((e) => { if (ids.has(e.id)) { e.transcript = null; e.lang = b.language || null; e.queuedAt = Date.now(); } });
    save();
    return { ok: true };
  },
  'GET /api/recording/peaks': (req, q) => peaks(q),
  'GET /api/workers': () => ({
    workers: [{
      name: 'demo-pc', online: true,
      info: { embedded: true, status: st.library.some((e) => e.queuedAt) ? 'transcribing' : 'idle', installed: true, gpu: '', log: ['connected'], local_only: 0, mirror_root: '', last_seen: Date.now() / 1000 },
      settings: st.worker,
    }],
    pending: st.library.filter((e) => e.queuedAt).length,
  }),
  'POST /api/workers/update': async (req) => { Object.assign(st.worker, (await body(req)).settings || {}); save(); return { ok: true }; },
  'POST /api/workers/command': () => ({ ok: true }),
  'POST /api/workers/forget': () => ({ ok: true }),
};

async function handle(req, url) {
  if (!ready && url.pathname === '/api/i18n') uiLang = (url.searchParams.get('lang') || 'en').slice(0, 2).toLowerCase();
  await init();
  if (url.pathname === '/api/recording') return recording(req, url.searchParams);
  if (url.pathname === '/api/demo/reset') {
    stopScanner();
    st = freshState(null);
    log.length = 0;
    save();
    return json({ ok: true });
  }
  const route = ROUTES[`${req.method} ${url.pathname}`];
  if (!route) return fail(404, 'Not found');
  try {
    const res = await route(req, url.searchParams);
    return res instanceof Response ? res : json(res);
  } catch (e) {
    return fail(400, e.message || String(e));
  }
}

// Also loaded as a plain page script where service workers aren't available
// (demo.js inPage()): the listeners below then never fire and handle() is
// called directly.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// A hard reload bypasses the worker - the page then asks to be taken over again.
self.addEventListener('message', (e) => { if (e.data === 'claim') self.clients.claim(); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin === location.origin && url.pathname.startsWith('/api/')) e.respondWith(handle(e.request, url));
});
