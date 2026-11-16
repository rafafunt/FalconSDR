// "Listen": the live scanner - what's on air now, controls, spectrum, quick
// settings, the scanned bands and the activity feed.
import { api, confirmDialog, errorToast, fmtChannel, fmtMHz, h, icon, modal, mount, select, seg, slider, t, toast, toggle } from '../core.js';
import { BOOST_STEPS_DB, liveAudio } from '../live-audio.js';
import { S, cfg, confirmRecording, on, patchConfig, setFastPolling } from '../state.js';
import { NAMES, eventLine, fold, levelControl, passes } from './messages.js';
import { askCustomRange, askSingleFrequency, bandEditor, bandRange, clearOverrides, hasOverrides, presetPicker } from './bands.js';

// Windows needs the WinUSB driver on the dongle (SDR++, SDR# and friends need the
// same one, so most users already have it) - without it the device never opens.
const isWindows = () => S.settings.capabilities.platform === 'win32';

// How a receiver is opened (config.device.driver): the label is chosen here, at the point of use, so it follows the language.
const methodLabel = (m) => ({
  soapy: t('SoapySDR'), librtlsdr: t('librtlsdr'), rtl_tcp: t('rtl_tcp'),
  libhackrf: t('libhackrf (experimental)'), libairspy: t('libairspy (experimental)'),
}[m] || m);
const stripMethod = (label) => label.replace(/ \((librtlsdr|libhackrf|libairspy)\)/, '');
const FAMILY_NAMES = { hackrf: 'HackRF', airspy: 'Airspy', airspyhf: 'Airspy HF+' };
export function driverHelp() {
  return modal(t('RTL-SDR driver for Windows'), h('div.col', { style: { gap: '10px' } },
    h('p', t('Windows only talks to an RTL-SDR dongle through the WinUSB driver, installed once with the free Zadig tool. If SDR++, SDR# or another SDR program already works with this dongle, the driver is already there.')),
    h('ol', { style: { margin: 0, paddingLeft: '20px', display: 'flex', flexDirection: 'column', gap: '6px' } },
      h('li', t('Plug in the dongle (skip the driver Windows offers to install).')),
      h('li', t('Download Zadig from '), h('a', { href: 'https://zadig.akeo.ie/', target: '_blank', rel: 'noopener' }, 'zadig.akeo.ie'), t(' and run it (allow the administrator prompt).')),
      h('li', t('Menu Options → List All Devices.')),
      h('li', t('Pick “Bulk-In, Interface (Interface 0)” or “RTL2838UHIDIR” from the list - not Interface 1, and not your keyboard, mouse or headset.')),
      h('li', t('Make sure the target driver on the right is WinUSB, then click Replace Driver (or Install Driver) and wait.')),
      h('li', t('Unplug the dongle, plug it back in and press Start here.'))),
    h('p.small', t('The driver is tied to the USB port - plugging the dongle into a different port may need the same steps again.'))));
}

export function stateLabel(state) {
  return {
    IDLE: t('Stopped'), CALCULATE_NEXT_FREQ: t('Scanning'), TUNING: t('Scanning'), SCANNING: t('Scanning'),
    SIGNAL_DETECTED: t('Signal'), LINGER: t('Linger'), RECONNECTING: t('Reconnecting'),
  }[state] || state;
}

function stateClass(live) {
  if (!live.running) return '';
  if (live.state === 'SIGNAL_DETECTED') return 'good live';
  if (live.state === 'LINGER') return 'warn';
  if (live.state === 'RECONNECTING') return 'bad';
  return 'accent';
}

