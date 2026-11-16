// "Messages": what the add-on decoders found - ACARS and APRS from the scanner, rtl_433 sensors and VDL2
// from their programs. Newest first; repeats are folded into one row with a counter; VDL2 link-layer chatter
// is hidden unless asked for; sensors are shown as one card per device. Also used by the Listen page while an
// external program holds the receiver (eventLine).
import { api, fmtTime, h, mount, seg, t } from '../core.js';
import { S, cfg, on } from '../state.js';

export const NAMES = () => ({ acars: 'ACARS', aprs: 'APRS', rtl_433: t('Sensors'), vdl2: 'VDL2' });
const MAX_ROWS = 800;
const REPEAT_WINDOW_S = 180;

// How much of VDL2 to show: only ACARS messages (the text), those and other data frames, or everything
// including the acknowledgements and polls that make up most of the traffic.
const LEVELS = () => [['acars', t('Messages')], ['data', t('Messages and data')], ['all', t('All frames')]];
export const vdl2Level = () => { try { return localStorage.getItem('vdl2Level') || 'acars'; } catch { return 'acars'; } };
export const passes = (e) => e.kind !== 'vdl2' || vdl2Level() === 'all' || (vdl2Level() === 'data' ? e.layer !== 'link' : e.layer === 'acars');
export function levelControl(onchange) {
  return seg(LEVELS(), vdl2Level(), (v) => { try { localStorage.setItem('vdl2Level', v); } catch { /* private window */ } onchange(); });
}

const pref = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v === '1'; } catch { return d; } };
const setPref = (k, v) => { try { localStorage.setItem(k, v ? '1' : '0'); } catch { /* private window */ } };

// -- readings of a sensor, with units ------------------------------------------------------------------
const UNITS = {
  temperature_C: (v) => `${(+v).toFixed(1)} °C`, temperature_F: (v) => `${(+v).toFixed(1)} °F`, humidity: (v) => `${v} %`,
  moisture: (v) => `${v} %`, pressure_hPa: (v) => `${v} hPa`, pressure_kPa: (v) => `${v} kPa`,
  wind_avg_km_h: (v) => `${v} km/h`, wind_max_km_h: (v) => `${t('gusts')} ${v} km/h`, wind_dir_deg: (v) => `${v}°`,
  rain_mm: (v) => `${v} mm`, light_lux: (v) => `${v} lx`, uv: (v) => `UV ${v}`, battery_V: (v) => `${v} V`,
};
const SKIP = new Set(['id', 'channel', 'battery_ok', 'mic', 'time', 'model', 'rssi', 'snr', 'noise', 'freq', 'freq1', 'freq2', 'subtype', 'protocol']);

export function sensorReadings(fields) {
  const out = [];
  for (const [k, fmt] of Object.entries(UNITS)) if (fields[k] !== undefined) out.push([k, fmt(fields[k])]);
  for (const [k, v] of Object.entries(fields)) {
    if (!(k in UNITS) && !SKIP.has(k) && typeof v === 'number' && out.length < 5) out.push([k, `${k.replace(/_/g, ' ')} ${v}`]);
  }
  return out;
}
const batteryChip = (f) => (f.battery_ok === undefined ? null
  : h('span.chip', { class: f.battery_ok ? 'good' : 'warn' }, f.battery_ok ? t('battery OK') : t('battery low')));
const deviceName = (e) => `${e.model}${e.device_id !== undefined && e.device_id !== null ? ` #${e.device_id}` : ''}${e.channel !== undefined && e.channel !== null ? ` · ${t('ch')} ${e.channel}` : ''}`;

// -- one line --------------------------------------------------------------------------------------------
const mapLink = (e) => (e.lat !== undefined && e.lon !== undefined
  ? h('a', { href: `https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=12/${e.lat}/${e.lon}`, target: '_blank', rel: 'noopener', onclick: (ev) => ev.stopPropagation() }, t('Map')) : null);

