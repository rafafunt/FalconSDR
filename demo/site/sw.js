// FalconSDR online demo: a virtual receiver living in this service worker.
// It answers the unchanged web UI's /api/* calls the way falconsdr/web/server.py
// does - scanner state machine, spectrum, live audio stream, recordings
// library, statistics - from synthetic transmissions (demo/audio.py).
// State stays in this browser (Cache Storage), nothing is sent anywhere.

const RATE = 12500;
const STATE_KEY = '/demo/state.json';
const STATE_VERSION = 5;  // bump when saved state stops matching the code (clip ids, defaults)
// Two virtual receivers; picking one sets its capture width (demo.js).
const DEVICES = [
  { driver: 'demo-rtl', label: 'Demo RTL-SDR (simulated, 2.4 MHz)', rate: 2.4e6, gain: [0, 49.6] },
  { driver: 'demo-wide', label: 'Demo wideband SDR (simulated, 10 MHz)', rate: 10e6, gain: [0, 59] },
  // The app's "Network receiver (rtl_tcp)": the address field is real UI, nothing is contacted - it scans like the first one.
  { driver: 'rtl_tcp', label: 'Network receiver (rtl_tcp)', rate: 2.4e6, gain: [0, 49.6] },
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
    axSeed();
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

// ------------------------------------------------------------------ antenna range (falconsdr/antenna_range.py)
const OPEN_END = 2 ** 40;
const rangeOf = (d) => {
  const lo = Math.max(0, +d.range_start_hz || 0), hi = +d.range_stop_hz > 0 ? +d.range_stop_hz : OPEN_END;
  return hi < lo ? [hi, lo] : [lo, hi];
};
const deviceRanges = () => [st.config.device, ...(st.config.device.extra_devices || [])].map(rangeOf);
const isLimited = (rs) => rs.some(([lo, hi]) => lo > 0 || hi < OPEN_END);

// What the user wants scanned that no dongle's range reaches (only when every dongle has a range).
function uncovered() {
  const rs = deviceRanges();
  if (!isLimited(rs) || rs.some(([lo, hi]) => lo === 0 && hi === OPEN_END)) return [];
  const merged = [];
  for (const [lo, hi] of [...rs].sort((x, y) => x[0] - y[0])) {
    const last = merged[merged.length - 1];
    if (last && lo <= last[1]) last[1] = Math.max(last[1], hi); else merged.push([lo, hi]);
  }
  const gaps = (start, stop) => {
    const out = [];
    let at = start;
    for (const [lo, hi] of merged) {
      if (hi < at) continue;
      if (lo > stop) break;
      if (lo > at) out.push([at, lo - 1]);
      at = Math.max(at, hi + 1);
    }
    if (at <= stop) out.push([at, stop]);
    return out;
  };
  const [bands, ml] = scanSelection(st.config);
  const items = [];
  for (const b of bands) {
    const [start, stop] = [b.start_hz, b.stop_hz].sort((x, y) => x - y), missing = gaps(start, stop);
    if (missing.length) items.push({ kind: 'band', label: b.label, start_hz: start, stop_hz: stop, gaps: missing });
  }
  if (ml) {
    const outside = st.channels.filter((c) => c.enabled && !merged.some(([lo, hi]) => lo <= c.freq_hz && c.freq_hz <= hi)).map((c) => c.freq_hz).sort((x, y) => x - y);
    if (outside.length) items.push({ kind: 'channels', count: outside.length, examples: outside.slice(0, 3) });
  }
  return items;
}

function makeWindow(chans) {
  const w = { lo: chans[0].f, hi: chans[chans.length - 1].f, chans, matches: [] };
  w.center = (w.lo + w.hi) / 2;
  w.band = chans[0].band;
  for (const src of SOURCES) {
    const ch = chans.find((x) => Math.abs(x.f - src.f) <= x.half);
    if (ch) w.matches.push({ src, ch });
  }
  return w;
}

function rebuild(logIt = true) {
  const chans = scanList();
  const width = st.config.device.sample_rate_hz, usable = width * 0.9;
  const groups = [];
  for (const ch of chans) {
    const g = groups[groups.length - 1];
    if (g && ch.f - g[0].f <= usable) g.push(ch); else groups.push([ch]);
  }
  const n = 1 + (st.config.device.extra_devices || []).length;
  const ranges = deviceRanges();
  const limited = isLimited(ranges);
  const share = Array.from({ length: n }, () => []);  // each dongle's groups of channels
  if (limited) {
    // Like split_by_range: a channel goes to one dongle whose range holds it (taking turns, window by
    // window, if several do), to none otherwise; a half-owned window keeps just its channels.
    const turn = new Map();
    for (const g of groups) {
      const by = new Map();
      for (const ch of g) {
        const who = ranges.map((r, k) => (r[0] <= ch.f && ch.f <= r[1] ? k : -1)).filter((k) => k >= 0);
        if (!who.length) continue;
        const key = who.join();
        if (!by.has(key)) by.set(key, { who, chans: [] });
        by.get(key).chans.push(ch);
      }
      for (const [key, { who, chans: cs }] of by) {
        const k = turn.get(key) || 0;
        turn.set(key, k + 1);
        share[who[k % who.length]].push(cs);
      }
    }
  } else groups.forEach((g, i) => share[i % n].push(g));  // every n-th window, like the app
  closeAllSlots();
  sim.tuners = share.map((gs, k) => {
    const T = newTuner(k);
    T.windows = gs.map(makeWindow);
    return T;
  });
  sim.windows = sim.tuners.flatMap((T) => T.windows);
  if (logIt || sim.running) {
    for (const T of sim.tuners) {
      addLog(sdrPrefix(T) + tr('Wideband scanning: {channels} channels in {windows} capture window(s) ({mhz:.2f} MHz each){extra}.', { channels: chans.length, windows: T.windows.length, mhz: width / 1e6, extra: '' }));
      const [low, high] = ranges[T.k];
      if (limited && !T.windows.length && (low > 0 || high < OPEN_END)) {
        addLog(sdrPrefix(T) + tr('Nothing for this receiver to scan inside its antenna range ({low}-{high} MHz).', { low: +(low / 1e6).toPrecision(6), high: high >= OPEN_END ? '...' : +(high / 1e6).toPrecision(6) }));
      }
    }
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
  if (ax.running) throw new Error(tr('An add-on is using the receiver - stop it first.'));
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
  axTick(Date.now() / 1000);
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
    solo_band: sim.solo, schedule_active: activeIndices(st.config), uncovered: uncovered(), clock: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
    spectrum: spectrum(), log: log.filter((e) => e.id > since), audio_rate: RATE,
    demo_birdie: !!(primary && primary.src.birdie),
    addon: ax.running || '', addons: ax.running ? [{ id: ax.running, receiver: 0 }] : [], addon_since: ax.since, notify_seq: ax.seq, schedule_now: axScheduleNow(),
  };
}

// ------------------------------------------------------------------ settings
function settings() {
  return {
    version: FX.version, config: st.config, channels: st.channels, blacklist: st.blacklist, presets: st.presets,
    // Like Controller.devices(): the network entry carries the typed address while it is the chosen one, or the UI loses it.
    devices: DEVICES.map((d) => ({ driver: d.driver, args: d.driver === 'rtl_tcp' && st.config.device.driver === 'rtl_tcp' ? st.config.device.device_args || '' : '', label: tr(d.label), rate: d.rate })),
    languages: FX.languages,
    capabilities: { addon_messages: true, ffmpeg: true, transcription_installed: true, digital: false, platform: 'linux', autostart: null },
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

// ------------------------------------------------------------------ add-ons (simulated)
// Fictional traffic in the shapes the real decoders produce (build.py runs the program's own parsers for rtl_433 and APRS;
// the aircraft messages are written here the way acars.py / external.parse_vdl2 return them). A seeded history fills
// Messages and Statistics; new messages appear while an add-on is on. Nothing is received and nothing is sent anywhere.
const AX_FIELD = { acars: 'acars_frequencies_hz', aprs: 'aprs_frequencies_hz', vdl2: 'vdl2_frequencies_hz' };
const AX_FLEET = [['SP-XQA', 'FS', 1204], ['SP-XQC', 'FS', 78], ['D-AXQB', 'QD', 4412], ['G-XQAB', 'QD', 213], ['OE-XQA', 'QD', 8801], ['PH-XQA', 'FS', 602],
  ['HA-XQA', 'XW', 3320], ['OK-XQA', 'XW', 115], ['LN-XQA', 'XW', 940], ['SE-XQA', 'FS', 2750], ['OO-XQA', 'QD', 1066], ['F-GXQA', 'FS', 1830], ['EI-XQA', 'XW', 77], ['TF-XQA', 'QD', 335]];
const AX_AIRPORTS = ['EPWA', 'EPKK', 'EPRZ', 'EPGD', 'EPWR', 'EPPO', 'EGLL', 'EDDF', 'LOWW', 'LFPG', 'EHAM', 'LKPR', 'LHBP', 'EKCH'];
const AX_WAYPOINTS = ['TEMDI', 'GOBAS', 'SOLAN', 'RIDAV', 'BEKLA', 'LAPSU', 'NOVEM', 'DOKAD', 'MIRKO', 'PEPIK'];
const AX_ACARS_HZ = [131725000, 131825000, 131525000];
const AX_VDL2_HZ = [136725000, 136775000, 136875000, 136975000];
const AX_PROTOCOLS = [
  { id: 1, name: 'Silvercrest Remote Control', default: true }, { id: 2, name: 'Rubicson, TFA 30.3197 or InFactory PT-310 Temperature Sensor', default: true },
  { id: 3, name: 'Prologue, FreeTec NC-7104, NC-7159-675 temperature sensor', default: true }, { id: 19, name: 'Nexus, FreeTec NC-7345, NX-3980, Solight TE82S, TFA 30.3209 temperature/humidity sensor', default: true },
  { id: 129, name: 'Eurochron temperature and humidity sensor', default: false }, { id: 133, name: 'Rubicson 48659 Thermometer', default: true },
  { id: 141, name: 'Auriol HG02832, HG05124A-DCF, Rubicson 48957 temperature/humidity sensor', default: true }, { id: 152, name: 'Eurochron EFTH-800 temperature and humidity sensor', default: true },
  { id: 222, name: 'Rubicson Pool Thermometer 48942', default: true }, { id: 242, name: 'Baldr / RainPoint rain gauge.', default: false },
];

const ax = { ring: [], link: [], nextId: Date.now(), hours: new Map(), running: null, since: 0, byTimer: false, resume: false, genAt: 0, next: [], counts: {}, notices: [], seq: Date.now(), cool: new Map(), sensors: null };

function axRng(seed) {   // mulberry32: the same history every time the worker starts
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const axPick = (r, list) => list[Math.floor(r() * list.length)];
const axPad = (n, w = 2) => String(Math.floor(n)).padStart(w, '0');

// -- messages -----------------------------------------------------------------------------------------------------------
function axAcarsFields(r, now) {
  const [reg, airline, base] = axPick(r, AX_FLEET);
  const flight = `${airline}${axPad(base + Math.floor(r() * 4), 4)}`;
  const dep = axPick(r, AX_AIRPORTS);
  let arr = axPick(r, AX_AIRPORTS);
  if (arr === dep) arr = 'EPWA';
  const d = new Date(now * 1000);
  const c = {
    reg, flight, dep, arr, wp: axPick(r, AX_WAYPOINTS), wp2: axPick(r, AX_WAYPOINTS), wp3: axPick(r, AX_WAYPOINTS),
    lat: `${axPad(48 + r() * 4)}${axPad(r() * 99)}${axPad(r() * 99)}`, lon: `${axPad(r() * 3, 3)}${axPad(r() * 99)}${axPad(r() * 99)}`,
    hms: `${axPad(d.getUTCHours())}${axPad(d.getUTCMinutes())}${axPad(d.getUTCSeconds())}`, hm: `${axPad(d.getUTCHours())}${axPad(d.getUTCMinutes())}`,
    hms2: `${axPad(d.getUTCHours() + (d.getUTCMinutes() > 40 ? 1 : 0))}${axPad((d.getUTCMinutes() + 9) % 60)}00`,
    fl: `${axPad(300 + Math.floor(r() * 12) * 10, 3)}`, temp: axPad(40 + r() * 20), wind: `${axPad(r() * 359, 3)}${axPad(5 + r() * 30, 3)}`,
    day: axPad(d.getUTCDate()), mon: axPad(d.getUTCMonth() + 1), rwy: axPick(r, ['9', '27', '33', '15']), atis: axPick(r, ['A', 'B', 'C', 'D', 'E', 'F']),
    qnh: 1000 + Math.floor(r() * 30), vdl: axPick(r, ['725', '775', '875', '975']),
  };
  // [label, downlink (aircraft sends), text]
  const T = [
    ['H1', true, `#M1BPOSN${c.lat}E${c.lon},${c.wp},${c.hms},${c.fl},${c.wp2},${c.hms2},${c.wp3},M${c.temp},${c.wind}`],
    ['80', true, `3N01 POSRPT ${c.day}${c.mon}/${c.dep}/${c.arr} .${c.reg}\n/POS N${c.lat.slice(0, 5)}E${c.lon.slice(0, 6)}\n/ALT ${c.fl}  /SPD 0.${78 + Math.floor(r() * 4)}`],
    ['14', true, `OFF${c.dep}${c.arr}${c.hm}`], ['14', true, `ON${c.dep}${c.arr}${c.hm}`],
    ['10', false, `PDC ${flight}\n${c.dep} TO ${c.arr}\nCLEARED TO ${c.arr} VIA ${c.wp}1A\nRWY ${c.rwy} QNH ${c.qnh}`],
    ['15', false, `${c.arr} ATIS ${c.atis} ${c.hm}Z\nRWY ${c.rwy} QNH ${c.qnh} WIND ${c.wind.slice(0, 3)}/${c.wind.slice(3).replace(/^0/, '')}KT`],
    ['5Z', true, `/${c.dep}YA.TI2/${c.arr}${c.hm}${c.reg}`], ['SA', true, `0EV136${c.vdl}`], ['Q0', true, ''], ['Q0', false, ''],
    ['B9', false, `FREE TEXT\nEXPECT ${c.wp} ARRIVAL ${c.hm}Z`],
  ];
  const [label, down, text] = axPick(r, T);
  const f = { mode: '2', registration: reg, ack: down ? String.fromCharCode(65 + Math.floor(r() * 26)) : '!', label, block_id: down ? String(1 + Math.floor(r() * 9)) : String.fromCharCode(65 + Math.floor(r() * 8)), text };
  if (down && text) { f.msg_no = `M${axPad(r() * 90)}${String.fromCharCode(65 + Math.floor(r() * 26))}`; f.flight = flight; }
  return f;
}
const axAcarsSummary = (e) => `${[e.registration, e.flight, `[${e.label}]`].filter(Boolean).join(' ')} ${e.text || ''}`.replace(/\n/g, ' ').trim();

function axAcars(r, now) {
  const f = axAcarsFields(r, now);
  const ev = { kind: 'acars', ...f, crc_ok: true, mapping: 'normal', freq_hz: axPick(r, AX_ACARS_HZ) };
  return [ev, axAcarsSummary(ev)];
}

function axVdl2(r, now, layer) {
  const addr = () => axPad(r() * 0xFFFFFF, 6).replace(/./g, () => '0123456789ABCDEF'[Math.floor(r() * 16)]);
  const ev = { kind: 'vdl2', layer, freq_hz: axPick(r, AX_VDL2_HZ), src: addr(), dst: addr(), sig_level: Math.round((-38 + r() * 24) * 10) / 10 };
  if (layer === 'acars') {
    const f = axAcarsFields(r, now);
    const reg = f.registration;
    Object.assign(ev, { frame_type: 'I', fields: { reg: `.${reg}`, label: f.label, msg_text: f.text, mode: f.mode, ack: f.ack, blk_id: f.block_id, ...(f.flight ? { flight: f.flight } : {}) },
      registration: reg, label: f.label, flight: f.flight, text: f.text });
    return [ev, axAcarsSummary(ev)];
  }
  ev.frame_type = layer === 'data' ? 'I' : axPick(r, ['RR', 'RR', 'RR', 'XID', 'UA', 'SABM', 'RNR']);
  ev.fields = {};
  return [ev, `${ev.src} → ${ev.dst} ${ev.frame_type}`];
}

function axAprs(r) {
  const s = axPick(r, FX.addons.aprs);
  return [{ ...clone(s.event), freq_hz: 144800000 }, s.summary];
}

function axSensors() {
  ax.sensors ||= FX.addons.sensors.map((d) => ({ ...d, every: d.every }));
  return ax.sensors;
}
// The value of one field at time `now` (a daily swing plus noise), or one of a list of choices.
function axValue(spec, r, now) {
  if (spec.length > 4 || typeof spec[0] === 'string') return axPick(r, spec);
  const [base, swing, noise, dec] = spec;
  const h = (now / 3600) % 24;      // warmest in the afternoon (UTC)
  const v = base + swing * Math.sin(((h - 9) / 24) * 2 * Math.PI) + (r() - 0.5) * 2 * noise;
  return +v.toFixed(dec);
}
function axSensor(dev, r, now) {
  const ev = clone(dev.event);
  for (const [key, spec] of Object.entries(dev.gen)) {
    const v = axValue(spec, r, now);
    if (key in ev.fields) ev.fields[key] = v;
    const reading = ev.readings.find((x) => x.key === key);
    if (reading) reading.value = v;
    const status = ev.status.find((x) => x.key === key);
    if (status) status.value = v;
  }
  const who = `${ev.model}${ev.device_id !== undefined && ev.device_id !== null ? ` #${ev.device_id}` : ''}`;
  let shown = ev.readings.slice(0, 3).map((x) => `${x.label.split(' ')[0] === 'Temperature' || x.label.split(' ')[0] === 'Humidity' ? '' : `${x.label} `}${x.value}${x.unit ? ` ${x.unit}` : ''}`).join(', ');
  if (!shown && ev.status.length) shown = ev.status.slice(0, 3).map((x) => `${x.key} ${x.value}`).join(', ');
  return [ev, shown ? `${who}: ${shown}` : who];
}

// -- statistics: the same hourly buckets as addons/stats.py ----------------------------------------------------------------
function axBucket(hour, addon) {
  let h = ax.hours.get(hour);
  if (!h) ax.hours.set(hour, h = {});
  return h[addon] ||= { n: 0, link: 0, last: 0, freq: {}, lvl: {}, who: {}, labels: {}, types: {}, pos: 0, sensors: {} };
}
function axRecord(e) {
  const b = axBucket(Math.floor(e.ts / 3600) * 3600, e.addon);
  b.last = Math.max(b.last, e.ts);
  if (e.kind === 'vdl2' && e.layer === 'link') { b.link += 1; return; }
  b.n += 1;
  if (e.freq_hz) {
    const k = String(Math.round(e.freq_hz));
    b.freq[k] = (b.freq[k] || 0) + 1;
    if (typeof e.sig_level === 'number') { const s = (b.lvl[k] ||= [0, 0]); s[0] += e.sig_level; s[1] += 1; }
  }
  const who = e.kind === 'sensor' ? `${e.model || '?'} #${e.device_id}` : String(e.registration || e.source || '');
  if (who && (who in b.who || Object.keys(b.who).length < 300)) { const w = (b.who[who] ||= [0, 0]); w[0] += 1; w[1] = Math.max(w[1], e.ts); }
  if (e.label) b.labels[e.label] = (b.labels[e.label] || 0) + 1;
  if (e.kind === 'aprs') {
    const k = String(e.type || '?');
    b.types[k] = (b.types[k] || 0) + 1;
    if ('lat' in e && 'lon' in e) b.pos += 1;
  }
  if (e.kind === 'sensor') {
    const s = (b.sensors[who] ||= { n: 0, last: 0, vn: 0, vsum: 0 });
    s.n += 1; s.last = Math.max(s.last, e.ts); s.cat = e.category || 'other';
    const main = (e.readings || []).find((x) => x.key === e.primary);
    if (main) {
      s.pl = main.label; s.pu = main.unit;
      s.vmin = Math.min(s.vmin ?? main.value, main.value); s.vmax = Math.max(s.vmax ?? main.value, main.value);
      s.vn += 1; s.vsum += main.value;
    }
    const batt = (e.fields || {}).battery_ok;
    if (batt !== undefined && batt !== null) s.batt = batt ? 1 : 0;
  }
}
function axSnapshot(days) {
  const now = Date.now() / 1000, since = days ? now - days * 86400 : 0;
  const out = {};
  const dayOf = (h) => new Date(h * 1000).toISOString().slice(0, 10);
  const available = new Set();
  for (const hour of [...ax.hours.keys()].sort((a, b) => a - b)) {
    available.add(dayOf(hour));
    if (hour + 3600 <= since) continue;
    for (const [addon, b] of Object.entries(ax.hours.get(hour))) {
      const a = (out[addon] ||= { n: 0, link: 0, last: 0, timeline: [], who: {}, freq: {}, lvl: {}, labels: {}, types: {}, pos: 0, farthest: null, sensors: {} });
      a.n += b.n; a.link += b.link; a.last = Math.max(a.last, b.last); a.pos += b.pos;
      a.timeline.push([hour, b.n, b.link]);
      for (const [name, [n, last]] of Object.entries(b.who)) { const w = (a.who[name] ||= [0, 0]); w[0] += n; w[1] = Math.max(w[1], last); }
      for (const key of ['freq', 'labels', 'types']) for (const [k, n] of Object.entries(b[key])) a[key][k] = (a[key][k] || 0) + n;
      for (const [k, [s, n]] of Object.entries(b.lvl)) { const t = (a.lvl[k] ||= [0, 0]); t[0] += s; t[1] += n; }
      for (const [name, s] of Object.entries(b.sensors)) {
        const t = (a.sensors[name] ||= { n: 0, vn: 0, vsum: 0, last: 0 });
        t.n += s.n; t.vn += s.vn; t.vsum += s.vsum; t.last = Math.max(t.last, s.last);
        if (s.last >= (t.seenAt || 0)) { t.seenAt = s.last; for (const k of ['cat', 'pl', 'pu']) if (k in s) t[k] = s[k]; }
        if ('vmin' in s) { t.vmin = Math.min(t.vmin ?? s.vmin, s.vmin); t.vmax = Math.max(t.vmax ?? s.vmax, s.vmax); }
        if ('batt' in s && s.last >= (t.battAt || 0)) { t.batt = s.batt; t.battAt = s.last; }
      }
    }
  }
  for (const a of Object.values(out)) {
    a.distinct = Object.keys(a.who).length;
    a.who = Object.entries(a.who).sort((x, y) => y[1][0] - x[1][0]).slice(0, 60).map(([n, [c, last]]) => [n, c, last]);
    a.freq = Object.entries(a.freq).sort((x, y) => y[1] - x[1]).map(([f, n]) => [+f, n, a.lvl[f] ? Math.round((a.lvl[f][0] / a.lvl[f][1]) * 10) / 10 : null]);
    delete a.lvl;
    a.labels = Object.entries(a.labels).sort((x, y) => y[1] - x[1]).slice(0, 20);
    a.types = Object.entries(a.types).sort((x, y) => y[1] - x[1]);
    a.sensors = Object.entries(a.sensors).sort((x, y) => y[1].n - x[1].n).map(([device, s]) => ({
      device, n: s.n, last: s.last, category: s.cat || 'other', label: s.pl || '', unit: s.pu || '', vmin: s.vmin ?? null, vmax: s.vmax ?? null,
      vavg: s.vn ? Math.round((s.vsum / s.vn) * 100) / 100 : null, batt: s.batt ?? null }));
  }
  return { days, days_available: available.size, addons: out };
}

// -- publishing, history ------------------------------------------------------------------------------------------------------
function axPublish(addon, pair, ts) {
  const [ev, summary] = pair;
  const e = { ...ev, addon, ts: Math.round(ts * 1000) / 1000, summary, id: ax.nextId++ };
  axRecord(e);
  const ring = e.kind === 'vdl2' && e.layer === 'link' ? ax.link : ax.ring;
  ring.push(e);
  if (ring.length > (ring === ax.ring ? 3000 : 600)) ring.shift();
  (ax.counts[addon] ||= { slots: 0, events: 0 }).events += 1;
  if (e.kind === 'aprs' || e.kind === 'acars') ax.counts[addon].slots = Math.max(ax.counts[addon].slots, Math.ceil(ax.counts[addon].events / 3));
  axNotify(e);
  return e;
}

// A week of past traffic and the last hours as messages, so Messages and Statistics have something to show from the first visit.
function axSeed() {
  const now = Date.now() / 1000, r = axRng(20261116);
  const traffic = (hour) => { const h = (hour / 3600) % 24; return 0.3 + 0.7 * Math.max(0, Math.sin((Math.PI * (h - 4)) / 16)); };
  const keepFrom = now - 3 * 3600;
  const add = (addon, pair, ts) => {     // history goes straight to the statistics; only the last hours become messages
    const e = { ...pair[0], addon, ts, summary: pair[1], id: 0 };
    axRecord(e);
    if (ts >= keepFrom && !(e.kind === 'vdl2' && e.layer === 'link')) { e.id = ax.nextId++; ax.ring.push(e); }
  };
  for (let t = Math.floor(now / 3600) * 3600 - AX_HISTORY_DAYS * 86400; t < now; t += 3600) {
    const f = traffic(t), end = Math.min(t + 3600, now);
    const times = (perHour) => Array.from({ length: Math.round(perHour * f * (0.7 + r() * 0.6)) }, () => t + r() * (end - t)).sort((a, b) => a - b);
    times(16).forEach((ts) => add('acars', axAcars(r, ts), ts));
    times(22).forEach((ts) => add('aprs', axAprs(r), ts));
    times(50).forEach((ts) => add('vdl2', axVdl2(r, ts, 'acars'), ts));
    times(30).forEach((ts) => add('vdl2', axVdl2(r, ts, 'data'), ts));
    axBucket(t, 'vdl2').link += Math.round(260 * f * (0.7 + r() * 0.6));
    axBucket(t, 'vdl2').last = Math.max(axBucket(t, 'vdl2').last, end - 30);
    // rtl_433 ran at night (an hour of daylight on the first and last day as well, so the last run is recent)
    const hourOfDay = (t / 3600) % 24;
    const night = hourOfDay >= 22 || hourOfDay < 2, lastRun = t > now - 3 * 3600 && t < now - 1.2 * 3600;
    const limit = Math.min(end, now - 1.2 * 3600);       // the last run ended a while ago
    if (night || lastRun) for (const dev of axSensors()) for (let ts = t + r() * dev.every; ts < limit; ts += dev.every * (0.85 + r() * 0.3)) add('rtl_433', axSensor(dev, r, ts), ts);
  }
  ax.ring.sort((a, b) => a.ts - b.ts);
  ax.ring.forEach((e) => { e.id = ax.nextId++; });
  ax.link = [];
}
const AX_HISTORY_DAYS = 7;

// -- running, new messages while on --------------------------------------------------------------------------------------------------
const axOn = (id) => st.config.addons.enabled.includes(id);
function axTick(now) {
  if (!ax.genAt) { ax.genAt = now; return; }
  const dt = Math.min(now - ax.genAt, 60);
  ax.genAt = now;
  if (dt <= 0) return;
  axSchedule();
  const r = Math.random;
  if (sim.running) {      // the built-in decoders hear only what the scanner stops on
    if (axOn('acars') && r() < dt / 40) axPublish('acars', axAcars(r, now), now);
    if (axOn('aprs') && r() < dt / 30) axPublish('aprs', axAprs(r), now);
  }
  if (ax.running === 'vdl2') {
    if (r() < dt / 14) axPublish('vdl2', axVdl2(r, now, 'acars'), now);
    if (r() < dt / 9) axPublish('vdl2', axVdl2(r, now, 'data'), now);
    if (r() < dt / 0.9) axPublish('vdl2', axVdl2(r, now, 'link'), now);
  }
  if (ax.running === 'rtl_433') {
    axSensors().forEach((dev, i) => {
      if (ax.next[i] === undefined) ax.next[i] = now + r() * Math.min(dev.every, 12);
      if (now >= ax.next[i]) { axPublish('rtl_433', axSensor(dev, r, now), now); ax.next[i] = now + dev.every * (0.85 + r() * 0.3); }
    });
  }
}

function axRun(id, byTimer = false) {
  const spec = FX.addons.status.find((s) => s.id === id);
  if (!spec || spec.kind !== 'external') throw new Error('No such add-on');
  if (ax.running) throw new Error(tr('Another add-on is running - stop it first.'));
  ax.resume = sim.running;
  if (sim.running) stopScanner();
  ax.running = id; ax.since = Date.now() / 1000; ax.next = []; ax.byTimer = byTimer;
  addLog(tr('{name} started - the scanner is paused while it runs.', { name: spec.name }));
}
function axStop(bySchedule = false) {
  if (!ax.running) return;
  if (ax.byTimer && !bySchedule) ax.suppressed = true;
  ax.running = null; ax.since = 0; ax.byTimer = false;
  if (ax.resume) { ax.resume = false; startScanner(); }
}
// A schedule entry can run an add-on instead of scanning.
function axSchedule() {
  const c = st.config;
  const wanted = c.schedule.enabled ? (activeIndices(c).map((i) => c.schedule.entries[i]).find((e) => e.addon) || {}).addon || '' : '';
  if (wanted && !ax.running && !ax.suppressed) { try { axRun(wanted, true); } catch { ax.suppressed = true; } }
  if (!wanted) ax.suppressed = false;
  if (ax.running && ax.byTimer && ax.running !== wanted) axStop(true);
}

function axStatus() {
  const a = st.config.addons;
  return FX.addons.status.map((s) => {
    const ext = s.kind === 'external';
    return { ...s, summary: tr(s.summary), license: tr(s.license), enabled: a.enabled.includes(s.id), installed: true, program: ext ? `/usr/bin/${s.id === 'vdl2' ? 'dumpvdl2' : 'rtl_433'}` : '',
      running: ax.running === s.id, frequencies: AX_FIELD[s.id] ? [...a[AX_FIELD[s.id]]] : [], receiver: a.receivers[s.id] ?? (ext ? 0 : -1),
      options: { ...s.options, ...(a.options[s.id] || {}) }, path: a.paths[s.id] || '', extra_args: a.extra_args[s.id] || '', install_hint: '', managed: false,
      can_install: false, install: null, stats: { slots: 0, events: 0, ...(ax.counts[s.id] || {}) }, error: '' };
  });
}
const axResponse = () => ({ addons: axStatus(), running: ax.running || '' });

function axConfigure(id, patch) {
  const spec = FX.addons.status.find((s) => s.id === id);
  if (!spec) throw new Error('No such add-on');
  const a = st.config.addons;
  if ('enabled' in patch) a.enabled = [...a.enabled.filter((x) => x !== id), ...(patch.enabled ? [id] : [])];
  if ('frequencies' in patch && AX_FIELD[id]) a[AX_FIELD[id]] = [...new Set(patch.frequencies.map(Math.round).filter((f) => f >= 1e6 && f <= 6e9))].sort((x, y) => x - y);
  if ('options' in patch && spec.options && Object.keys(spec.options).length) {
    const known = Object.keys(spec.options), out = { ...(a.options[id] || {}) };
    for (const [k, v] of Object.entries(patch.options)) if (known.includes(k)) out[k] = v;
    a.options = { ...a.options, [id]: out };
  }
  if ('receiver' in patch) a.receivers = { ...a.receivers, [id]: +patch.receiver };
  if ('path' in patch) a.paths = { ...a.paths, [id]: String(patch.path).trim() };
  if ('extra_args' in patch) a.extra_args = { ...a.extra_args, [id]: String(patch.extra_args).trim() };
  if ('listen_to_data' in patch) a.listen_to_data = !!patch.listen_to_data;
  save();
}

function axSince(after, addon) {
  return [...ax.ring, ...ax.link].filter((e) => e.id > after && (!addon || e.addon === addon)).sort((a, b) => a.id - b.id).slice(-500);
}

// -- notifications: rules are checked against the simulated messages, but nothing is ever sent -----------------------------------
const AX_MASK = '••••••••', AX_SECRETS = ['token', 'password', 'header_value'];
const axNotifyMasked = (channels) => channels.map((c) => ({ ...c, settings: Object.fromEntries(Object.entries(c.settings).map(([k, v]) => [k, AX_SECRETS.includes(k) && v ? AX_MASK : v])) }));
const axNotifyState = () => ({ enabled: st.config.notify.enabled, channels: axNotifyMasked(st.config.notify.channels), rules: st.config.notify.rules, recent: ax.notices.slice(-30) });
function axNotifySave(body) {
  const n = st.config.notify, old = Object.fromEntries(n.channels.map((c) => [c.id, c]));
  const rnd = () => Math.random().toString(16).slice(2, 10);
  n.channels = (body.channels || []).slice(0, 20).map((c) => ({ ...c, id: c.id || rnd(), settings: Object.fromEntries(Object.entries(c.settings || {}).map(([k, v]) => [k, v === AX_MASK && old[c.id] ? old[c.id].settings[k] : v])) }));
  const ids = new Set(n.channels.map((c) => c.id));
  n.rules = (body.rules || []).slice(0, 50).map((r) => ({ ...r, id: r.id || rnd(), channels: (r.channels || []).filter((id) => ids.has(id)), cooldown_s: Math.min(86400, Math.max(30, +r.cooldown_s || 300)) }));
  n.enabled = !!body.enabled;
  save();
  return axNotifyState();
}
function axNotify(e) {
  const n = st.config.notify;
  if (!n.enabled) return;
  const text = [e.summary, e.text, e.registration, e.flight, e.source, e.model, e.label, e.src, e.dst].filter(Boolean).join(' ').toLowerCase();
  for (const rule of n.rules) {
    if (!rule.enabled || rule.source !== 'message' || (rule.addons.length && !rule.addons.includes(e.addon))) continue;
    const words = (rule.text_any || '').toLowerCase().split(',').map((w) => w.trim()).filter(Boolean);
    if (words.length && !words.some((w) => text.includes(w))) continue;
    if (rule.who && !`${e.registration || ''} ${e.flight || ''} ${e.source || ''} ${e.model || ''} #${e.device_id} ${e.src || ''}`.toLowerCase().includes(rule.who.toLowerCase())) continue;
    if (rule.label && e.label !== rule.label) continue;
    if (rule.freq_mhz && Math.abs((e.freq_hz || 0) - rule.freq_mhz * 1e6) > 4200) continue;
    if (rule.metric) {
      const v = e.kind === 'sensor' ? (e.fields || {})[rule.metric] : undefined;
      if (typeof v !== 'number' || !(rule.op === '<' ? v < rule.value : v > rule.value)) continue;
    }
    const key = `${rule.id}|${e.registration || e.source || e.model || e.freq_hz}`;
    if ((ax.cool.get(key) || 0) + rule.cooldown_s * 1000 > Date.now()) continue;
    ax.cool.set(key, Date.now());
    const results = Object.fromEntries(n.channels.filter((c) => rule.channels.includes(c.id) && c.enabled).map((c) => [c.name, `error: ${tr('Not available in the online demo - download FalconSDR to use it.')}`]));
    ax.notices.push({ id: ++ax.seq, ts: Math.round(Date.now()) / 1000, rule: rule.name, title: rule.name, message: e.summary.slice(0, 200), source: 'message', results });
    if (ax.notices.length > 100) ax.notices.shift();
  }
}

// What every receiver does now, for the Schedule page.
function axScheduleNow() {
  const c = st.config;
  if (!c.schedule.enabled) return [];
  const devs = 1 + (c.device.extra_devices || []).length;
  const idx = activeIndices(c);
  const names = idx.map((i) => c.schedule.entries[i].name || `${c.schedule.entries[i].start}–${c.schedule.entries[i].end}`);
  return Array.from({ length: devs }, (_, k) => {
    const addon = ax.running && k === 0 ? ax.running : '';
    return { receiver: k, addon, by_schedule: !!(addon && ax.byTimer), scanning: !addon, memory: !addon && !!c.memory_list_enabled, bands: addon ? [] : c.bands.filter((b) => b.enabled).map((b) => b.label), entries: names };
  });
}

// Minimal stored-only ZIP builder for multi-file downloads in the online demo.
const zipJobs = new Map();
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[i] = c >>> 0;
  }
  return t;
})();
function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function buildZip(entries) {
  const enc = new TextEncoder();
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const { name, data } of entries) {
    const nb = enc.encode(name);
    const crc = crc32(data);
    const lh = new ArrayBuffer(30 + nb.length);
    const lv = new DataView(lh);
    lv.setUint32(0, 0x04034b50, true); lv.setUint16(4, 20, true);
    lv.setUint32(14, crc, true); lv.setUint32(18, data.length, true); lv.setUint32(22, data.length, true);
    lv.setUint16(26, nb.length, true);
    new Uint8Array(lh, 30).set(nb);
    locals.push(new Uint8Array(lh), data);
    const ch = new ArrayBuffer(46 + nb.length);
    const cv = new DataView(ch);
    cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true);
    cv.setUint32(16, crc, true); cv.setUint32(20, data.length, true); cv.setUint32(24, data.length, true);
    cv.setUint16(28, nb.length, true); cv.setUint32(42, offset, true);
    new Uint8Array(ch, 46).set(nb);
    centrals.push(new Uint8Array(ch));
    offset += 30 + nb.length + data.length;
  }
  const cdSize = centrals.reduce((a, b) => a + b.length, 0);
  const eocd = new ArrayBuffer(22);
  const ev = new DataView(eocd);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, entries.length, true); ev.setUint16(10, entries.length, true);
  ev.setUint32(12, cdSize, true); ev.setUint32(16, offset, true);
  const out = new Uint8Array(offset + cdSize + 22);
  let pos = 0;
  for (const p of [...locals, ...centrals, new Uint8Array(eocd)]) { out.set(p, pos); pos += p.length; }
  return out;
}

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
  'GET /api/addons': () => axResponse(),
  'GET /api/addons/events': (req, q) => ({ events: axSince(+(q.get('since') || 0), q.get('addon') || '') }),
  'GET /api/addons/stats': (req, q) => axSnapshot(+(q.get('days') || 7)),
  'GET /api/addons/protocols': () => ({ protocols: AX_PROTOCOLS }),
  'GET /api/addons/metrics': () => ({ metrics: FX.addons.metrics }),
  'POST /api/addons/configure': async (req) => { const b = await body(req); axConfigure(String(b.id || ''), b); return axResponse(); },
  'POST /api/addons/run': async (req) => { axRun(String((await body(req)).id || '')); return axResponse(); },
  'POST /api/addons/stop': () => { axStop(); return axResponse(); },
  'POST /api/addons/install': notInDemo,
  'POST /api/addons/uninstall': notInDemo,
  'GET /api/notify': () => axNotifyState(),
  'GET /api/notify/recent': (req, q) => ({ items: ax.notices.filter((n) => n.id > +(q.get('since') || 0)) }),
  'POST /api/notify/save': async (req) => axNotifySave(await body(req)),
  'POST /api/notify/test': () => ({ error: tr('Not available in the online demo - download FalconSDR to use it.') }),
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
  'POST /api/library/favorite_state': async (req) => {
    const ids = new Set((await body(req)).ids || []);
    const matched = st.library.filter((e) => ids.has(e.id));
    return { count: matched.length, favorites: matched.filter((e) => e.favorite).length };
  },
  'POST /api/library/zip': async (req) => {
    const ids = new Set((await body(req)).ids || []);
    const matched = st.library.filter((e) => ids.has(e.id));
    if (!matched.length) throw new Error('No recordings selected');
    const token = Math.random().toString(16).slice(2) + Date.now().toString(16);
    if (zipJobs.size > 8) zipJobs.clear();
    zipJobs.set(token, matched.map((e) => e.id));
    return { token, count: matched.length, bytes: matched.reduce((a, e) => a + sizeOf(e), 0) };
  },
  'GET /api/library/zip': async (req, q) => {
    const ids = zipJobs.get(q.get('token') || '');
    if (!ids) return fail(404, 'Not found');
    zipJobs.delete(q.get('token'));
    const entries = [];
    for (const id of ids) {
      const e = st.library.find((x) => x.id === id);
      if (e) entries.push({ name: e.id.split('/').pop(), data: await wavOf(e) });
    }
    const zip = buildZip(entries);
    return new Response(zip, { headers: { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="falconsdr-recordings.zip"', 'Content-Length': String(zip.length) } });
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
    axStop(true);
    stopScanner();
    st = freshState(null);
    log.length = 0;
    wavCache.clear();
    zipJobs.clear();
    ax.ring.length = 0;
    ax.link.length = 0;
    ax.hours.clear();
    ax.notices.length = 0;
    ax.counts = {};
    ax.cool.clear();
    ax.suppressed = false;
    axSeed();
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
