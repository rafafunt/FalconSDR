// "Listen": the live scanner - what's on air now, controls, spectrum, quick
// settings, the scanned bands and the activity feed.
import { api, confirmDialog, errorToast, fmtChannel, fmtMHz, h, icon, modal, mount, select, seg, slider, t, toast, toggle } from '../core.js';
import { liveAudio } from '../live-audio.js';
import { S, cfg, confirmRecording, on, patchConfig, setFastPolling } from '../state.js';
import { askCustomRange, askSingleFrequency, bandEditor, bandRange, clearOverrides, hasOverrides, presetPicker } from './bands.js';

// Windows needs the WinUSB driver on the dongle (SDR++, SDR# and friends need the
// same one, so most users already have it) - without it the device never opens.
const isWindows = () => S.settings.capabilities.platform === 'win32';
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
  const meterFill = h('div.fill');
  const meterMark = h('div.mark');
  const levelText = h('span.mono');
  const cycleText = h('span.faint.small');

  const startBtn = h('button.big.primary', { onclick: () => toggleScan() });
  const listenBtn = h('button.big', { onclick: () => liveAudio.toggle().catch(errorToast) });
  const volume = h('input', { type: 'range', min: 0, max: 1.5, step: 0.05, value: liveAudio.volume, oninput: (e) => liveAudio.setVolume(+e.target.value), title: t('Volume') });
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
  const hero = h('section.card.hero',
    h('div.row', stateChip, h('span.spacer'), cycleText),
    dongleRow,
    h('div', freqEl, nameEl),
    driverHint,
    others,
    h('div.col', { style: { gap: '4px' } },
      h('div.meter', meterFill, meterMark),
      h('div.meter-legend', h('span', '-100'), levelText, h('span', '0 dBFS'))),
    h('div.controls', startBtn, h('div.row', listenBtn, h('div.volume', icon('volume'), volume)), skipBtn, blBtn, holdBtn, recBtn),
    h('div.row.wrap', autoRec, h('span.spacer'), iqBtn));

  // ---------------------------------------------------------- spectrum
  const canvas = h('canvas', { height: 260 });
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
    h('div.col', { style: { gap: '16px' } }, hero, spectrumCard, bandsCard),
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

  function renderLive() {
    const live = S.live;
    if (!live) return;
    stateChip.className = 'chip state ' + stateClass(live);
    driverHint.classList.toggle('hidden', !(live.state === 'RECONNECTING' && isWindows()));
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
    mount(quick,
      h('div.card-head', h('h2', t('Receiver')), h('span.faint.small', t('Changes apply live'))),
      slider({ name: t('Trigger level'), min: -90, max: 0, step: 1, value: c.trigger_level_dbfs, unit: 'dBFS', oninput: (v) => patchConfig({ trigger_level_dbfs: v }) }),
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
  let deviceInputs = [];
  function renderDevice() {
    const c = cfg();
    const devices = S.settings.devices;
    const current = devices.findIndex((d) => d.driver === c.device.driver && d.args === c.device.device_args);
    const sel = select(devices.map((d, i) => [i, d.label]), current >= 0 ? current : 0, (v) => {
      const d = devices[+v];
      // Another dongle may sit on another antenna - its Bias-T starts off again.
      patchConfig({ device: { driver: d.driver, device_args: d.args, bias_tee: false } });
      renderDevice();
    });
    const bias = biasTeeToggle(c.device.bias_tee, (v) => { patchConfig({ device: { bias_tee: v } }); renderDevice(); });
    const bw = h('input', { type: 'number', min: 0.25, max: 61.44, step: 0.01, value: (c.device.sample_rate_hz / 1e6).toFixed(2), onchange: (e) => patchConfig({ device: { sample_rate_hz: +e.target.value * 1e6 } }) });
    // Experimental: more dongles scanning together - each takes its share of the capture windows.
    const extra = c.device.extra_devices || [];
    // An extra dongle must be a specific one - and never a simulation next to real hardware (fake
    // transmissions in the library, part of the band no longer really listened to).
    const sim = c.device.driver === 'simulated';
    const choices = devices.filter((d) => d.driver !== 'auto' && (d.driver === 'simulated') === sim);
    const setExtra = (list) => { patchConfig({ device: { extra_devices: list } }); renderDevice(); };
    const extraInputs = [];
    const extraRows = extra.map((x, i) => {
      const at = choices.findIndex((d) => d.driver === x.driver && d.args === x.device_args);
      const s = select([...(at < 0 ? [[-1, t('(not connected)')]] : []), ...choices.map((d, j) => [j, d.label])], at, (v) => {
        const d = choices[+v];
        setExtra(extra.map((e, j) => (j === i ? { driver: d.driver, device_args: d.args } : e)));
      });
      const del = h('button.icon.ghost', { title: t('Remove'), onclick: () => setExtra(extra.filter((_, j) => j !== i)) }, icon('x'));
      const xBias = biasTeeToggle(!!x.bias_tee, (v) => setExtra(extra.map((e, j) => (j === i ? { ...e, bias_tee: v } : e))));
      extraInputs.push(s, del, xBias.querySelector('input'));
      return h('div.col', { style: { gap: '6px' } }, h('label.field', t('Dongle {n}', { n: i + 2 }), h('div.row', s, del)), xBias);
    });
    const used = new Set([`${c.device.driver}|${c.device.device_args}`, ...extra.map((x) => `${x.driver}|${x.device_args}`)]);
    const add = h('button.ghost.small', {
      onclick: () => {
        const d = choices.find((x) => !used.has(`${x.driver}|${x.args}`));
        if (!d) { toast(t('No other SDR device found - plug it in, then reload this page.'), 'bad'); return; }
        setExtra([...extra, { driver: d.driver, device_args: d.args }]);
      },
    }, icon('plus'), t('Scan with another dongle (experimental)'));
    deviceInputs = [sel, bw, bias.querySelector('input'), add, ...extraInputs];
    mount(device,
      h('div.card-head', h('h2', t('SDR device'))),
      h('div.fields',
        h('label.field', t('Device'), sel),
        h('label.field', t('Capture width (MHz)'), bw)),
      bias,
      ...extraRows,
      extra.length ? h('span.faint.small', t('The scan plan is split between all dongles - each scans its own share of the windows, so a full cycle is that much shorter. You hear one dongle at a time; the others keep recording.')) : null,
      add,
      h('span.faint.small.lockhint', t('Stop the scanner to change the device or capture width.')),
      // Picked by USB order (no serial of its own) - that order can change when dongles are replugged.
      devices.some((d) => /(^|,)index=/.test(d.args)) ? h('span.faint.small', t('Some dongles share a serial number (often 00000001) and are told apart only by USB order, which can change after replugging. To keep your choice for good, give each one its own serial with rtl_eeprom -s.')) : null,
      isWindows() ? h('button.ghost.small', { onclick: () => driverHelp() }, t('Dongle not detected? Driver (Zadig) instructions')) : null);
    lockDevice(S.live && S.live.running);
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
    // The safe choice is the prominent one, and last - the button a dialog focuses (Enter).
    [[t('Turn on'), true, 'ghost'], [t('Cancel'), false, 'danger solid']])) === true;
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
  return { destroy() { offs.forEach((f) => f()); setFastPolling(false); spectrum.destroy(); } };
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
