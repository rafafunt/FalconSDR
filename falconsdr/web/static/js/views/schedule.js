// "Schedule": time ranges on chosen weekdays. Each either scans (its own choice of bands and the Memory List) or
// runs an add-on (rtl_433, VDL2), and may be for chosen receivers only. A timeline shows what every receiver does
// through the day. When on, it replaces the switches in Listen → What to scan (or falls back to them between ranges).
import { api, confirmDialog, h, icon, mount, seg, t, toggle } from '../core.js';
import { S, cfg, on, patchConfig } from '../state.js';
import { receiverName } from './addons.js';
import { askCustomRange, askSingleFrequency, bandEditor, bandRange, presetPicker } from './bands.js';

const DAY_KEYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_SETS = () => [[t('Every day'), [0, 1, 2, 3, 4, 5, 6]], [t('Weekdays'), [0, 1, 2, 3, 4]], [t('Weekend'), [5, 6]]];
const WEEK = 7 * 1440;

const minutes = (hhmm) => { const [hh, mm] = hhmm.split(':').map(Number); return hh * 60 + mm; };

// Minutes of the week [from, to) an entry covers; a range past midnight belongs to its start day.
function weekSpans(e) {
  const start = minutes(e.start), end = minutes(e.end);
  const len = start === end ? 1440 : (end - start + 1440) % 1440;
  const spans = [];
  for (const d of e.days) {
    const from = d * 1440 + (start === end ? 0 : start), to = from + len;
    if (to <= WEEK) spans.push([from, to]);
    else spans.push([from, WEEK], [0, to - WEEK]);
  }
  return spans;
}

const overlaps = (a, b) => weekSpans(a).some(([a1, a2]) => weekSpans(b).some(([b1, b2]) => a1 < b2 && b1 < a2));

const receiverCount = () => 1 + ((cfg().device.extra_devices || []).length);
// Do two entries really compete? Scanning entries for named receivers are those receivers' own and ignore the shared
// ones; add-on entries compete when they want the same receiver (the first runs).
const addonReceiver = (e, defaults) => (e.receivers.length ? e.receivers[0] : (defaults[e.addon] || 0));
function competes(a, b, defaults) {
  if (!!a.addon !== !!b.addon) return false;
  if (a.addon) return addonReceiver(a, defaults) === addonReceiver(b, defaults);
  if (!a.receivers.length || !b.receivers.length) return !a.receivers.length && !b.receivers.length;
  return a.receivers.some((k) => b.receivers.includes(k));
}
const todayIndex = () => (new Date().getDay() + 6) % 7;     // 0 = Monday

