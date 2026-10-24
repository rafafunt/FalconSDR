// "Schedule": time ranges on chosen weekdays, each with its own choice of
// bands and the Memory List. When on, it replaces the switches in
// Listen → What to scan (or falls back to them between ranges).
import { confirmDialog, h, icon, mount, seg, t, toggle } from '../core.js';
import { S, cfg, on, patchConfig } from '../state.js';
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

export function scheduleView(root) {
  const offs = [];
  const head = h('section.card');
  const list = h('div.col', { style: { gap: '16px' } });
  mount(root, h('div.page', h('div.page-head', h('h1', t('Schedule'))), h('div.col', { style: { gap: '16px' } }, head, list)));

  const sched = () => cfg().schedule;
  const save = () => patchConfig({ schedule: { entries: sched().entries } });
  const saveBands = () => patchConfig({ bands: cfg().bands });
  const active = () => (S.live && S.live.schedule_active) || [];
  const entryName = (e) => e.name || `${e.start}–${e.end}`;
  const status = h('p.small.muted', { style: { margin: 0 } });
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
    const now = active().map((i) => sched().entries[i]).filter(Boolean);
    status.textContent = `${t('Receiver time: {time}', { time: S.live.clock })} · ${
      now.length ? t('Now: {names}', { names: now.map(entryName).join(', ') })
        : sched().outside === 'normal' ? t('No time range right now - scanning as usual') : t('No time range right now - scanning paused')}`;
  }

  function renderList() {
    const entries = sched().entries;
    mount(list,
      entries.length ? entries.map(entryCard) : h('section.card', h('div.empty', t('No time ranges yet.'))),
      h('div.row', h('button.primary', { onclick: addEntry }, icon('plus'), t('Add time range'))));
  }

  function addEntry() {
    const c = cfg();
    sched().entries.push({
      start: '08:00', end: '16:00', days: [0, 1, 2, 3, 4, 5, 6], name: '',
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
    const clash = sched().entries.filter((o, j) => j !== i && overlaps(e, o));
    const channels = S.settings.channels.filter((ch) => ch.enabled).length;
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
    return h('section.card',
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
        t('Overlaps with {names} - while both run, everything selected in either is scanned.', { names: clash.map(entryName).join(', ') }))) : null,
      h('div.col', { style: { gap: 0 } }, cfg().bands.length
        ? cfg().bands.map(bandRow)
        : h('div.empty', t('Nothing to scan yet - add a band preset or a frequency below.'))),
      h('div.row.wrap', presetPicker(addBand),
        h('button', { onclick: async () => addBand(await askCustomRange()) }, icon('plus'), t('Custom range')),
        h('button', { onclick: async () => addBand(await askSingleFrequency()) }, icon('plus'), t('Single frequency')),
        h('a', { href: '#/settings/presets', style: { marginLeft: 'auto' } }, t('Edit band presets →'))),
      h('div.setting',
        h('div', toggle(t('Memory List'), e.memory_list, (v) => { e.memory_list = v; save(); }, { hint: t('{n} enabled channels', { n: channels }) })),
        h('a', { href: '#/channels' }, t('Edit channels →'))));
  }

  renderHead();
  renderList();
  let lastActive = JSON.stringify(active());
  offs.push(on('live', () => {
    renderStatus();
    const now = JSON.stringify(active());
    if (now !== lastActive) { lastActive = now; renderList(); }
  }));
  offs.push(on('settings', () => { renderHead(); renderList(); }));
  return { destroy() { offs.forEach((f) => f()); } };
}