export function listenView(root) {
  setFastPolling(true);
  const offs = [];
  const page = h('div.page');
  mount(root, page);

  // ---------------------------------------------------------- hero
  const stateChip = h('span.chip.state');
  const freqEl = h('div.freq', '—');
  const nameEl = h('div.freq-name');
  const others = h('div.others');
  const driverHint = h('div.row.wrap.hidden', { style: { gap: '8px', color: '#ff9f0a' } },
    t('Is the dongle connected? On Windows it needs the WinUSB driver.'),
    h('button.small', { onclick: () => driverHelp() }, t('How to install it')));
  const methodHint = h('div.row.wrap.hidden', { style: { gap: '8px', color: '#ff9f0a' } });
  const meterFill = h('div.fill');
  const meterMark = h('div.mark');
  const levelText = h('span.mono');
  const cycleText = h('span.faint.small');

  const startBtn = h('button.big.primary', { onclick: () => toggleScan() });
  const listenBtn = h('button.big', { onclick: () => liveAudio.toggle().catch(errorToast) });
  const volume = h('input', { type: 'range', min: 0, max: 1.5, step: 0.05, value: liveAudio.volume, oninput: (e) => liveAudio.setVolume(+e.target.value), title: t('Volume') });
  const boost = select(BOOST_STEPS_DB.map((db) => [db, db ? `+${db} dB` : t('No boost')]), liveAudio.boostDb, (v) => liveAudio.setBoost(+v),
    { style: { width: 'auto' }, title: t('Louder quiet voices on a phone or a weak speaker - loud peaks are still held back') });
  const skipBtn = h('button', { onclick: () => command('skip'), title: t('Leave this frequency for now') }, icon('skip'), t('Skip'));
  const blBtn = h('button', { onclick: () => command('blacklist'), title: t('Leave it and never stop on it again (adds it to the blacklist)') }, icon('ban'), t('Skip + blacklist'));
  const holdBtn = h('button', { onclick: () => command('hold', { enabled: !(S.live && S.live.hold) }), title: t('Stay on this frequency - the scanner stops moving on') }, icon('pause'), t('Hold'));
  const recBtn = h('button.recbtn', { onclick: () => toggleManualRecord(), title: t('Continuous recording of what you hear, including silence') }, icon('record'), t('Record'));
  const iqBtn = h('button.ghost.small', { onclick: () => toggleRawIq(), title: t('Raw wideband IQ (SigMF) - about 19 MB per second') }, t('Raw IQ'));
  const autoRec = toggle(t('Auto-record transmissions'), cfg().recording.enabled, async (v, e) => {
    if (v && !(await confirmRecording())) { e.target.checked = false; return; }
    patchConfig({ recording: { enabled: v } });
  });

  const dongleRow = h('div.row.wrap', { style: { gap: '6px' } });
  // Bands/channels that no dongle's antenna range reaches (live.uncovered, antenna_range.py).
  const rangeNotice = h('div.notice.hidden', { role: 'status' }, icon('ban'), h('span', { style: { flex: '1 1 260px' } }));
  const hero = h('section.card.hero',
    h('div.row', stateChip, h('span.spacer'), cycleText),
    dongleRow,
    h('div', freqEl, nameEl),
    driverHint,
    methodHint,
    rangeNotice,
    others,
    h('div.col', { style: { gap: '4px' } },
      h('div.meter', meterFill, meterMark),
      h('div.meter-legend', h('span', '-100'), levelText, h('span', '0 dBFS'))),
    h('div.controls', startBtn, h('div.row', listenBtn, h('div.volume', icon('volume'), volume)), skipBtn, blBtn, holdBtn, recBtn),
    h('div.row.wrap', autoRec, h('span.spacer'), h('span.faint.small', t('Boost')), boost, iqBtn));

  // ---------------------------------------------------------- an external add-on (rtl_433, VDL2) holds the receiver
  // The scanner is paused then, and there is no spectrum (the program owns the dongle): show that, what the
  // program decodes right now, and the way back.
  const addonRecent = h('div.col');
  const addonTitle = h('div.row.wrap');
  const addonNote = h('p.muted', { style: { margin: '4px 0 8px' } });
  const addonFilter = h('div.row.wrap');
  const addonBars = h('div.col', { style: { gap: '4px' } });
  const addonPanel = h('section.card.addon-panel.hidden',
    addonTitle,
    addonNote,
    h('div.row.wrap', h('button.primary.big', { onclick: async () => { try { await api.post('/api/addons/stop', {}); } catch (e) { errorToast(e); } } }, icon('stop'), t('Stop and resume scanning')),
      h('a.btn.ghost', { href: '#/messages' }, t('Open Messages'))),
    addonFilter, addonBars, addonRecent);
  let addonPoll = null;
  let addonLastId = 0;
  let addonSeen = [];
  let addonName = '';

  async function pollAddon() {
    const ids = ((S.live && S.live.addons) || []).map((a) => a.id);
    if (!ids.length) return;
    try {
      const r = await api.get(`/api/addons/events?since=${addonLastId}`);
      if (r.events.length) {
        addonLastId = r.events[r.events.length - 1].id;
        addonSeen = addonSeen.concat(r.events.filter((e) => ids.includes(e.addon))).slice(-200);
      }
    } catch { return; }
    drawAddon();
  }

  function drawAddon() {
    const live = S.live;
    if (!live || !live.addon) return;
    const names = NAMES();
    const since = live.addon_since ? Math.max(0, Math.round(Date.now() / 1000 - live.addon_since)) : 0;
    const clock = `${Math.floor(since / 60)}:${String(since % 60).padStart(2, '0')}`;
    const shown = addonSeen.filter(passes);
    addonNote.textContent = live.running
      ? t('This program has one receiver; the others keep scanning. What it decodes appears here and in Messages.')
      : t('The scanner is paused: this program has the receiver. What it decodes appears here and in Messages.');
    mount(addonFilter, live.addon === 'vdl2' ? levelControl(drawAddon) : null);
    // Which channels are busy (a small stand-in for a spectrum): messages per frequency in the last 5 minutes.
    const recent = addonSeen.filter((e) => e.freq_hz && e.ts > Date.now() / 1000 - 300);
    const perFreq = new Map();
    for (const e of recent) {
      const c = perFreq.get(e.freq_hz) || { n: 0, sum: 0, levels: 0 };
      c.n += 1;
      if (typeof e.sig_level === 'number') { c.sum += e.sig_level; c.levels += 1; }
      perFreq.set(e.freq_hz, c);
    }
    const top = Math.max(1, ...[...perFreq.values()].map((c) => c.n));
    mount(addonBars, ...[...perFreq.entries()].sort((a, b) => a[0] - b[0]).map(([f, c]) => h('div.row', { style: { gap: '10px' } },
      h('span.mono.small.muted', { style: { width: '70px' } }, (f / 1e6).toFixed(3)),
      h('div.chan-bar', h('div', { style: { width: `${Math.max(3, 100 * c.n / top)}%` } })),
      h('span.small.muted', { style: { minWidth: '90px', textAlign: 'right' } }, c.levels ? `${c.n} · ${(c.sum / c.levels).toFixed(0)} dB` : String(c.n)))));
    const running = (live.addons || []).map((a) => names[a.id] || a.id);
    mount(addonTitle, h('span.chip.good.live', h('span.dot'), running.length > 1 ? t('{name} are using receivers', { name: running.join(', ') }) : t('{name} is using the receiver', { name: names[live.addon] || live.addon })),
      h('span.faint.small', t('running for {time}', { time: clock })), h('span.spacer'),
      h('span.faint.small', t('{n} messages decoded', { n: addonSeen.length })));
    const groups = fold(shown).slice(-8).reverse();
    mount(addonRecent, ...(groups.length ? groups.map((g) => eventLine(g.last, g.count))
      : [h('div.empty', t('Waiting for the first message…'))]));
  }

  function syncAddonPanel() {
    const id = S.live && S.live.addon;
    // Several receivers: the others keep scanning, so the scanner stays on screen next to the panel.
    const paused = !!id && !(S.live && S.live.running);
    hero.classList.toggle('hidden', paused);
    spectrumCard.classList.toggle('hidden', paused);
    addonPanel.classList.toggle('hidden', !id);
    if (id && !addonPoll) { addonLastId = 0; addonSeen = []; addonName = id; pollAddon(); addonPoll = setInterval(pollAddon, 2000); }
    if (!id && addonPoll) { clearInterval(addonPoll); addonPoll = null; }
    if (id) drawAddon();
  }

  // ---------------------------------------------------------- spectrum
  const canvas = h('canvas', { height: 260, role: 'img', 'aria-label': t('Live spectrum and waterfall of the scanned band. The frequency and state are shown in text above.') });
  const axis = h('div.spectrum-axis');
  const spectrumCard = h('section.card.spectrum-card',
    h('div.card-head', h('h2', t('Spectrum')), h('span.faint.small', t('Yellow line: trigger level'))),
    h('div.spectrum-wrap', canvas), axis);
  const spectrum = spectrumPainter(canvas, axis);

  // ---------------------------------------------------------- bands
  const bandsBody = h('div.col');
  const bandsCard = h('section.card', h('div.card-head', h('h2', t('What to scan'))), bandsBody);
  const renderBands = () => mount(bandsBody, bandsSection());

  // ---------------------------------------------------------- right column
  const quick = h('section.card');
  const device = h('section.card');
  const feed = h('div.feed');
  const feedCard = h('section.card', h('div.card-head', h('h2', t('Activity'))), feed);

  mount(page, h('div.page-head', h('h1', t('Listen'))), h('div.grid.listen',
    h('div.col', { style: { gap: '16px' } }, addonPanel, hero, spectrumCard, bandsCard),
    h('div.col', { style: { gap: '16px' } }, quick, device, feedCard)));

  // ---------------------------------------------------------- behaviour
  async function toggleScan() {
    try {
      if (S.live && S.live.running) await api.post('/api/scanner/stop');
      else {
        await api.post('/api/scanner/start');
        if (!liveAudio.playing && localStorage.getItem('autolisten') !== '0') liveAudio.start().catch(() => {});
      }
    } catch (e) { errorToast(e); }
  }

  async function command(name, body = {}) {
    try { await api.post('/api/scanner/' + name, body); } catch (e) { errorToast(e); }
  }

  async function toggleManualRecord() {
    const active = S.live && S.live.manual_recording;
    if (!active && !(await confirmRecording())) return;
    command('manual_record', { enabled: !active });
  }

  async function toggleRawIq() {
    const active = S.live && S.live.raw_iq;
    if (!active && !(await confirmRecording())) return;
    command('raw_iq', { enabled: !active });
  }

  function renderRangeNotice(live) {
    const items = (live && live.uncovered) || [];
    rangeNotice.classList.toggle('hidden', !items.length);
    if (!items.length) return;
    const parts = items.map((it) => {
      if (it.kind === 'channels') return t('Memory List channels: {n} (e.g. {freq} MHz)', { n: it.count, freq: it.examples.map((f) => fmtMHz(f, 3)).join(', ') });
      const whole = it.gaps.length === 1 && it.gaps[0][0] === it.start_hz && it.gaps[0][1] === it.stop_hz;
      if (it.label && whole) return it.label;  // the name usually carries the range already
      const gaps = it.gaps.map(([a, b]) => `${fmtMHz(a, 3)}–${fmtMHz(b, 3)}`).join(', ');
      return it.label ? `${it.label} (${gaps} MHz)` : `${gaps} MHz`;
    });
    rangeNotice.querySelector('span').textContent =
      `${t('Not scanned - outside the antenna range of every receiver:')} ${parts.join('; ')}. ${t('Change the range under SDR device.')}`;
  }

  function renderLive() {
    const live = S.live;
    if (!live) return;
    syncAddonPanel();
    stateChip.className = 'chip state ' + stateClass(live);
    driverHint.classList.toggle('hidden', !(live.state === 'RECONNECTING' && isWindows()));
    const here = S.settings.devices.find((d) => d.driver === S.settings.config.device.driver && d.args === S.settings.config.device.device_args);
    const other = here && live.state === 'RECONNECTING' ? S.settings.devices.find((d) => d.group && d.group === here.group && d !== here) : null;
    methodHint.classList.toggle('hidden', !other);
    if (other) methodHint.textContent = t('The receiver did not open with {method}. Stop the scanner and try {other} under SDR device → Access method.', { method: methodLabel(here.method), other: methodLabel(other.method) });
    renderRangeNotice(live);
    mount(stateChip, h('span.dot'), live.stopping ? t('Stopping…') : stateLabel(live.state));
    const [primary, ...rest] = live.frequencies || [];
    const active = live.state === 'SIGNAL_DETECTED' || live.state === 'LINGER';
    if (primary) {
      mount(freqEl, fmtChannel(primary[0]), h('small', 'MHz'));
      freqEl.title = `${fmtMHz(primary[0], 4)} MHz`;  // the carrier, for the curious
      nameEl.textContent = primary[1] || '';
    } else {
      mount(freqEl, '—');
      nameEl.textContent = live.running ? '' : t('Press Start to begin scanning');
    }
    // Several dongles: each one's own state, so a stuck or idle one is visible.
    mount(dongleRow, (live.dongles || []).map((d, i) => h('span.chip', { class: stateClass({ ...d, running: true }) }, `SDR ${i + 1}: ${stateLabel(d.state)}`)));
    mount(others, active ? rest.map(([f, n]) => h('span.chip', h('b.mono', fmtChannel(f)), n ? ` ${n}` : '')) : []);
    const pct = (v) => Math.max(0, Math.min(100, (v + 100)));
    meterFill.style.width = `${pct(live.level)}%`;
    const trig = live.spectrum ? live.spectrum.trigger : cfg().trigger_level_dbfs;
    meterMark.style.left = `${pct(trig)}%`;
    levelText.textContent = live.running ? `${live.level.toFixed(1)} dBFS` : '';
    cycleText.textContent = live.running && live.cycle_time ? t('Back to this window: {s} s ago', { s: live.cycle_time.toFixed(1) }) : '';
    mount(startBtn, icon(live.running ? 'stop' : 'play'), live.running ? t('Stop') : t('Start'));
    startBtn.className = 'big ' + (live.running ? 'danger' : 'primary');
    startBtn.disabled = !!live.stopping;
    [skipBtn, blBtn, holdBtn, recBtn, iqBtn].forEach((b) => { b.disabled = !live.running; });
    holdBtn.classList.toggle('on', !!live.hold);
    mount(holdBtn, icon(live.hold ? 'play' : 'pause'), live.hold ? t('Resume scanning') : t('Hold'));
    recBtn.classList.toggle('on', !!live.manual_recording);
    mount(recBtn, icon('record'), live.manual_recording ? t('Stop recording') : t('Record'));
    iqBtn.textContent = live.raw_iq ? t('Stop raw IQ') : t('Raw IQ');
    iqBtn.classList.toggle('on', !!live.raw_iq);
    spectrum.draw(live.spectrum);
    gainSlider && gainSlider.input && (gainSlider.input.min = live.gain_range[0], gainSlider.input.max = live.gain_range[1]);
    lockDevice(live.running);
    renderFeed();
  }

  function renderAudio() {
    listenBtn.classList.toggle('on', liveAudio.playing);
    mount(listenBtn, icon(liveAudio.playing ? 'headphones' : 'mute'), liveAudio.playing ? t('Listening') : t('Listen'));
  }

  let lastFeedId = -1;
  function renderFeed() {
    const last = S.log.length ? S.log[S.log.length - 1].id : 0;
    if (last === lastFeedId) return;
    lastFeedId = last;
    mount(feed, [...S.log].reverse().slice(0, 40).map((e) =>
      h('div', h('time', new Date(e.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })), h('span', e.text))));
  }

  // ---------------------------------------------------------- quick settings
  let gainSlider = null;
  function renderQuick() {
    const c = cfg();
    const gainMode = c.device.gain_mode;
    const range = S.live ? S.live.gain_range : [0, 50];
    gainSlider = slider({ name: gainMode === 'sw_auto' ? t('Starting gain') : t('Gain'), min: range[0], max: range[1], step: 0.1, value: c.device.gain_db, unit: 'dB', oninput: (v) => patchConfig({ device: { gain_db: v } }) });
    const squelch = slider({ name: t('Squelch below trigger'), min: -40, max: 0, step: 1, value: c.squelch_offset_db, unit: 'dB', oninput: (v) => patchConfig({ squelch_offset_db: v }) });
    squelch.input.disabled = !c.squelch_enabled;
    const nr = slider({ name: t('Noise reduction strength'), min: 0, max: 100, step: 1, value: Math.round(c.noise_reduction.strength * 100), unit: '%', oninput: (v) => patchConfig({ noise_reduction: { strength: v / 100 } }) });
    nr.input.disabled = !c.noise_reduction.enabled;
    const ownTrigger = c.bands.filter((b) => b.enabled && b.trigger_level_dbfs != null);
    mount(quick,
      h('div.card-head', h('h2', t('Receiver')), h('span.faint.small', t('Changes apply live'))),
      slider({ name: t('Trigger level'), min: -90, max: 0, step: 1, value: c.trigger_level_dbfs, unit: 'dBFS', oninput: (v) => patchConfig({ trigger_level_dbfs: v }) }),
      ownTrigger.length ? h('p.faint.small', t('Bands with their own trigger level ignore this slider: {names}.', { names: ownTrigger.map((b) => b.label || bandRange(b)).join(', ') })) : null,
      h('div.col', { style: { gap: '6px' } },
        toggle(t('Squelch'), c.squelch_enabled, (v) => { patchConfig({ squelch_enabled: v }); squelch.input.disabled = !v; }, { hint: t('Mutes the noise tail after a transmission ends') }),
        squelch),
      h('div.col', { style: { gap: '6px' } },
        h('span.small.muted', t('Gain')),
        seg([['manual', t('Manual')], ['auto', t('Auto (hardware)')], ['sw_auto', t('Auto (software)')]], gainMode, (v) => { patchConfig({ device: { gain_mode: v } }); renderQuick(); }),
        gainMode === 'auto' ? h('span.faint.small', t('The tuner sets its own gain. Can overload on very strong stations - try software auto then.')) : gainSlider),
      slider({ name: t('Tuning time'), min: 0, max: 1000, step: 10, value: c.timing.tuning_time_ms, unit: 'ms', oninput: (v) => patchConfig({ timing: { tuning_time_ms: v } }) }),
      slider({ name: t('Hold after a transmission'), min: 0, max: 30000, step: 100, value: c.timing.linger_time_ms, format: (v) => `${(v / 1000).toFixed(1)} s`, oninput: (v) => patchConfig({ timing: { linger_time_ms: v } }) }),
      h('div.col', { style: { gap: '6px' } },
        toggle(t('Noise reduction'), c.noise_reduction.enabled, (v) => { patchConfig({ noise_reduction: { enabled: v } }); nr.input.disabled = !v; }, { hint: t('Adaptive filter for weak, noisy voice') }),
        nr),
      recencyBoost(c.recency_boost));
  }

  function recencyBoost(rb) {
    const duration = slider({ name: t('For how long'), min: 0, max: 600, step: 5, value: rb.duration_s, format: (v) => `${v} s`, oninput: (v) => patchConfig({ recency_boost: { duration_s: v } }) });
    const interval = slider({ name: t('Extra visit every'), min: 0.5, max: 60, step: 0.5, value: rb.interval_s, format: (v) => `${(+v).toFixed(1)} s`, oninput: (v) => patchConfig({ recency_boost: { interval_s: v } }) });
    const lock = (on) => { duration.input.disabled = interval.input.disabled = !on; };
    lock(rb.enabled);
    return h('div.col', { style: { gap: '6px' } },
      toggle(t('Recency boost'), rb.enabled, (v) => { patchConfig({ recency_boost: { enabled: v } }); lock(v); },
        { hint: t('Come back sooner after a transmission') }),
      duration, interval);
  }

  // ---------------------------------------------------------- device
  // A receiver that is plugged in (USB ids, Linux) but that nothing here can drive: say why instead of an empty list.
  function missingReceiverNotice(devices) {
    const caps = S.settings.capabilities;
    const listed = new Set(devices.filter((d) => !d.missing).map((d) => d.family));
    const missing = (caps.usb_seen || []).filter((f) => !listed.has(f));
    if (!missing.length) return null;
    const name = missing.map((f) => FAMILY_NAMES[f] || f).join(', ');
    return h('span.small', { style: { color: '#ff9f0a' } }, caps.soapy
      ? t('{name} is plugged in but not listed. Install its SoapySDR module (e.g. soapysdr-module-hackrf), allow USB access (udev rules or the plugdev group), then reload this page.', { name })
      : t('{name} is plugged in, but SoapySDR is not available to FalconSDR (the Python module python3-soapysdr is not visible to it). Install python3-soapysdr and soapysdr-module-<your receiver>, then reinstall with install.sh --from-source, or install libhackrf0 / libairspy0 for the experimental native access.', { name }));
  }

  let deviceInputs = [];
  function renderDevice() {
    const c = cfg();
    const devices = S.settings.devices;
    const current = devices.findIndex((d) => d.driver === c.device.driver && d.args === c.device.device_args);
    const here = devices[current >= 0 ? current : 0];
    // One entry per physical receiver, and - when there is more than one way to open it - a second choice.
    const groups = [];
    const groupOf = (d) => d.group || d.driver;   // the online demo's devices have no group
    devices.forEach((d) => {
      let g = groups.find((x) => x.group === groupOf(d));
      if (!g) groups.push(g = { group: groupOf(d), entries: [] });
      g.entries.push(d);
    });
    const myGroup = groups.find((g) => g.group === groupOf(here));
    const patchDevice = (d) => {
      // Another dongle may sit on another antenna - its Bias-T starts off again.
      const patch = { driver: d.driver, device_args: d.args, bias_tee: false };
      if (d.default_rate_hz && ![2.5e6, 3e6, 6e6, 10e6].includes(c.device.sample_rate_hz)) patch.sample_rate_hz = d.default_rate_hz;
      patchConfig({ device: patch });
      renderDevice();
    };
    const takenByExtra = (g) => g.group !== 'rtl_tcp' && (c.device.extra_devices || []).some((x) => g.entries.some((d) => d.driver === x.driver && d.args === x.device_args));
    const sel = select(groups.map((g, i) => [i, stripMethod(g.entries[0].label)]), groups.indexOf(myGroup), (v) => {
      if (takenByExtra(groups[+v])) { toast(t('An extra dongle already uses this receiver.'), 'bad'); renderDevice(); return; }
      patchDevice(groups[+v].entries[0]);
    });
    const methods = myGroup.entries.length > 1
      ? select(myGroup.entries.map((d, i) => [i, methodLabel(d.method)]), myGroup.entries.indexOf(here), (v) => patchDevice(myGroup.entries[+v]))
      : null;
    const bias = biasTeeToggle(c.device.bias_tee, (v) => { patchConfig({ device: { bias_tee: v } }); renderDevice(); });
    // An RTL-SDR on another computer (rtl_tcp): nothing to detect, so its address is typed here. "host" or "host:port";
    // kept in device_args as host=...,port=... - and in the list entry, or the next render would not find the device.
    const netEntry = devices.find((d) => d.driver === 'rtl_tcp');
    const netArgsOf = (args) => Object.fromEntries((args || '').split(',').filter((kv) => kv.includes('=')).map((kv) => kv.split(/=(.*)/s).slice(0, 2)));
    const showAddr = (args) => {
      const a = netArgsOf(args);
      return a.host ? `${a.host.includes(':') ? `[${a.host}]` : a.host}${a.port && a.port !== '1234' ? ':' + a.port : ''}` : '';
    };
    // host, host:port, [ipv6]:port or a bare IPv6 address (more than one colon: all of it is the host) -> "host=...,port=..."
    const parseAddr = (text) => {
      const raw = text.trim().replace(/^tcp:\/\//, '');
      const m = raw.match(/^\[(.+)\](?::(\d+))?$/) || (raw.split(':').length > 2 ? [raw, raw] : raw.match(/^(.*?)(?::(\d+))?$/));
      return m && m[1] ? `host=${m[1]},port=${m[2] || 1234}` : '';
    };
    const addrInput = (args, onset) => h('input', { type: 'text', placeholder: '192.168.1.20:1234', spellcheck: false, autocapitalize: 'off', value: showAddr(args),
      onchange: (e) => onset(parseAddr(e.target.value)) });
    const addr = c.device.driver === 'rtl_tcp' ? addrInput(c.device.device_args, (args) => { if (netEntry) netEntry.args = args; patchConfig({ device: { device_args: args } }); }) : null;
    const bw = h('input', { type: 'number', min: 0.25, max: 61.44, step: 0.01, value: (c.device.sample_rate_hz / 1e6).toFixed(2), onchange: (e) => patchConfig({ device: { sample_rate_hz: +e.target.value * 1e6 } }) });
    // Experimental: more dongles scanning together - each takes its share of the capture windows.
    const extra = c.device.extra_devices || [];
    // An extra dongle must be a specific one - and never a simulation next to real hardware (fake
    // transmissions in the library, part of the band no longer really listened to).
    const sim = c.device.driver === 'simulated';
    const usable = (d) => d.driver !== 'auto' && (d.driver === 'simulated') === sim;
    const extraGroups = groups.filter((g) => g.entries.some(usable));
    const groupOfDevice = (x) => extraGroups.find((g) => g.entries.some((d) => d.driver === x.driver && (d.driver === 'rtl_tcp' || d.args === x.device_args)));
    const setExtra = (list) => { patchConfig({ device: { extra_devices: list } }); renderDevice(); };
    const extraInputs = [];
    // One physical receiver can scan once: the first one and the other extras hold theirs. Network receivers (rtl_tcp) may repeat - each has its own address.
    const heldBy = (except) => new Set([myGroup.group, ...extra.filter((_, j) => j !== except).map((x) => (groupOfDevice(x) || {}).group)].filter((g) => g && g !== 'rtl_tcp'));
    const extraRows = extra.map((x, i) => {
      const mine = groupOfDevice(x);
      const taken = heldBy(i);
      const options = extraGroups.filter((g) => g === mine || !taken.has(g.group));
      const setMine = (patch) => setExtra(extra.map((e, j) => (j === i ? { ...e, ...patch } : e)));
      const pickGroup = (g) => {
        const d = g.entries.find(usable);
        setMine({ driver: d.driver, device_args: d.driver === 'rtl_tcp' ? '' : d.args, bias_tee: false });
      };
      const s = select([...(mine ? [] : [[-1, t('(not connected)')]]), ...options.map((g) => [extraGroups.indexOf(g), stripMethod(g.entries[0].label)])],
        mine ? extraGroups.indexOf(mine) : -1, (v) => pickGroup(extraGroups[+v]));
      const entries = mine ? mine.entries.filter(usable) : [];
      const xMethod = entries.length > 1
        ? select(entries.map((d, k) => [k, methodLabel(d.method)]), Math.max(0, entries.findIndex((d) => d.driver === x.driver)), (v) => setMine({ driver: entries[+v].driver, device_args: entries[+v].args }))
        : null;
      const xAddr = x.driver === 'rtl_tcp' ? addrInput(x.device_args, (args) => setMine({ device_args: args })) : null;
      const del = h('button.icon.ghost', { title: t('Remove'), onclick: () => setExtra(extra.filter((_, j) => j !== i)) }, icon('x'));
      const xBias = biasTeeToggle(!!x.bias_tee, (v) => setMine({ bias_tee: v }));
      extraInputs.push(s, ...(xMethod ? [xMethod] : []), ...(xAddr ? [xAddr] : []), del, xBias.querySelector('input'));
      const xRange = rangeFields(x.range_start_hz || 0, x.range_stop_hz || 0, (p) => setMine(p));
      // Receivers differ: a HackRF can capture 10 MHz where an RTL-SDR takes 2.4. Empty = the same as the first one.
      const xWidth = h('input', {
        type: 'number', min: 0.25, max: 61.44, step: 0.01, placeholder: t('same as the first'), value: x.sample_rate_hz ? (x.sample_rate_hz / 1e6).toFixed(2) : '',
        onchange: (e) => setMine({ sample_rate_hz: e.target.value ? Math.round(+e.target.value * 1e6) : 0 }),
      });
      extraInputs.push(xWidth);
      const twice = extra.some((o, j) => j !== i && o.driver === x.driver && o.device_args === x.device_args) || (x.driver === c.device.driver && x.device_args === c.device.device_args);
      return h('div.col', { style: { gap: '6px' } }, h('label.field', t('Dongle {n}', { n: i + 2 }), h('div.row', s, del)),
        xMethod ? h('label.field', t('Access method'), xMethod) : null,
        xAddr ? h('label.field', t('rtl_tcp address'), xAddr) : null,
        twice ? h('span.small', { style: { color: '#ff9f0a' } }, t('This receiver is already used above - it can only be opened once.')) : null,
        h('label.field', t('Capture width (MHz)'), xWidth), xBias, xRange);
    });
    const add = h('button.ghost.small', {
      onclick: () => {
        const taken = heldBy(-1);
        const g = extraGroups.find((x) => x.group !== 'rtl_tcp' && !taken.has(x.group))
          || (extraGroups.find((x) => x.group === 'rtl_tcp') && ![c.device, ...extra.map((x) => ({ driver: x.driver, device_args: x.device_args }))].some((x) => x.driver === 'rtl_tcp' && !x.device_args) ? extraGroups.find((x) => x.group === 'rtl_tcp') : null);
        if (!g) { toast(t('No other SDR device found - plug it in, then reload this page.'), 'bad'); return; }
        const d = g.entries.find(usable);
        setExtra([...extra, { driver: d.driver, device_args: d.driver === 'rtl_tcp' ? '' : d.args }]);
      },
    }, icon('plus'), t('Scan with another dongle (experimental)'));
    deviceInputs = [sel, ...(methods ? [methods] : []), ...(addr ? [addr] : []), bw, bias.querySelector('input'), add, ...extraInputs];
    mount(device,
      h('div.card-head', h('h2', t('SDR device'))),
      h('div.fields',
        h('label.field', t('Device'), sel),
        methods ? h('label.field', t('Access method'), methods) : null,
        addr ? h('label.field', t('rtl_tcp address'), addr) : null,
        h('label.field', t('Capture width (MHz)'), bw)),
      addr ? h('span.faint.small', t('The RTL-SDR on another computer, through its rtl_tcp server (host or host:port, default port 1234). It serves one program at a time, the stream is not encrypted - use it on a trusted network. Retuning takes longer than with a local dongle, so scanning many windows is slower.')) : null,
      methods ? h('span.faint.small', t('How FalconSDR talks to this receiver. The first choice is the default; the other one is the fallback when it will not open, and a way to compare.')) : null,
      missingReceiverNotice(devices),
      bias,
      rangeFields(c.device.range_start_hz || 0, c.device.range_stop_hz || 0, (p) => { patchConfig({ device: p }); renderDevice(); }),
      ...extraRows,
      extra.length ? h('span.faint.small', t('The scan plan is split between all dongles - each scans its own share of the windows, so a full cycle is that much shorter. You hear one dongle at a time; the others keep recording.')) : null,
      extra.some((x) => x.sample_rate_hz && x.sample_rate_hz !== c.device.sample_rate_hz)
        ? h('span.faint.small', t('Receivers with different capture widths split the scan in proportion to their width, and a wide one joins neighbouring windows into one. The processor must keep up: a 30 ms block of a 10 MHz capture takes about 20 ms to analyse.')) : null,
      add,
      h('span.faint.small.lockhint', t('Stop the scanner to change the device or capture width.')),
      // Picked by USB order (no serial of its own) - that order can change when dongles are replugged.
      devices.some((d) => /(^|,)index=/.test(d.args)) ? h('span.faint.small', t('Some dongles share a serial number (often 00000001) and are told apart only by USB order, which can change after replugging. To keep your choice for good, give each one its own serial with rtl_eeprom -s.')) : null,
      isWindows() ? h('button.ghost.small', { onclick: () => driverHelp() }, t('Dongle not detected? Driver (Zadig) instructions')) : null);
    lockDevice(S.live && S.live.running);
  }
  // Antenna range: optional and per dongle; 0 in the config = no limit on that side. Applies live
  // (the scan plan is rebuilt), so unlike the device itself it stays editable while scanning.
  function rangeFields(startHz, stopHz, set) {
    const mhz = (hz) => (hz ? String(+(hz / 1e6).toFixed(6)) : '');
    const field = (hz, label, key) => h('input', {
      type: 'number', min: 0, step: 0.001, placeholder: t('no limit'), 'aria-label': `${t('Antenna range (MHz)')}: ${label}`, value: mhz(hz),
      onchange: (e) => set({ [key]: Math.max(0, Math.round((+e.target.value || 0) * 1e6)) }),
    });
    return h('div.col', { style: { gap: '4px' } },
      h('label.field', t('Antenna range (MHz)'), h('div.row', field(startHz, t('From'), 'range_start_hz'), h('span.faint', '–'), field(stopHz, t('To'), 'range_stop_hz'))),
      h('span.faint.small', t('Optional. This dongle scans only inside this range - for one on an antenna made for a single band, e.g. 144 to 146 for 2 m. Empty = no limit.')));
  }

  // Off by default: DC on the antenna cable can damage what's on it.
  function biasTeeToggle(on, set) {
    return toggle(t('Bias-T (antenna power)'), on, async (v, e) => {
      if (v && !(await confirmBiasTee())) { e.target.checked = false; return; }
      set(v);
    }, { hint: on ? t('On while scanning - powers an LNA or active antenna') : null });
  }

  async function confirmBiasTee() {
    return (await modal(t('Turn on Bias-T?'), h('div.col', { style: { gap: '10px' } },
      h('p', t('Bias-T sends about 4.5 V DC up the antenna cable to power an LNA or an active antenna.')),
      h('p', t('Turn it on only when such a device is connected and your dongle supports it (e.g. RTL-SDR Blog V3/V4). A DC-shorted antenna, a splitter or other equipment on the cable can be damaged - and so can the dongle.')),
      h('p.small.muted', t('FalconSDR switches it off whenever scanning stops.'))),
    // The safe choice is the prominent one, and last - the button a dialog focuses (Enter):
    // people click the highlighted button, and here that must not be the risky one.
    [[t('Turn on'), true, 'ghost'], [t('Cancel'), false, 'primary']])) === true;
  }

  function lockDevice(running) {
    deviceInputs.forEach((el) => { el.disabled = !!running; });
    const hint = device.querySelector('.lockhint');
    if (hint) hint.classList.toggle('hidden', !running);
  }

  // ---------------------------------------------------------- bands
  function bandsSection() {
    const c = cfg();
    const scheduled = c.schedule.enabled;
    const locked = scheduleLocked();
    const enabledChannels = S.settings.channels.filter((ch) => ch.enabled).length;
    const custom = c.bands.filter(hasOverrides).length;
    return [
      scheduled ? h('div.notice', icon('clock'), h('span', { style: { flex: '1 1 220px' } }, locked
        ? t('Schedule mode is on - the schedule decides what is scanned.')
        : t('Schedule mode is on - these switches apply only outside its time ranges.')),
        h('a', { href: '#/schedule' }, t('Open the schedule →'))) : null,
      custom ? h('div.row.wrap.small.muted',
        t('{n} of {total} bands use their own settings instead of the Receiver panel.', { n: custom, total: c.bands.length }),
        h('button.ghost.small', {
          onclick: async () => {
            if (!(await confirmDialog(t('Use the general settings everywhere'), t('Remove the own settings (trigger, times, gain, noise reduction) of all {n} bands? From then on every band follows the Receiver panel.', { n: custom }), t('Use general settings')))) return;
            c.bands.forEach(clearOverrides);
            patchConfig({ bands: c.bands });
            renderBands();
          },
        }, icon('refresh'), t('Use general settings in all bands'))) : null,
      c.bands.length ? c.bands.map((b, i) => bandRow(b, i)) : h('div.empty', t('Nothing to scan yet - add a band preset or a frequency below.')),
      h('div.row.wrap', presetPicker(addBand),
        h('button', { onclick: async () => addBand(await askCustomRange()) }, icon('plus'), t('Custom range')),
        h('button', { onclick: async () => addBand(await askSingleFrequency()) }, icon('plus'), t('Single frequency')),
        h('a', { href: '#/settings/presets', style: { marginLeft: 'auto' } }, t('Edit band presets →'))),
      h('div.setting',
        h('div', toggle(t('Memory List'), c.memory_list_enabled, (v) => patchConfig({ memory_list_enabled: v }),
          { hint: t('{n} enabled channels', { n: enabledChannels }), disabled: locked })),
        h('a', { href: '#/channels' }, t('Edit channels →'))),
    ];
  }

  // The switches only matter while the schedule can fall back to them.
  function scheduleLocked() {
    const s = cfg().schedule;
    return s.enabled && s.outside !== 'normal';
  }

  function addBand(b) {
    if (!b) return;
    cfg().bands.push(b);
    patchConfig({ bands: cfg().bands });
    renderBands();
  }

  const expanded = new Set();
  function bandRow(b, i) {
    const save = () => patchConfig({ bands: cfg().bands });
    const solo = S.live && S.live.solo_band === i;
    const custom = hasOverrides(b);
    const row = h('div.band', { class: (b.enabled ? '' : 'disabled') + (solo ? ' solo' : '') },
      h('div.band-main',
        h('label.switch', h('input', {
          type: 'checkbox', checked: b.enabled, disabled: scheduleLocked(),
          title: scheduleLocked() ? t('Schedule mode is on') : t('Scan this band'),
          onchange: (e) => { b.enabled = e.target.checked; save(); row.classList.toggle('disabled', !b.enabled); },
        })),
        h('div.grow',
          h('div.title.ellipsis', b.label || t('(unnamed)'), custom ? h('span.chip.accent', { style: { marginLeft: '8px' } }, t('own settings')) : null),
          h('div.range', bandRange(b))),
        h('button.icon.ghost', { class: solo ? 'on' : '', title: solo ? t('Back to scanning everything') : t('Scan only this band for now'), onclick: () => api.post('/api/scanner/solo', { band: solo ? null : i }).catch(errorToast) }, icon('headphones')),
        h('button.icon.ghost', { title: t('Edit'), onclick: () => { expanded.has(i) ? expanded.delete(i) : expanded.add(i); renderBands(); } }, icon(expanded.has(i) ? 'chevron' : 'edit')),
        h('button.icon.ghost', {
          title: t('Remove'), onclick: async () => {
            if (!(await confirmDialog(t('Remove band'), t('Remove “{name}” from the scan list?', { name: b.label || t('(unnamed)') }), t('Remove'), true))) return;
            cfg().bands.splice(i, 1); expanded.clear(); save(); renderBands();
          },
        }, icon('trash'))));
    if (expanded.has(i)) row.append(bandEditor(b, save, renderBands));
    return row;
  }

  // ---------------------------------------------------------- wiring
  renderQuick();
  renderDevice();
  renderBands();
  renderAudio();
  renderLive();
  offs.push(on('live', renderLive), on('audio', renderAudio), on('settings', () => { renderQuick(); renderDevice(); renderBands(); }));
  let lastSolo = S.live && S.live.solo_band;
  offs.push(on('live', (live) => { if (live.solo_band !== lastSolo) { lastSolo = live.solo_band; renderBands(); } }));
  return { destroy() { offs.forEach((f) => f()); setFastPolling(false); spectrum.destroy(); clearInterval(addonPoll); } };
}

