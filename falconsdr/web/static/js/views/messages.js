// "Messages": what the add-on decoders found - ACARS and APRS from the scanner, rtl_433 sensors and VDL2
// from their programs. Newest first; repeats are folded into one row with a counter; VDL2 link-layer chatter
// is hidden unless asked for; sensors are shown as one card per device. Also used by the Listen page while an
// external program holds the receiver (eventLine).
import { api, fmtTime, h, icon, mount, select, seg, t } from '../core.js';
import { S, cfg, on } from '../state.js';

export const NAMES = () => ({ acars: 'ACARS', aprs: 'APRS', tetra: 'TETRA', rtl_433: t('Sensors'), vdl2: 'VDL2' });
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
// The server names and classifies what a device measures (addons/sensors.py): e.readings = [{key, label, unit, value}],
// e.status = [{key, value}] (door open, alarm...), e.category, e.primary. Older stored events only have e.fields.
const LEGACY_UNITS = { temperature_C: ['Temperature', '°C'], temperature_F: ['Temperature', '°F'], humidity: ['Humidity', '%'], pressure_kPa: ['Pressure', 'kPa'],
  pressure_hPa: ['Pressure', 'hPa'], wind_avg_km_h: ['Wind', 'km/h'], wind_dir_deg: ['Wind direction', '°'], rain_mm: ['Rain', 'mm'] };
const CATEGORY = () => ({ thermometer: t('Thermometer'), weather: t('Weather station'), tpms: t('Tyre pressure'), energy: t('Power and energy'),
  meter: t('Utility meter'), security: t('Door, motion or alarm'), remote: t('Remote control'), tank: t('Tank level'), soil: t('Soil'),
  air: t('Air quality'), lightning: t('Lightning'), other: t('Device') });
const readingText = (r) => `${r.value}${r.unit ? ' ' + r.unit : ''}`;
const isPlain = (r) => ['Temperature', 'Humidity'].includes(r.label.split(' ')[0]);

export function readingsOf(e) {
  if (Array.isArray(e.readings)) return e.readings;
  return Object.entries(LEGACY_UNITS).filter(([k]) => (e.fields || {})[k] !== undefined)
    .map(([k, [label, unit]]) => ({ key: k, label, unit, value: e.fields[k] }));
}
const statusChip = (x) => h('span.chip', { class: ['alarm', 'tamper', 'leaking', 'water'].includes(x.key) && +x.value ? 'warn' : '' }, `${x.key} ${x.value}`);
const batteryChip = (e) => {
  const v = (e.fields || {}).battery_ok;
  return v === undefined ? null : h('span.chip', { class: v ? 'good' : 'warn' }, v ? t('battery OK') : t('battery low'));
};
const deviceName = (e) => `${e.model}${e.device_id !== undefined && e.device_id !== null ? ` #${e.device_id}` : ''}${e.channel !== undefined && e.channel !== null ? ` · ${t('ch')} ${e.channel}` : ''}`;

// -- one line --------------------------------------------------------------------------------------------
const mapLink = (e) => (e.lat !== undefined && e.lon !== undefined
  ? h('a', { href: `https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=12/${e.lat}/${e.lon}`, target: '_blank', rel: 'noopener', onclick: (ev) => ev.stopPropagation() }, t('Map')) : null);