export function scheduleView(root) {
  const offs = [];
  const head = h('section.card');
  const timeline = h('section.card');
  const list = h('div.col', { style: { gap: '16px' } });
  mount(root, h('div.page', h('div.page-head', h('h1', t('Schedule'))), h('div.col', { style: { gap: '16px' } }, head, timeline, list)));
  let day = todayIndex();
  let addons = [];            // the catalog (GET /api/addons): which external programs there are, installed or not
  const externals = () => addons.filter((a) => a.kind === 'external');
  const addonName = (id) => (addons.find((a) => a.id === id) || { name: id }).name;
  const normalise = () => sched().entries.forEach((e) => { e.receivers ||= []; e.addon ||= ''; });

  const sched = () => cfg().schedule;
  const save = () => patchConfig({ schedule: { entries: sched().entries } });
  const saveBands = () => patchConfig({ bands: cfg().bands });
  const active = () => (S.live && S.live.schedule_active) || [];
  const entryName = (e) => e.name || `${e.start}–${e.end}`;
  const status = h('div.col', { style: { gap: '4px' } });
  const expanded = new Set();  // "entry index:band id" with the band editor open

  function renderHead() {
    const s = sched();
    mount(head,
      h('div.setting',
        h('div', h('b', t('Scan by schedule')),
          h('p', t('When on, the time ranges below decide what is scanned instead of the switches in Listen → What to scan.'))),
        toggle('', s.enabled, (v) => patchConfig({ schedule: { enabled: v } }))),
      h('div.setting',
        h('div', h('b', t('Outside the time ranges'))),
        seg([['off', t('Pause scanning')], ['normal', t('Scan as usual')]], s.outside, (v) => { patchConfig({ schedule: { outside: v } }); renderHead(); })),
      h('p.faint.small', { style: { margin: 0 } }, s.outside === 'normal'
        ? t('Between the ranges the scanner scans what is switched on in Listen → What to scan.')
        : t('Between the ranges the scanner waits and scans nothing.')),
      status);
    renderStatus();
  }

  function renderStatus() {
    if (!S.live) return;
    const lines = (S.live.schedule_now || []);
    const clock = h('p.small.muted', { style: { margin: 0 } }, t('Receiver time: {time}', { time: S.live.clock }));
    if (!sched().enabled || !lines.length) {
      const now = active().map((i) => sched().entries[i]).filter(Boolean);
      mount(status, clock, h('p.small.muted', { style: { margin: 0 } }, sched().enabled
        ? (now.length ? t('Now: {names}', { names: now.map(entryName).join(', ') }) : t('No time range right now'))
        : t('The schedule is off - the switches in Listen → What to scan decide.')));
      return;
    }
    // One line per receiver: what it does right now and why.
    mount(status, clock, ...lines.map((r) => h('div.row.wrap.now-line', { style: { gap: '8px', alignItems: 'baseline' } },
      h('b', receiverCount() > 1 ? receiverName(r.receiver) : t('Receiver')),
      r.addon ? h('span.chip.addon', h('span.dot'), t('{name} runs here', { name: addonName(r.addon) }), r.by_schedule ? ` (${t('by the schedule')})` : ` (${t('started by hand')})`)
        : r.scanning ? h('span.chip.good', h('span.dot'), t('scanning'))
          : h('span.chip', t('nothing planned')),
      r.scanning ? h('span.small.muted', [...r.bands, ...(r.memory ? [t('Memory List')] : [])].join(', ')) : null,
      r.entries.length ? h('span.faint.small', `· ${r.entries.join(', ')}`) : null)));
  }

  // -- the timeline: one lane per receiver, the day's entries as blocks ---------------------------------------
  let lastNowMin = -1;
  function renderTimeline() {
    const entries = sched().entries;
    const n = receiverCount();
    const names = [t('Mon'), t('Tue'), t('Wed'), t('Thu'), t('Fri'), t('Sat'), t('Sun')];
    const dayStart = day * 1440;
    const now = new Date();
    lastNowMin = now.getHours() * 60 + now.getMinutes();
    const blocks = [];     // {lane, from, to, entry index}
    entries.forEach((e, i) => {
      const lanes = e.addon ? [e.receivers[0] ?? defaultReceiver(e.addon)] : (e.receivers.length ? e.receivers : [...Array(n).keys()]);
      for (const [a, b] of weekSpans(e)) {
        const from = Math.max(a, dayStart), to = Math.min(b, dayStart + 1440);
        if (from < to) lanes.filter((k) => k < n).forEach((lane) => blocks.push({ lane, from: from - dayStart, to: to - dayStart, i }));
      }
    });
    const laneEl = (lane) => {
      const mine = blocks.filter((x) => x.lane === lane).sort((x, y) => x.from - y.from);
      const rows = [];     // greedy packing: overlapping blocks go on separate rows
      mine.forEach((x) => { let r = rows.findIndex((end) => end <= x.from); if (r < 0) { r = rows.length; rows.push(0); } rows[r] = x.to; x.row = r; });
      const now = new Date();
      const nowMin = now.getHours() * 60 + now.getMinutes();
      return h('div.tl-lane', { style: { height: `${Math.max(1, rows.length) * 32 + 8}px` } },
        ...mine.map((x) => {
          const e = entries[x.i];
          return h('button.tl-block', { class: e.addon ? 'addon' : 'scan', title: `${e.start}–${e.end} · ${entryName(e)}`,
            style: { left: `${x.from / 14.4}%`, width: `${Math.max(1.5, (x.to - x.from) / 14.4)}%`, top: `${4 + x.row * 32}px` },
            onclick: () => { const el = document.getElementById(`entry-${x.i}`); if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); el.classList.add('spotlight'); setTimeout(() => el.classList.remove('spotlight'), 1500); } } },
          e.addon ? `${addonName(e.addon)}` : entryName(e));
        }),
        day === todayIndex() ? h('div.tl-now', { style: { left: `${nowMin / 14.4}%` }, title: t('Now') }) : null);
    };
    const axis = h('div.tl-axis', ...[0, 3, 6, 9, 12, 15, 18, 21, 24].map((hr) => h('span', `${String(hr).padStart(2, '0')}:00`)));
    mount(timeline,
      h('div.card-head', h('h2', t('The day at a glance')),
        h('div.row.wrap', { style: { gap: '6px' } }, names.map((nm, d) => h('button.small.day', { class: d === day ? 'on' : '', 'aria-pressed': String(d === day), onclick: () => { day = d; renderTimeline(); } },
          d === todayIndex() ? `${nm} •` : nm)))),
      h('div.tl', ...[...Array(n).keys()].flatMap((lane) => [h('div.tl-label', n > 1 ? receiverName(lane) : t('Receiver')), laneEl(lane)]), h('span'), axis),
      h('div.row.wrap.small.muted', { style: { gap: '14px' } },
        h('span', h('span.swatch', { style: { background: 'var(--accent)' } }), t('Scanning')),
        h('span', h('span.swatch', { style: { background: 'var(--purple)' } }), t('Add-on')),
        h('span', sched().enabled ? (sched().outside === 'normal' ? t('Empty stretches: scanning as usual') : t('Empty stretches: nothing is scanned')) : t('The schedule is off'))));
  }

  const defaultReceiver = (id) => (addons.find((a) => a.id === id) || { receiver: 0 }).receiver || 0;

  function renderList() {
    normalise();
    const entries = sched().entries;
    renderTimeline();
    mount(list,
      entries.length ? entries.map(entryCard) : h('section.card', h('div.empty', t('No time ranges yet.'))),
      h('div.row.wrap',
        h('button.primary', { onclick: () => addEntry('') }, icon('plus'), t('Add a scanning time range')),
        externals().length ? h('button', { onclick: () => addEntry(externals()[0].id) }, icon('plus'), t('Add an add-on time range')) : null));
  }

  function addEntry(addon) {
    const c = cfg();
    sched().entries.push({
      start: addon ? '22:00' : '08:00', end: addon ? '06:00' : '16:00', days: [0, 1, 2, 3, 4, 5, 6], name: '', receivers: [], addon,
      band_ids: c.bands.filter((b) => b.enabled).map((b) => b.id), memory_list: c.memory_list_enabled,
    });
    save();
    renderList();
  }

  function daysPicker(e) {
    const setDays = (days) => { e.days = [...days].sort(); save(); renderList(); };
    const preset = DAY_SETS().find(([, d]) => d.length === e.days.length && d.every((x) => e.days.includes(x)));
    return h('div.col', { style: { gap: '6px' } },
      h('div.row.wrap', { style: { gap: '6px' } }, DAY_KEYS.map((k, d) => h('button.small.day', {
        class: e.days.includes(d) ? 'on' : '', 'aria-pressed': String(e.days.includes(d)),
        onclick: () => setDays(e.days.includes(d) ? e.days.filter((x) => x !== d) : [...e.days, d]),
      }, t(k)))),
      h('div.row.wrap', { style: { gap: '6px' } },
        DAY_SETS().map(([label, days]) => h('button.ghost.small', { class: preset && preset[0] === label ? 'on' : '', onclick: () => setDays(days) }, label))));
  }

  function entryCard(e, i) {
    const setBand = (id, on) => {
      e.band_ids = on ? [...new Set([...e.band_ids, id])] : e.band_ids.filter((x) => x !== id);
      save();
    };
    // A band added here is off in Listen - only the schedule scans it.
    const addBand = (b) => {
      if (!b) return;
      b.enabled = false;
      cfg().bands.push(b);
      saveBands();
      setBand(b.id, true);
      renderList();
    };
    const time = (key) => h('input.time', {
      type: 'time', value: e[key], 'aria-label': key === 'start' ? t('From') : t('To'),
      onchange: (ev) => { if (ev.target.value) { e[key] = ev.target.value; save(); renderList(); } },
    });
    const clash = sched().entries.filter((o, j) => j !== i && overlaps(e, o) && competes(e, o, Object.fromEntries(addons.map((a) => [a.id, a.receiver]))));
    const channels = S.settings.channels.filter((ch) => ch.enabled).length;
    const n = receiverCount();
    const info = addons.find((a) => a.id === e.addon);
    const setAddon = (id) => { e.addon = id; if (id) e.receivers = e.receivers.slice(0, 1); save(); renderList(); };
    const setReceivers = (list) => { e.receivers = [...list].sort(); save(); renderList(); };
    const bandRow = (b) => {
      const key = `${i}:${b.id}`;
      const row = h('div.band', h('div.band-main',
        h('label.switch', h('input', { type: 'checkbox', checked: e.band_ids.includes(b.id), title: t('Scan this band'), onchange: (ev) => setBand(b.id, ev.target.checked) })),
        h('div.grow', h('div.title.ellipsis', b.label || t('(unnamed)')), h('div.range', bandRange(b))),
        h('button.icon.ghost', { title: t('Edit'), onclick: () => { expanded.has(key) ? expanded.delete(key) : expanded.add(key); renderList(); } },
          icon(expanded.has(key) ? 'chevron' : 'edit'))));
      if (expanded.has(key)) {
        row.append(h('p.faint.small', { style: { margin: 0 } }, t('Changes apply to this band everywhere, not only in this time range.')),
          bandEditor(b, saveBands, renderList));
      }
      return row;
    };
    // What this range does, and on which receivers.
    const actionRow = h('div.col', { style: { gap: '6px' } },
      h('div.row.wrap', h('b', t('What to do')),
        seg([['', t('Scan')], ...externals().map((a) => [a.id, a.name])], e.addon, (v) => setAddon(v))),
      e.addon && info && !info.installed ? h('div.notice', icon('ban'), h('span', { style: { flex: '1 1 260px' } }, t('{name} is not installed - this range is ignored until it is. Install it in Add-ons.', { name: info.name })),
        h('a', { href: '#/addons' }, t('Add-ons'))) : null);
    const receiverRow = (n > 1 || e.receivers.length) ? h('div.col', { style: { gap: '6px' } },
      h('div.row.wrap', h('b', t('Receivers')),
        e.addon
          ? seg([['', t('As set in Add-ons ({name})', { name: receiverName(defaultReceiver(e.addon)) })], ...[...Array(n).keys()].map((k) => [String(k), receiverName(k)])], e.receivers.length ? String(e.receivers[0]) : '', (v) => setReceivers(v === '' ? [] : [+v]))
          : h('div.row.wrap', { style: { gap: '6px' } },
            h('button.small.day', { class: e.receivers.length ? '' : 'on', 'aria-pressed': String(!e.receivers.length), onclick: () => setReceivers([]) }, t('All')),
            [...Array(n).keys()].map((k) => h('button.small.day', { class: e.receivers.includes(k) ? 'on' : '', 'aria-pressed': String(e.receivers.includes(k)),
              onclick: () => setReceivers(e.receivers.includes(k) ? e.receivers.filter((x) => x !== k) : [...e.receivers, k]) }, receiverName(k))))),
      h('p.faint.small', { style: { margin: 0 } }, e.addon
        ? (n > 1 ? t('The program holds this receiver while the range runs; the others keep scanning.') : t('The scanner pauses while the range runs.'))
        : (e.receivers.length ? t('Only these receivers scan this range, and they scan nothing else meanwhile.') : t('All receivers share this range: together they scan what it selects.')))) : null;

    const scanBody = [
      h('div.col', { style: { gap: 0 } }, cfg().bands.length
        ? cfg().bands.map(bandRow)
        : h('div.empty', t('Nothing to scan yet - add a band preset or a frequency below.'))),
      h('div.row.wrap', presetPicker(addBand),
        h('button', { onclick: async () => addBand(await askCustomRange()) }, icon('plus'), t('Custom range')),
        h('button', { onclick: async () => addBand(await askSingleFrequency()) }, icon('plus'), t('Single frequency')),
        h('a', { href: '#/settings/presets', style: { marginLeft: 'auto' } }, t('Edit band presets →'))),
      h('div.setting',
        h('div', toggle(t('Memory List'), e.memory_list, (v) => { e.memory_list = v; save(); }, { hint: t('{n} enabled channels', { n: channels }) })),
        h('a', { href: '#/channels' }, t('Edit channels →')))];
    const addonBody = [h('div.row.wrap', h('span.muted', info ? t(info.summary) : ''), h('a', { href: '#/addons', style: { marginLeft: 'auto' } }, t('Add-on settings →')))];

    return h('section.card', { id: `entry-${i}` },
      h('div.row.wrap',
        time('start'), h('span', '–'), time('end'),
        h('input', { style: { flex: '1 1 180px', minWidth: 0 }, value: e.name, placeholder: t('Name (optional)'), onchange: (ev) => { e.name = ev.target.value.trim(); save(); renderList(); renderStatus(); } }),
        active().includes(i) ? h('span.chip.good.live', h('span.dot'), t('Now')) : null,
        h('button.icon.ghost', {
          title: t('Remove'), onclick: async () => {
            if (!(await confirmDialog(t('Remove time range'), t('Remove “{name}” from the schedule?', { name: entryName(e) }), t('Remove'), true))) return;
            sched().entries.splice(i, 1); expanded.clear(); save(); renderList();
          },
        }, icon('trash'))),
      daysPicker(e),
      e.days.length ? null : h('div.notice', icon('clock'), t('No days selected - this time range never runs.')),
      clash.length ? h('div.notice', icon('clock'), h('span', { style: { flex: '1 1 220px' } },
        e.addon ? t('Overlaps with {names} on the same receiver - only the first of them runs.', { names: clash.map(entryName).join(', ') })
          : t('Overlaps with {names} - while both run, everything selected in either is scanned.', { names: clash.map(entryName).join(', ') }))) : null,
      actionRow, receiverRow,
      ...(e.addon ? addonBody : scanBody));
  }

  renderHead();
  renderList();
  api.get('/api/addons').then((r) => { addons = r.addons; renderList(); }).catch(() => {});
  let lastActive = JSON.stringify(active());
  offs.push(on('live', () => {
    renderStatus();
    const now = JSON.stringify(active());
    if (now !== lastActive) { lastActive = now; renderList(); }
    else if (day === todayIndex()) {
      const d = new Date();
      if (d.getHours() * 60 + d.getMinutes() !== lastNowMin) renderTimeline();   // the line for "now" moves once a minute
    }
  }));
  offs.push(on('settings', () => { renderHead(); renderList(); }));
  return { destroy() { offs.forEach((f) => f()); } };
}