// Spectrum line + scrolling waterfall of the current capture window.
function spectrumPainter(canvas, axis) {
  const SPEC_H = 140;
  const ctx = canvas.getContext('2d');
  const water = document.createElement('canvas');
  const wctx = water.getContext('2d');
  let lo = -100, hi = -20, window_ = '';
  let last = null;

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    // Rounded: a canvas width is whole pixels - at 125% zoom an unrounded one never
    // matched, so every frame "resized" and wiped the waterfall.
    const w = Math.round(Math.max(200, canvas.clientWidth) * dpr);
    if (canvas.width !== w) {
      canvas.width = w; canvas.height = 260 * dpr;
      water.width = w; water.height = (260 - SPEC_H) * dpr;
    }
    return dpr;
  }
  const ro = new ResizeObserver(() => { resize(); draw(last, true); });
  ro.observe(canvas);

  function color(v) {  // 0..1 -> dark blue .. cyan .. yellow .. red
    const stops = [[5, 9, 30], [20, 60, 140], [30, 170, 220], [250, 220, 70], [250, 70, 50]];
    const x = Math.max(0, Math.min(0.999, v)) * (stops.length - 1);
    const i = Math.floor(x), f = x - i;
    return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
  }

  function draw(sp, redrawOnly = false) {
    const dpr = resize();
    const W = canvas.width, SH = SPEC_H * dpr;
    ctx.fillStyle = '#050913';
    ctx.fillRect(0, 0, W, canvas.height);
    if (!sp || !sp.db.length) {
      ctx.fillStyle = '#5f6b85';
      ctx.font = `${12 * dpr}px system-ui`;
      ctx.fillText(t('No data - start the scanner'), 14 * dpr, 24 * dpr);
      axis.replaceChildren();
      return;
    }
    last = sp;
    const db = sp.db;
    const key = `${sp.f0}-${sp.f1}`;
    if (key !== window_) { window_ = key; wctx.clearRect(0, 0, water.width, water.height); }
    const mn = Math.min(...db), mx = Math.max(...db, sp.trigger);
    lo += ((mn - 6) - lo) * 0.2;
    hi += ((mx + 8) - hi) * 0.2;
    const y = (v) => SH - ((v - lo) / (hi - lo)) * SH;
    // grid
    ctx.strokeStyle = 'rgba(120,140,180,.12)';
    ctx.lineWidth = 1;
    for (let g = Math.ceil(lo / 10) * 10; g < hi; g += 10) { ctx.beginPath(); ctx.moveTo(0, y(g)); ctx.lineTo(W, y(g)); ctx.stroke(); }
    // spectrum fill + line
    const accent = sp.custom ? '#c084fc' : '#38bdf8';
    const grad = ctx.createLinearGradient(0, 0, 0, SH);
    grad.addColorStop(0, sp.custom ? 'rgba(192,132,252,.45)' : 'rgba(56,189,248,.45)');
    grad.addColorStop(1, 'rgba(56,189,248,0)');
    ctx.beginPath();
    ctx.moveTo(0, SH);
    db.forEach((v, i) => ctx.lineTo((i / (db.length - 1)) * W, y(v)));
    ctx.lineTo(W, SH);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    db.forEach((v, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, (i / (db.length - 1)) * W, y(v)));
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.5 * dpr;
    ctx.stroke();
    // trigger
    ctx.setLineDash([6 * dpr, 4 * dpr]);
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth = 1.2 * dpr;
    ctx.beginPath(); ctx.moveTo(0, y(sp.trigger)); ctx.lineTo(W, y(sp.trigger)); ctx.stroke();
    ctx.setLineDash([]);
    // waterfall: shift down one row, paint the new one on top
    if (!redrawOnly) {
      const row = wctx.createImageData(water.width, 1);
      for (let x = 0; x < water.width; x++) {
        const v = db[Math.floor((x / water.width) * db.length)];
        const [r, g, b] = color((v - lo) / (hi - lo));
        row.data.set([r, g, b, 255], x * 4);
      }
      wctx.drawImage(water, 0, 1 * dpr);
      for (let k = 0; k < Math.max(1, Math.round(dpr)); k++) wctx.putImageData(row, 0, k);
    }
    ctx.drawImage(water, 0, SH);
    const mid = (sp.f0 + sp.f1) / 2;
    axis.replaceChildren(...[sp.f0, mid, sp.f1].map((f) => h('span', `${fmtMHz(f, 3)} MHz`)));
  }

  return { draw: (sp) => draw(sp), destroy: () => ro.disconnect() };
}