function body(e) {
  if (e.kind === 'sensor') {
    return [h('b', deviceName(e)), ...readingsOf(e).slice(0, 4).map((r) => h('span', isPlain(r) ? readingText(r) : `${t(r.label)} ${readingText(r)}`)),
      ...(e.status || []).slice(0, 3).map(statusChip), batteryChip(e)];
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
  if (e.kind === 'tetra') {
    const sub = e.subtype || '';
    if (sub === 'sync') {
      return [h('b', `MCC ${e.mcc} / MNC ${e.mnc}`), h('span.chip', `CC ${e.cc}`), h('span.muted', `TS ${e.ts || 1}`)];
    }
    if (sub === 'sysinfo') {
      return [h('b', `MCC ${e.mcc} / MNC ${e.mnc}`), h('span.chip', `LA ${e.la}`),
        h('span.chip', { class: e.encrypted ? 'warn' : 'good' }, e.encrypted ? t('Encrypted network') : t('Clear network'))];
    }
    if (sub === 'call') {
      return [h('b', `GSSI ${e.gssi || 0}`), e.issi ? h('span.muted', `← ISSI ${e.issi}`) : null,
        h('span.chip', e.event || 'call'), h('span.muted', `TS ${e.ts || 1}`)];
    }
    if (sub === 'encrypted') {
      return [h('b', e.gssi ? `GSSI ${e.gssi}` : t('Encrypted')), h('span.chip.warn', t('Encrypted (muted)')),
        h('span.muted', `TS ${e.ts || 1}`)];
    }
    if (sub === 'sds') {
      return [h('b', `ISSI ${e.src || 0}`), h('span.muted', `→ ${e.dst || 0}`), h('span.chip', 'SDS'),
        e.text ? h('span.mono.msg-text', e.text) : null];
    }
    if (sub === 'lip') {
      const where = e.lat !== undefined ? `${(+e.lat).toFixed(4)}, ${(+e.lon).toFixed(4)}` : '';
      return [h('b', `ISSI ${e.src || 0}`), h('span.chip', 'LIP'), where ? h('span', where) : null, mapLink(e)];
    }
  }
  return [h('span', e.summary)];
}

export function whoOf(e) {
  if (e.kind === 'sensor') return `${e.model} #${e.device_id}`;      // as the summary spells it, so the search finds it
  if (e.kind === 'tetra') {
    if (e.gssi) return `GSSI ${e.gssi}`;
    if (e.src) return `ISSI ${e.src}`;
    if (e.mcc !== undefined) return `MCC ${e.mcc} / MNC ${e.mnc}`;
  }
  return e.registration || e.source || e.src || e.model || '';
}

// onPick(text): clicking the aircraft / station / sensor name filters by it (Messages page).
export function eventLine(e, count = 1, onPick = null) {
  const names = NAMES();
  const parts = body(e);
  const who = whoOf(e);
  if (onPick && who && parts[0] && parts[0].tagName === 'B') {
    parts[0] = h('b.pick', { title: t('Show only this'), tabindex: 0, role: 'button', onclick: (ev) => { ev.stopPropagation(); onPick(who); },
      onkeydown: (ev) => { if (ev.key === 'Enter') { ev.stopPropagation(); onPick(who); } } }, parts[0].textContent);
  }
  return h('div.row.msg-line', { style: { gap: '10px', alignItems: 'baseline', flexWrap: 'wrap' } },
    h('span.mono.small.muted', fmtTime(new Date(e.ts * 1000).toISOString())),
    h('span.chip', names[e.addon] || e.addon),
    e.freq_hz ? h('span.mono.small.muted', (e.freq_hz / 1e6).toFixed(4)) : null,
    ...parts, count > 1 ? h('span.chip.accent', `×${count}`) : null);
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

const prefS = (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } };
const setPrefS = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private window */ } };