function body(e) {
  if (e.kind === 'sensor') {
    return [h('b', deviceName(e)), ...sensorReadings(e.fields || {}).map(([, text]) => h('span', text)), batteryChip(e.fields || {})];
  }
  if ((e.kind === 'acars' || e.kind === 'vdl2') && (e.registration || e.text)) {
    return [h('b', e.registration || ''), e.flight ? h('span.muted', e.flight) : null, e.label ? h('span.chip', e.label) : null,
      e.text ? h('span.mono.msg-text', e.text) : null];
  }
  if (e.kind === 'vdl2') return [h('span.muted', `${e.src || '?'} → ${e.dst || '?'}`), h('span.chip', e.frame_type || '?')];
  if (e.kind === 'aprs') {
    const where = e.lat !== undefined ? `${(+e.lat).toFixed(4)}, ${(+e.lon).toFixed(4)}` : '';
    return [h('b', e.source), h('span.muted', e.type || ''), where ? h('span', where) : null, e.comment || e.text ? h('span', e.comment || e.text) : null, mapLink(e)];
  }
  return [h('span', e.summary)];
}

export function eventLine(e, count = 1) {
  const names = NAMES();
  return h('div.row.msg-line', { style: { gap: '10px', alignItems: 'baseline', flexWrap: 'wrap' } },
    h('span.mono.small.muted', fmtTime(new Date(e.ts * 1000).toISOString())),
    h('span.chip', names[e.addon] || e.addon),
    e.freq_hz ? h('span.mono.small.muted', (e.freq_hz / 1e6).toFixed(4)) : null,
    ...body(e), count > 1 ? h('span.chip.accent', `×${count}`) : null);
}

// -- grouping --------------------------------------------------------------------------------------------
export function fold(events) {
  const open = new Map();
  const groups = [];
  for (const e of events) {   // oldest first
    const key = `${e.addon}|${e.summary}`;
    const g = open.get(key);
    if (g && e.ts - g.last.ts <= REPEAT_WINDOW_S) { g.count += 1; g.last = e; g.times.push(e.ts); continue; }
    const fresh = { last: e, count: 1, times: [e.ts] };
    open.set(key, fresh);
    groups.push(fresh);
  }
  return groups;
}