export function messagesView(root) {
  let rows = [];
  let lastId = 0;
  let filter = '';
  let query = '';
  let paused = false;
  let timer = null;
  let fold_ = pref('msgFold', true);
  let range = +prefS('msgRange', '3600');        // seconds back; 0 = everything
  let sort = prefS('msgSort', 'new');            // new | old | count
  let view = prefS('msgView', 'items');          // items | who
  let freq = '';
  let clearedAt = +prefS('msgClearedAt', '0');   // "Clear list" hides what came before, here only - nothing is deleted
  let freqSig = '';
  const open = new Set();

  const list = h('div.msg-list');
  const chips = h('div.row.wrap');
  const levelRow = h('div.row.wrap');
  const hidden = h('div');
  const search = h('input', { type: 'search', placeholder: t('Search…'), oninput: (e) => { query = e.target.value.toLowerCase(); draw(); } });
  const pauseBtn = h('button.ghost', { onclick: () => { paused = !paused; pauseBtn.textContent = paused ? t('Resume') : t('Pause'); } }, t('Pause'));
  const clearBtn = h('button.ghost', { title: t('Hides the messages received so far from this list. Nothing is deleted.'),
    onclick: () => { clearedAt = Date.now(); setPrefS('msgClearedAt', String(clearedAt)); draw(); } }, t('Clear list'));
  const rangeSel = select([['300', t('Last 5 minutes')], ['3600', t('Last hour')], ['86400', t('Last 24 hours')], ['0', t('Everything')]], String(range),
    (v) => { range = +v; setPrefS('msgRange', v); draw(); }, { style: { width: 'auto' }, 'aria-label': t('Time range') });
  const sortSel = select([['new', t('Newest first')], ['old', t('Oldest first')], ['count', t('Most messages first')]], sort,
    (v) => { sort = v; setPrefS('msgSort', v); draw(); }, { style: { width: 'auto' }, 'aria-label': t('Sort') });
  const viewSeg = seg([['items', t('Messages')], ['who', t('By aircraft / station')]], view, (v) => { view = v; setPrefS('msgView', v); draw(); });
  let freqSel = h('span');
  const foldBox = h('label.switch.small', h('input', { type: 'checkbox', checked: fold_, onchange: (e) => { fold_ = e.target.checked; setPref('msgFold', fold_); draw(); } }),
    h('span.label', t('Fold repeats')));
  const banner = h('div');
  const toolbar = h('div.row.wrap', { style: { gap: '10px' } }, viewSeg, rangeSel, sortSel, freqSel, foldBox, levelRow);
  const controls = h('div.col', { style: { gap: '12px' } }, h('div.row.wrap', chips, h('span.spacer'), search, pauseBtn, clearBtn), toolbar, hidden);
  const intro = h('div.msg-intro');
  mount(root, h('div.page', h('div.page-head', h('h1', t('Messages'))),
    h('section.card', banner, controls, intro, list)));

  // Nothing received yet: say what this page is and where to switch the decoders on.
  function drawIntro() {
    const names = NAMES();
    const on_ = cfg().addons.enabled.map((id) => names[id] || id);
    mount(intro,
      h('h2', { style: { margin: '0 0 6px' } }, t('What is this page?')),
      h('p', { style: { margin: '0 0 8px' } }, t('Whatever the add-on decoders read appears here: aircraft text messages (ACARS and VDL2), amateur radio position reports and messages (APRS) and readings of weather stations and sensors (rtl_433).')),
      h('p.muted', { style: { margin: '0 0 12px' } }, on_.length
        ? t('Switched on: {list}. Waiting for the first message - the scanner has to visit their channels.', { list: on_.join(', ') })
        : t('No add-on is switched on yet.')),
      h('button.primary', { onclick: () => { location.hash = '#/addons'; } }, icon('addons'), on_.length ? t('Add-on settings') : t('Go to Add-ons and switch one on')));
  }

  const pick = (who) => { query = who.toLowerCase(); search.value = who; draw(); };
  const handed = sessionStorage.getItem('msgQuery');       // from the statistics: "show me this aircraft"
  if (handed) { sessionStorage.removeItem('msgQuery'); range = 0; rangeSel.value = '0'; query = handed.toLowerCase(); search.value = handed; }

  // -- what is shown ---------------------------------------------------------------------------------------
  function selected() {
    const since = range ? Date.now() / 1000 - range : 0;
    return rows.filter((e) => passes(e) && e.ts * 1000 > clearedAt && e.ts >= since && (!filter || e.addon === filter)
      && (!freq || String(e.freq_hz) === freq) && (!query || JSON.stringify(e).toLowerCase().includes(query)));
  }

  function drawChips(base) {
    const names = NAMES();
    const counts = {};
    for (const r of base) counts[r.addon] = (counts[r.addon] || 0) + 1;
    const ids = Object.keys(names).filter((id) => counts[id] || cfg().addons.enabled.includes(id) || filter === id);
    mount(chips, ...['', ...ids].map((id) => h('button', { class: id === filter ? 'primary' : 'ghost', onclick: () => { filter = id; draw(); } },
      id ? `${names[id]}${counts[id] ? ` · ${counts[id]}` : ''}` : t('All'))));
  }

  function drawToolbar() {
    // The frequency list follows what has been received; rebuilt only when it changes (an open list must not close).
    const freqs = [...new Set(rows.filter((e) => e.freq_hz).map((e) => e.freq_hz))].sort((a, b) => a - b);
    const sig = freqs.join(',');
    if (sig !== freqSig) {
      freqSig = sig;
      const next = select([['', t('All frequencies')], ...freqs.map((f) => [String(f), (f / 1e6).toFixed(4)])], freq,
        (v) => { freq = v; draw(); }, { style: { width: 'auto' }, 'aria-label': t('Frequency') });
      freqSel.replaceWith(next);
      freqSel = next;
    }
    freqSel.classList.toggle('hidden', freqs.length < 2);
    const hasVdl2 = rows.some((r) => r.kind === 'vdl2') && (!filter || filter === 'vdl2');
    mount(levelRow, hasVdl2 ? h('div.row', h('span.small.muted', 'VDL2'), levelControl(draw)) : null);
    foldBox.classList.toggle('hidden', filter === 'rtl_433' || view === 'who');
    const n = rows.filter((e) => e.ts * 1000 <= clearedAt).length;
    mount(hidden, n ? h('p.small.muted', { style: { margin: '8px 0 0' } }, t('{n} earlier messages are hidden.', { n }), ' ',
      h('a', { href: '#', onclick: (ev) => { ev.preventDefault(); clearedAt = 0; setPrefS('msgClearedAt', '0'); draw(); } }, t('Show them'))) : null);
  }

  function sensorCards(events) {
    const devices = new Map();
    for (const e of events) {
      const key = deviceName(e) + e.model;
      const d = devices.get(key) || { last: e, count: 0, history: [] };
      d.last = e; d.count += 1; d.history.push(e);
      devices.set(key, d);
    }
    const order = { new: (a, b) => b.last.ts - a.last.ts, old: (a, b) => a.history[0].ts - b.history[0].ts, count: (a, b) => b.count - a.count };
    const cards = [...devices.values()].sort(order[sort]).map((d) => {
      const e = d.last;
      const readings = readingsOf(e);
      const main = readings.find((r) => r.key === e.primary) || readings[0];
      const rest = readings.filter((r) => r !== main).slice(0, 4);
      const id = `s${e.model}${e.device_id}${e.channel}`;
      const toggleSensor = () => { if (open.has(id)) open.delete(id); else open.add(id); draw(); };
      return h('div.sensor', { tabindex: 0, role: 'button', onclick: toggleSensor,
        onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleSensor(); } } },
        h('div.row', h('b', deviceName(e)), h('span.spacer'), batteryChip(e)),
        h('div.faint.small', CATEGORY()[e.category || 'other'] || ''),
        main ? h('div.big', `${isPlain(main) ? '' : t(main.label) + ' '}${readingText(main)}`) : h('div.row.wrap', { style: { gap: '4px', minHeight: '34px' } }, ...(e.status || []).map(statusChip)),
        h('div.small.muted', rest.map((r) => `${isPlain(r) ? '' : t(r.label) + ' '}${readingText(r)}`).join(' · ')),
        main && (e.status || []).length ? h('div.row.wrap', { style: { gap: '4px' } }, ...e.status.map(statusChip)) : null,
        h('div.faint.small', t('last {time} · {n} readings', { time: fmtTime(new Date(e.ts * 1000).toISOString()), n: d.count })),
        open.has(id) ? h('div.sensor-history', { onclick: (ev) => ev.stopPropagation() }, ...d.history.slice(-10).reverse().map((x) => h('div.small.mono', `${fmtTime(new Date(x.ts * 1000).toISOString())}  ${readingsOf(x).map(readingText).join(' · ')} ${(x.status || []).map((y) => `${y.key} ${y.value}`).join(' ')}`))) : null);
    });
    return h('div.sensor-grid', ...cards);
  }

  // One row per aircraft / station: how many messages, the latest one, and the whole thread on a click.
  function whoRows(events) {
    const people = new Map();
    for (const e of events) {
      const who = whoOf(e) || NAMES()[e.addon];
      const g = people.get(who) || { who, events: [], addons: new Set() };
      g.events.push(e); g.addons.add(e.addon);
      people.set(who, g);
    }
    const order = { new: (a, b) => b.events[b.events.length - 1].ts - a.events[a.events.length - 1].ts, old: (a, b) => a.events[0].ts - b.events[0].ts, count: (a, b) => b.events.length - a.events.length };
    return [...people.values()].sort(order[sort]).map((g) => {
      const last = g.events[g.events.length - 1];
      const id = `w${g.who}`;
      const toggleOpen = () => { if (open.has(id)) open.delete(id); else open.add(id); draw(); };
      return h('div.msg', { class: open.has(id) ? 'open' : '', tabindex: 0, role: 'button', onclick: toggleOpen,
        onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleOpen(); } } },
      h('div.row', { style: { gap: '10px', alignItems: 'baseline', flexWrap: 'wrap' } },
        h('b', g.who), last.flight ? h('span.muted', last.flight) : null, h('span.chip.accent', String(g.events.length)),
        h('span.mono.small.muted', fmtTime(new Date(last.ts * 1000).toISOString())), h('span.grow.muted.small', last.summary)),
      open.has(id) ? h('div.col', { style: { gap: '2px', marginTop: '6px' }, onclick: (ev) => ev.stopPropagation() }, ...fold(g.events).slice(-30).reverse().map((x) => eventLine(x.last, x.count))) : null);
    });
  }

  function draw() {
    const empty = !rows.length;
    controls.classList.toggle('hidden', empty);
    intro.classList.toggle('hidden', !empty);
    if (empty) { drawIntro(); mount(list); return; }
    const base = selected();
    drawChips(rows.filter((e) => passes(e) && e.ts * 1000 > clearedAt));
    drawToolbar();
    if (!base.length) {
      // Something was received, only not in the chosen time range: say so and offer the way out.
      const older = range && !filter && !query && !freq ? rows.filter((e) => passes(e) && e.ts * 1000 > clearedAt).length : 0;
      mount(list, older
        ? h('div.empty', h('p', t('Nothing in this time range, but {n} older messages are stored.', { n: older })),
          h('button.primary', { onclick: () => { range = 0; rangeSel.value = '0'; setPrefS('msgRange', '0'); draw(); } }, t('Show everything')))
        : h('div.empty', rows.length ? t('Nothing matches the filters.') : t('Nothing decoded yet. Turn an add-on on in Add-ons and scan a band that contains its channels.')));
      return;
    }
    if (filter === 'rtl_433') { mount(list, sensorCards(base)); return; }
    if (view === 'who') { mount(list, ...whoRows(base)); return; }
    let groups = fold_ ? fold(base) : base.map((e) => ({ last: e, count: 1, times: [e.ts] }));
    if (sort === 'count') groups = [...groups].sort((a, b) => b.count - a.count || b.last.ts - a.last.ts);
    else if (sort === 'new') groups = [...groups].reverse();
    groups = groups.slice(0, MAX_ROWS);
    mount(list, ...groups.map((g) => {
      const e = g.last;
      const toggleOpen = () => { if (open.has(e.id)) open.delete(e.id); else open.add(e.id); draw(); };
      const detail = open.has(e.id) ? h('pre.msg-detail', { onclick: (ev) => ev.stopPropagation() },
        (g.count > 1 ? `${t('Received {n} times:', { n: g.count })} ${g.times.map((x) => fmtTime(new Date(x * 1000).toISOString())).join(', ')}\n\n` : '')
        + JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !['id', 'summary'].includes(k))), null, 2)) : null;
      return h('div.msg', { class: open.has(e.id) ? 'open' : '', tabindex: 0, role: 'button', onclick: toggleOpen,
        onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleOpen(); } } },
      eventLine(e, g.count, pick), detail);
    }));
  }

  let pendingDraw = false;
  async function poll() {
    if (paused) return;
    if (pendingDraw) {            // the selection was let go: show what arrived meanwhile
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !list.contains(sel.anchorNode)) { pendingDraw = false; draw(); }
    }
    try {
      const r = await api.get(`/api/addons/events?since=${lastId}`);
      if (r.events.length) {
        rows = rows.concat(r.events).slice(-MAX_ROWS * 3);
        lastId = r.events[r.events.length - 1].id;
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed && list.contains(sel.anchorNode)) pendingDraw = true;      // a redraw would drop what is being selected/copied
        else draw();
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