export function messagesView(root) {
  let rows = [];
  let lastId = 0;
  let filter = '';
  let query = '';
  let paused = false;
  let timer = null;
  let fold_ = pref('msgFold', true);
  const open = new Set();

  const list = h('div.msg-list');
  const chips = h('div.row.wrap');
  const options = h('div.row.wrap');
  const search = h('input', { type: 'search', placeholder: t('Search…'), oninput: (e) => { query = e.target.value.toLowerCase(); draw(); } });
  const pauseBtn = h('button.ghost', { onclick: () => { paused = !paused; pauseBtn.textContent = paused ? t('Resume') : t('Pause'); } }, t('Pause'));
  const clearBtn = h('button.ghost', { onclick: async () => { await api.post('/api/addons/clear', {}); rows = []; draw(); } }, t('Clear'));
  const banner = h('div');
  mount(root, h('div.page', h('div.page-head', h('h1', t('Messages'))),
    h('section.card', banner, h('div.row.wrap', chips, h('span.spacer'), search, pauseBtn, clearBtn), options, list)));

  const visible = passes;

  function drawChips() {
    const names = NAMES();
    const counts = {};
    for (const r of rows) if (visible(r)) counts[r.addon] = (counts[r.addon] || 0) + 1;
    const ids = Object.keys(names).filter((id) => counts[id] || cfg().addons.enabled.includes(id));
    mount(chips, ...['', ...ids].map((id) => h('button', { class: id === filter ? 'primary' : 'ghost', onclick: () => { filter = id; draw(); } },
      id ? `${names[id]}${counts[id] ? ` · ${counts[id]}` : ''}` : t('All'))));
    const hasVdl2 = rows.some((r) => r.kind === 'vdl2') && (!filter || filter === 'vdl2');
    mount(options,
      hasVdl2 ? h('div.row', h('span.small.muted', 'VDL2'), levelControl(draw)) : null,
      filter === 'rtl_433' ? null : h('label.switch.small', h('input', { type: 'checkbox', checked: fold_, onchange: (e) => { fold_ = e.target.checked; setPref('msgFold', fold_); draw(); } }),
        h('span.label', t('Fold repeats'))));
  }

  function sensorCards(events) {
    const devices = new Map();
    for (const e of events) {
      const key = deviceName(e) + e.model;
      const d = devices.get(key) || { last: e, count: 0, history: [] };
      d.last = e; d.count += 1; d.history.push(e);
      devices.set(key, d);
    }
    const cards = [...devices.values()].sort((a, b) => b.last.ts - a.last.ts).map((d) => {
      const f = d.last.fields || {};
      const readings = sensorReadings(f);
      const [main, ...rest] = readings;
      const id = `s${d.last.model}${d.last.device_id}${d.last.channel}`;
      return h('div.sensor', { tabindex: 0, role: 'button', onclick: () => { if (open.has(id)) open.delete(id); else open.add(id); draw(); } },
        h('div.row', h('b', deviceName(d.last)), h('span.spacer'), batteryChip(f)),
        h('div.big', main ? main[1] : '—'),
        h('div.small.muted', rest.map((r) => r[1]).join(' · ')),
        h('div.faint.small', t('last {time} · {n} readings', { time: fmtTime(new Date(d.last.ts * 1000).toISOString()), n: d.count })),
        open.has(id) ? h('div.sensor-history', ...d.history.slice(-10).reverse().map((e) => h('div.small.mono', `${fmtTime(new Date(e.ts * 1000).toISOString())}  ${sensorReadings(e.fields || {}).map((r) => r[1]).join(' · ')}`))) : null);
    });
    return h('div.sensor-grid', ...cards);
  }

  function draw() {
    drawChips();
    const shown = rows.filter((r) => visible(r) && (!filter || r.addon === filter) && (!query || JSON.stringify(r).toLowerCase().includes(query)));
    if (filter === 'rtl_433' && shown.length) { mount(list, sensorCards(shown)); return; }
    const groups = (fold_ ? fold(shown) : shown.map((e) => ({ last: e, count: 1, times: [e.ts] }))).slice(-MAX_ROWS).reverse();
    mount(list, ...(groups.length ? groups.map((g) => {
      const e = g.last;
      const toggleOpen = () => { if (open.has(e.id)) open.delete(e.id); else open.add(e.id); draw(); };
      const detail = open.has(e.id) ? h('pre.msg-detail',
        (g.count > 1 ? `${t('Received {n} times:', { n: g.count })} ${g.times.map((x) => fmtTime(new Date(x * 1000).toISOString())).join(', ')}\n\n` : '')
        + JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !['id', 'summary'].includes(k))), null, 2)) : null;
      return h('div.msg', { class: open.has(e.id) ? 'open' : '', tabindex: 0, role: 'button', onclick: toggleOpen,
        onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleOpen(); } } },
      eventLine(e, g.count), detail);
    }) : [h('div.empty', t('Nothing decoded yet. Turn an add-on on in Add-ons and scan a band that contains its channels.'))]));
  }

  async function poll() {
    if (paused) return;
    try {
      const r = await api.get(`/api/addons/events?since=${lastId}`);
      if (r.events.length) {
        rows = rows.concat(r.events).slice(-MAX_ROWS * 3);
        lastId = r.events[r.events.length - 1].id;
        draw();
      }
    } catch { /* the next poll tries again */ }
  }

  const drawBanner = () => mount(banner, S.live && S.live.addon
    ? h('p', { style: { margin: '0 0 8px' } }, h('span.chip.good.live', h('span.dot'), t('{name} is using the receiver', { name: NAMES()[S.live.addon] || S.live.addon })), ' ',
      h('a', { href: '#/addons' }, t('Stop it in Add-ons'))) : null);
  const off = on('live', drawBanner);
  draw();
  drawBanner();
  poll();
  timer = setInterval(poll, 2000);
  return { destroy() { clearInterval(timer); if (off) off(); } };
}
