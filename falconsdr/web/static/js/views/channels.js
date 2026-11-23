// "Channels": the Memory List (named frequencies in folders, each with its
// own modulation, priority weight, optional overrides and on/off) and the blacklist.
import { carrierHz, confirmDialog, fmtChannel, fmtMHz, formDialog, h, icon, modal, mount, select, tabs, t, toast, typedAs833 } from '../core.js';
import { S, cfg, loadSettings, on, patchConfig, saveBlacklist, saveChannels } from '../state.js';
import { isDigitalMod, modLabel, modOptions } from './bands.js';
let tab = 'memory';

const CH_OVERRIDES = [
  'bandwidth_hz', 'trigger_level_dbfs', 'squelch_enabled', 'squelch_offset_db',
  'linger_time_ms', 'noise_reduction_enabled', 'noise_reduction_strength',
];
const hasChannelOverrides = (c) => CH_OVERRIDES.some((k) => c[k] !== null && c[k] !== undefined);
function overrideSummary(c) {
  const parts = [];
  if (c.bandwidth_hz != null) parts.push(`${(c.bandwidth_hz / 1000).toFixed(c.bandwidth_hz % 1000 ? 2 : 0)} kHz`);
  if (c.trigger_level_dbfs != null) parts.push(`${c.trigger_level_dbfs} dBFS`);
  if (c.squelch_enabled != null || c.squelch_offset_db != null) parts.push(c.squelch_enabled === false ? `${t('Squelch')}: ${t('Off')}` : `${t('Squelch')}${c.squelch_offset_db != null ? ` ${c.squelch_offset_db} dB` : ''}`);
  if (c.linger_time_ms != null) parts.push(`${(c.linger_time_ms / 1000).toFixed(1)}s`);
  if (c.noise_reduction_enabled != null || c.noise_reduction_strength != null) parts.push(c.noise_reduction_enabled === false ? `${t('Noise reduction')}: ${t('Off')}` : `${t('Noise reduction')}${c.noise_reduction_strength != null ? ` ${Math.round(c.noise_reduction_strength * 100)}%` : ''}`);
  return parts.join(' · ');
}

// Sortable column headers (click: sort by it, click again: reverse). `dir` is the first click's direction:
// 1 = ascending (A-Z, low-high), -1 = descending (highest / newest / switched on first).
// Memory List also supports key === 'custom' (saved order in channels.csv, editable with arrows or drag-and-drop).
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const SORTS = {
  memory: { storage: 'channelsSort', cols: {
    on: { get: (c) => (c.enabled ? 1 : 0), dir: -1 },
    freq: { get: (c) => c.freq_hz, dir: 1 },
    name: { get: (c) => c.name || '', text: true, dir: 1 },
    mod: { get: (c) => modLabel(c.modulation), text: true, dir: 1 },
    weight: { get: (c) => c.weight || 0, dir: -1 },
  } },
  blacklist: { storage: 'blacklistSort', cols: {
    freq: { get: (b) => b.freq_hz, dir: 1 },
    margin: { get: (b) => b.margin_hz, dir: -1 },
    label: { get: (b) => (b.label === 'Auto-lockout (SKIP)' ? t('Skipped from the Listen page') : b.label || ''), text: true, dir: 1 },
    added: { get: (b) => b.date_added || '', text: true, dir: -1 },
  } },
};
const sortState = {};
for (const [name, def] of Object.entries(SORTS)) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(def.storage)); } catch { /* private window: start from the default */ }
  const ok = saved && ((name === 'memory' && saved.key === 'custom') || (def.cols[saved.key] && (saved.dir === 1 || saved.dir === -1)));
  sortState[name] = ok ? saved : { key: 'freq', dir: 1 };
}

function sorted(name, list) {
  const { key, dir } = sortState[name];
  if (key === 'custom') return [...list];
  const col = SORTS[name].cols[key];
  return [...list].sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    // An empty text (no name, no description) goes last either way - reversing must not bury the real entries.
    if (col.text && (x === '') !== (y === '')) return x === '' ? 1 : -1;
    const c = col.text ? collator.compare(x, y) : x - y;
    return c ? c * dir : SORTS[name].cols.freq.get(a) - SORTS[name].cols.freq.get(b);
  });
}

export function channelsView(root) {
  const offs = [];
  const body = h('div.col');
  const tabsBox = h('div');
  const page = h('div.page', h('div.page-head', h('h1', t('Channels'))), tabsBox, body);
  mount(root, page);
  const renderTabs = () => mount(tabsBox, tabs([['memory', t('Memory List'), S.settings.channels.length], ['blacklist', t('Blacklist'), S.settings.blacklist.length]], tab,
    (v) => { tab = v; sel.clear(); render(); }));
  let savedCollapsed = [];
  try { savedCollapsed = JSON.parse(localStorage.getItem('collapsedFolders') || '[]'); } catch { savedCollapsed = []; }
  const collapsed = new Set(Array.isArray(savedCollapsed) ? savedCollapsed : []);
  let query = '';

  // Row selection like Recordings: click, Ctrl/⌘+click, Shift+click; Delete
  // removes, Esc clears, Ctrl+A selects everything shown.
  const sel = new Set();
  const rowOf = new Map();
  const boxOf = new Map();            // item -> its checkbox
  let anchor = null;
  let bulkBox = h('div.bulk.hidden');
  let headBox = null;
  const shown = () => [...rowOf.keys()];
  function syncSel() {
    rowOf.forEach((tr, item) => tr.classList.toggle('selected', sel.has(item)));
    boxOf.forEach((cb, item) => { cb.checked = sel.has(item); });
    const all = shown();
    const n = all.filter((x) => sel.has(x)).length;
    if (headBox) { headBox.checked = !!all.length && n === all.length; headBox.indeterminate = n > 0 && n < all.length; }
    renderBulk();
  }
  // The checkbox column: tick one, shift+tick a range; the header box ticks everything shown.
  function checkbox(item) {
    const cb = h('input', { type: 'checkbox', 'aria-label': t('Select'), onclick: (e) => {
      e.stopPropagation();
      const all = shown();
      if (e.shiftKey && all.includes(anchor)) {
        const a = all.indexOf(anchor), b = all.indexOf(item);
        all.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => sel.add(x));
      } else { cb.checked ? sel.add(item) : sel.delete(item); }
      anchor = item;
      syncSel();
    } });
    boxOf.set(item, cb);
    return h('td.pick', cb);
  }
  function selectAllHead() {
    headBox = h('input', { type: 'checkbox', 'aria-label': t('Select all shown'), onclick: () => {
      const all = shown();
      if (all.every((x) => sel.has(x))) all.forEach((x) => sel.delete(x)); else all.forEach((x) => sel.add(x));
      syncSel();
    } });
    return h('th.pick', headBox);
  }
  function renderBulk() {
    bulkBox.classList.toggle('hidden', !sel.size);
    if (!sel.size) return;
    const items = [...sel];
    const all = shown();
    const actions = tab === 'memory' ? [
      h('button', { onclick: () => setEnabled(items, true) }, t('Scan')),
      h('button', { onclick: () => setEnabled(items, false) }, t("Don't scan")),
      select([['', t('Move to folder…')], ['__none__', t('(no folder)')], ...folders().map((f) => [f, f])], '', (v) => { if (v) moveTo(items, v === '__none__' ? '' : v); }, { style: { width: 'auto' }, 'aria-label': t('Move to folder…') }),
    ] : [];
    mount(bulkBox, h('div.row.wrap',
      h('b', t('{n} selected', { n: items.length.toLocaleString() })),
      items.length < all.length ? h('button.ghost', { onclick: () => { all.forEach((x) => sel.add(x)); syncSel(); } }, t('Select all {n}', { n: all.length.toLocaleString() })) : null,
      h('span.spacer'), ...actions,
      h('button.danger', { onclick: removeSelected }, icon('trash'), t('Delete')),
      h('button.ghost', { onclick: () => { sel.clear(); syncSel(); } }, t('Clear'))));
  }
  async function setEnabled(items, on) {
    items.forEach((c) => { c.enabled = on; });
    await saveChannels(S.settings.channels);
    render();
  }
  async function moveTo(items, folder) {
    items.forEach((c) => { c.folder = folder; });
    await saveChannels(S.settings.channels);
    render();
  }
  async function deleteAll() {
    const n = tab === 'memory' ? S.settings.channels.length : S.settings.blacklist.length;
    if (!n) return;
    const msg = tab === 'memory' ? t('Delete all {n} channels from the Memory List?', { n }) : t('Remove all {n} entries from the blacklist?', { n });
    if (!(await confirmDialog(tab === 'memory' ? t('Delete all channels') : t('Empty the blacklist'), msg, t('Delete all'), true))) return;
    if (tab === 'memory') await saveChannels([]); else await saveBlacklist([]);
    sel.clear();
    render();
  }
  function selectable(item, tr) {
    rowOf.set(item, tr);
    tr.addEventListener('click', (e) => {
      if (e.target.closest('input, button, label, a')) return;
      const all = shown();
      if (e.shiftKey && all.includes(anchor)) {
        const a = all.indexOf(anchor), b = all.indexOf(item);
        all.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => sel.add(x));
      } else if (e.ctrlKey || e.metaKey) { sel.has(item) ? sel.delete(item) : sel.add(item); anchor = item; }
      else { sel.clear(); sel.add(item); anchor = item; }
      syncSel();
    });
    return tr;
  }

  async function removeSelected() {
    const items = [...sel];
    if (tab === 'memory') {
      const msg = items.length === 1
        ? t('Delete {f} MHz {name}?', { f: fmtChannel(items[0].freq_hz), name: items[0].name })
        : t('Delete {n} selected channels?', { n: items.length });
      if (!(await confirmDialog(t('Delete channel'), msg, t('Delete'), true))) return;
      await saveChannels(S.settings.channels.filter((c) => !sel.has(c)));
    } else await saveBlacklist(S.settings.blacklist.filter((b) => !sel.has(b)));
    sel.clear();
    render();
  }

  const onKey = (e) => {
    if (document.activeElement.matches('textarea, select, input:not([type=checkbox])') || document.querySelector('.modal-back')) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && sel.size) { e.preventDefault(); removeSelected(); }
    else if (e.key === 'Escape' && sel.size) { sel.clear(); syncSel(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === 'a') { e.preventDefault(); shown().forEach((x) => sel.add(x)); syncSel(); }
  };
  document.addEventListener('keydown', onKey);

  function sortTh(name, key, label, title) {
    const st = sortState[name], active = st.key === key;
    const flip = () => {
      sortState[name] = active ? { key, dir: -st.dir } : { key, dir: SORTS[name].cols[key].dir };
      try { localStorage.setItem(SORTS[name].storage, JSON.stringify(sortState[name])); } catch { /* not remembered, still sorted */ }
      render();
    };
    return h('th', { 'aria-sort': active ? (st.dir === 1 ? 'ascending' : 'descending') : 'none' },
      h('button.sort', { class: active ? 'on' : '', title: title || t('Sort by {col}', { col: label }), onclick: flip },
        label, h('span.arrow', active ? (st.dir === 1 ? '▲' : '▼') : '↕')));
  }

  function render() {
    rowOf.clear();
    boxOf.clear();
    headBox = null;
    bulkBox = h('div.bulk.hidden');
    renderTabs();
    mount(body, tab === 'memory' ? memory() : blacklist());
    sel.forEach((x) => { if (!rowOf.has(x)) sel.delete(x); });  // gone, filtered out or folded away
    syncSel();
  }

  // ---------------------------------------------------------- memory list
  function folders() {
    return [...new Set([...cfg().channel_folders, ...S.settings.channels.map((c) => c.folder).filter(Boolean)])].sort((a, b) => a.localeCompare(b));
  }

  async function editChannel(ch) {
    const isNew = !ch;
    const fl = folders();
    const c = cfg();
    const freqIn = h('input', { type: 'number', step: 'any', value: ch ? fmtChannel(ch.freq_hz) : '' });
    const nameIn = h('input', { value: ch ? ch.name : '', placeholder: t('e.g. Downtown Tower') });
    const modIn = select(modOptions(), ch ? ch.modulation : 'AM', () => syncAnalogOnly());
    const folderIn = select([['', t('(no folder)')], ...fl.map((f) => [f, f])], ch ? ch.folder : '', () => {});
    const weightIn = h('input', { type: 'number', step: 'any', min: 0, max: 20, value: ch ? ch.weight : 0 });

    // Optional per-channel overrides: empty / "Default" = null (inherits band/global settings).
    const tri = (val) => (val === true ? 'on' : val === false ? 'off' : '');
    const parseTri = (v) => (v === 'on' ? true : v === 'off' ? false : null);
    const optNum = (el, scale = 1, round = false) => {
      const s = el.value.trim();
      if (!s) return null;
      const n = parseFloat(s);
      if (!Number.isFinite(n)) return null;
      return round ? Math.round(n * scale) : n * scale;
    };
    const bwIn = h('input', { type: 'number', step: 'any', min: 1, max: 500, placeholder: String(c.memory_channel_bandwidth_hz / 1000), value: ch && ch.bandwidth_hz != null ? ch.bandwidth_hz / 1000 : '' });
    const trigIn = h('input', { type: 'number', step: 1, min: -120, max: 0, placeholder: String(c.trigger_level_dbfs), value: ch && ch.trigger_level_dbfs != null ? ch.trigger_level_dbfs : '' });
    const sqModeIn = select([['', t('Global / band')], ['on', t('On')], ['off', t('Off')]], ch ? tri(ch.squelch_enabled) : '', () => {});
    const sqOffIn = h('input', { type: 'number', step: 1, min: -40, max: 0, placeholder: String(c.squelch_offset_db), value: ch && ch.squelch_offset_db != null ? ch.squelch_offset_db : '' });
    const lingerIn = h('input', { type: 'number', step: 0.1, min: 0, max: 30, placeholder: String(c.timing.linger_time_ms / 1000), value: ch && ch.linger_time_ms != null ? ch.linger_time_ms / 1000 : '' });
    const nrModeIn = select([['', t('Global / band')], ['on', t('On')], ['off', t('Off')]], ch ? tri(ch.noise_reduction_enabled) : '', () => {});
    const nrStrIn = h('input', { type: 'number', step: 1, min: 0, max: 100, placeholder: String(Math.round(c.noise_reduction.strength * 100)), value: ch && ch.noise_reduction_strength != null ? Math.round(ch.noise_reduction_strength * 100) : '' });

    const sqRow = h('div.fields',
      h('label.field', t('Squelch'), sqModeIn),
      h('label.field', t('Squelch offset (dB)'), sqOffIn));
    const nrRow = h('div.fields',
      h('label.field', t('Noise reduction'), nrModeIn),
      h('label.field', t('Noise reduction strength (%)'), nrStrIn));
    const syncAnalogOnly = () => {
      const hide = isDigitalMod(modIn.value);
      sqRow.classList.toggle('hidden', hide);
      nrRow.classList.toggle('hidden', hide);
    };
    syncAnalogOnly();

    const ovOpen = !!(ch && hasChannelOverrides(ch));
    const form = h('form.col', { onsubmit: (e) => { e.preventDefault(); form.closest('.modal-back').querySelector('.actions button.primary').click(); } },
      h('label.field', t('Frequency (MHz)'), freqIn, h('span.hint', t('Airband: type the channel as published, e.g. 126.805'))),
      h('label.field', t('Name'), nameIn),
      h('div.fields',
        h('label.field', t('Modulation'), modIn),
        h('label.field', t('Folder'), folderIn),
        h('label.field', t('Priority weight'), weightIn)),
      h('span.small.muted', t('0 = normal. Higher = checked more often.')),
      h('details', { open: ovOpen },
        h('summary', t('Custom settings for this channel')),
        h('div.col', { style: { marginTop: '10px', gap: '10px' } },
          h('p.small.muted', t('Leave empty to use the band or global setting.')),
          h('div.fields',
            h('label.field', t('Channel bandwidth (kHz)'), bwIn),
            h('label.field', t('Trigger level (dBFS)'), trigIn),
            h('label.field', t('Hold after a transmission (s)'), lingerIn)),
          sqRow,
          nrRow)));

    const ok = await modal(isNew ? t('Add channel') : t('Edit channel'), form, [[t('Cancel'), false, 'ghost'], [isNew ? t('Add') : t('Save'), true, 'primary']]);
    if (!ok) return;
    const freq = parseFloat(freqIn.value);
    if (!(freq > 0)) return;
    const hz = carrierHz(freq);
    const channels = S.settings.channels;
    if (channels.some((x) => x.freq_hz === hz && x !== ch)) { toast(t('{f} MHz is already in the list', { f: fmtChannel(hz) }), 'bad'); return; }
    const data = {
      freq_hz: hz, ch833: typedAs833(freq), name: nameIn.value.trim(), modulation: modIn.value,
      folder: folderIn.value, weight: parseFloat(weightIn.value) || 0,
      bandwidth_hz: optNum(bwIn, 1000, true),
      trigger_level_dbfs: optNum(trigIn, 1, false),
      squelch_enabled: parseTri(sqModeIn.value),
      squelch_offset_db: optNum(sqOffIn, 1, false),
      linger_time_ms: optNum(lingerIn, 1000, true),
      noise_reduction_enabled: parseTri(nrModeIn.value),
      noise_reduction_strength: optNum(nrStrIn, 0.01, false),
    };
    if (isNew) channels.push({ ...data, enabled: true }); else Object.assign(ch, data);
    await saveChannels(channels);
    render();
  }

  async function newFolder() {
    const v = await formDialog(t('New folder'), [{ key: 'name', label: t('Folder name') }], t('Create'));
    const name = v && v.name.trim();
    if (!name) return;
    if (folders().includes(name)) { toast(t('Folder “{name}” already exists.', { name }), 'bad'); return; }
    patchConfig({ channel_folders: [...cfg().channel_folders, name] });
    render();
  }

  async function renameFolder(old) {
    const v = await formDialog(t('Rename folder'), [{ key: 'name', label: t('Folder name'), value: old }], t('Rename'));
    const name = v && v.name.trim();
    if (!name || name === old) return;
    if (folders().includes(name)) { toast(t('Folder “{name}” already exists.', { name }), 'bad'); return; }
    S.settings.channels.forEach((c) => { if (c.folder === old) c.folder = name; });
    patchConfig({ channel_folders: cfg().channel_folders.map((f) => (f === old ? name : f)) });
    await saveChannels(S.settings.channels);
    render();
  }

  async function deleteFolder(name) {
    const inside = S.settings.channels.filter((c) => c.folder === name);
    const ok = await confirmDialog(t('Delete folder'), inside.length ? t('Delete “{name}” and its {n} channels?', { name, n: inside.length }) : t('Delete the empty folder “{name}”?', { name }), t('Delete'), true);
    if (!ok) return;
    patchConfig({ channel_folders: cfg().channel_folders.filter((f) => f !== name) });
    await saveChannels(S.settings.channels.filter((c) => c.folder !== name));
    render();
  }

  async function loadCurated(kind) {
    const existing = new Set(S.settings.channels.map((c) => c.freq_hz));
    const folder = kind === 'military' ? t('Military') : t('Fire brigade');
    const add = S.settings.curated[kind].filter((c) => !existing.has(c.freq_hz)).map((c) => ({ ...c, folder }));
    if (!add.length) { toast(t('All of these are already in the list.')); return; }
    if (!cfg().channel_folders.includes(folder)) patchConfig({ channel_folders: [...cfg().channel_folders, folder] });
    await saveChannels([...S.settings.channels, ...add]);
    toast(t('Added {n} channels', { n: add.length }), 'good');
    render();
  }

  let dragChannel = null;
  async function moveChannel(ch, dir, list) {
    const idx = list.indexOf(ch);
    const other = list[idx + dir];
    if (!other) return;
    const all = S.settings.channels;
    const a = all.indexOf(ch), b = all.indexOf(other);
    if (a < 0 || b < 0) return;
    [all[a], all[b]] = [all[b], all[a]];
    await saveChannels(all);
    render();
  }

  async function reorderChannel(src, dst) {
    if (!src || !dst || src === dst || src.folder !== dst.folder) return;
    const all = S.settings.channels;
    const from = all.indexOf(src), to = all.indexOf(dst);
    if (from < 0 || to < 0) return;
    all.splice(from, 1);
    all.splice(all.indexOf(dst) + (from < to ? 1 : 0), 0, src);
    await saveChannels(all);
    render();
  }

  function channelRow(ch, idx, list) {
    const save = () => saveChannels(S.settings.channels);
    const custom = sortState.memory.key === 'custom' && !query.trim();
    const tr = h('tr', {
      draggable: custom ? true : false,
      ondblclick: (e) => { if (!e.target.closest('input, button, label')) editChannel(ch); },
      ondragstart: custom ? (e) => { dragChannel = ch; tr.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; } : null,
      ondragend: custom ? () => { dragChannel = null; tr.classList.remove('dragging'); body.querySelectorAll('.drag-over').forEach((x) => x.classList.remove('drag-over')); } : null,
      ondragover: custom ? (e) => { if (dragChannel && dragChannel !== ch && dragChannel.folder === ch.folder) { e.preventDefault(); tr.classList.add('drag-over'); } } : null,
      ondragleave: custom ? () => tr.classList.remove('drag-over') : null,
      ondrop: custom ? (e) => { e.preventDefault(); tr.classList.remove('drag-over'); reorderChannel(dragChannel, ch); } : null,
    },
      checkbox(ch),
      h('td', h('label.switch', h('input', { type: 'checkbox', checked: ch.enabled, title: t('Scan this channel'), onchange: (e) => { ch.enabled = e.target.checked; save(); } }))),
      h('td.mono', { title: `${fmtMHz(ch.freq_hz, 4)} MHz` }, fmtChannel(ch.freq_hz)),
      h('td', ch.name || h('span.faint', t('(unnamed)'))),
      h('td', h('div.row', { style: { gap: '6px', flexWrap: 'wrap' } },
        h('span.chip', modLabel(ch.modulation)),
        hasChannelOverrides(ch) ? h('span.chip.accent', { title: overrideSummary(ch) }, t('custom')) : null)),
      h('td', h('input', { type: 'number', min: 0, max: 20, step: 0.5, value: ch.weight, style: { width: '74px' }, title: t('Priority weight'), onchange: (e) => { ch.weight = +e.target.value || 0; save(); } })),
      h('td', h('div.actions',
        custom ? h('button.icon.ghost', { title: t('Move up'), disabled: idx === 0, onclick: () => moveChannel(ch, -1, list) }, icon('up')) : null,
        custom ? h('button.icon.ghost', { title: t('Move down'), disabled: idx === list.length - 1, onclick: () => moveChannel(ch, 1, list) }, icon('down')) : null,
        h('button.icon.ghost', { title: t('Edit'), onclick: () => editChannel(ch) }, icon('edit')),
        h('button.icon.ghost', {
          title: t('Delete'), onclick: async () => {
            if (!(await confirmDialog(t('Delete channel'), t('Delete {f} MHz {name}?', { f: fmtChannel(ch.freq_hz), name: ch.name }), t('Delete'), true))) return;
            await saveChannels(S.settings.channels.filter((c) => c !== ch));
            render();
          },
        }, icon('trash')))));
    return selectable(ch, tr);
  }

  function memory() {
    const q = query.trim().toLowerCase();
    const match = (c) => !q || `${fmtChannel(c.freq_hz)} ${fmtMHz(c.freq_hz, 4)} ${c.name} ${c.modulation} ${c.folder}`.toLowerCase().includes(q);
    const channels = sorted('memory', S.settings.channels.filter(match));
    const groups = [['', channels.filter((c) => !c.folder)], ...folders().map((f) => [f, channels.filter((c) => c.folder === f)])];
    const search = h('input', { type: 'search', placeholder: t('Search frequency, name, folder…'), value: query, oninput: (e) => { query = e.target.value; const pos = e.target.selectionStart; render(); const el = body.querySelector('input[type=search]'); el.focus(); el.setSelectionRange(pos, pos); } });
    const rows = [];
    for (const [folder, list] of groups) {
      if (folder) {
        if (q && !list.length) continue;
        const isCollapsed = collapsed.has(folder) && !q;
        rows.push(h('tr.folder-row', {
          onclick: () => { isCollapsed ? collapsed.delete(folder) : collapsed.add(folder); localStorage.setItem('collapsedFolders', JSON.stringify([...collapsed])); render(); },
        }, h('td', { colSpan: 6 }, h('div.row', icon('folder'), folder, h('span.faint.small', `${list.length}`))),
        h('td', h('div.actions', { onclick: (e) => e.stopPropagation() },
          h('button.icon.ghost', { title: t('Rename folder'), onclick: () => renameFolder(folder) }, icon('edit')),
          h('button.icon.ghost', { title: t('Delete folder'), onclick: () => deleteFolder(folder) }, icon('trash'))))));
        if (isCollapsed) continue;
      }
      rows.push(...list.map((ch, idx) => channelRow(ch, idx, list)));
    }
    const enabled = S.settings.channels.filter((c) => c.enabled).length;
    const isCustom = sortState.memory.key === 'custom';
    const toggleCustomOrder = () => {
      sortState.memory = isCustom ? { key: 'freq', dir: 1 } : { key: 'custom', dir: 1 };
      try { localStorage.setItem(SORTS.memory.storage, JSON.stringify(sortState.memory)); } catch { /* ignore */ }
      render();
    };
    return h('section.card',
      h('div.row.wrap',
        h('div.grow', search),
        h('button', { class: isCustom ? 'on' : '', title: t('Drag rows or use the arrows to arrange channels in your own order. Scan priority is controlled by Weight.'), onclick: toggleCustomOrder }, t('Custom order')),
        h('button.primary', { onclick: () => editChannel(null) }, icon('plus'), t('Add channel')),
        h('button', { onclick: newFolder }, icon('folder'), t('New folder')),
        S.settings.channels.length ? h('button.ghost.danger', { onclick: deleteAll }, icon('trash'), t('Delete all')) : null),
      h('div.row.wrap.small.muted',
        t('{n} channels, {e} scanned', { n: S.settings.channels.length, e: enabled }),
        cfg().memory_list_enabled ? null : h('span.chip.warn', t('The Memory List is switched off on the Listen page')),
        h('span.spacer'),
        h('button.ghost.small', { onclick: () => loadCurated('military') }, t('Add Polish military frequencies')),
        h('button.ghost.small', { onclick: () => loadCurated('fire') }, t('Add Polish fire brigade frequencies'))),
      bulkBox,
      S.settings.channels.length
        ? h('div.table-wrap', h('table',
          h('thead', h('tr', selectAllHead(), sortTh('memory', 'on', '✓', t('Sort by scanned / not scanned')), sortTh('memory', 'freq', t('MHz')), sortTh('memory', 'name', t('Name')), sortTh('memory', 'mod', t('Mode')), sortTh('memory', 'weight', t('Weight')), h('th', ''))),
          h('tbody', rows)))
        : h('div.empty', icon('channels'), h('div', t('No saved channels yet. Add one, or save a frequency straight from a recording.'))));
  }

  // ---------------------------------------------------------- blacklist
  let bq = '';
  async function addBlacklist() {
    const v = await formDialog(t('Add to blacklist'), [
      { key: 'freq', label: t('Frequency (MHz)'), type: 'number' },
      { key: 'margin', label: t('Margin ± (kHz)'), type: 'number', value: (cfg().default_blacklist_margin_hz / 1000) },
      { key: 'label', label: t('Description'), value: '' },
    ], t('Add'));
    if (!v || !(v.freq > 0)) return;
    await saveBlacklist([...S.settings.blacklist, { freq_hz: carrierHz(v.freq), margin_hz: Math.round((v.margin || 0) * 1000), label: v.label.trim(), date_added: new Date().toISOString().slice(0, 10) }]);
    render();
  }

  function blacklist() {
    const q = bq.trim().toLowerCase();
    const list = sorted('blacklist', S.settings.blacklist.filter((b) => !q || `${fmtChannel(b.freq_hz)} ${fmtMHz(b.freq_hz, 4)} ${b.label} ${b.date_added}`.toLowerCase().includes(q)));
    const search = h('input', { type: 'search', placeholder: t('Search blacklist…'), value: bq, oninput: (e) => { bq = e.target.value; render(); const el = body.querySelector('input[type=search]'); el.focus(); } });
    return h('section.card',
      h('div.row.wrap', h('div.grow', search), h('button.primary', { onclick: addBlacklist }, icon('plus'), t('Add')),
        S.settings.blacklist.length ? h('button.ghost.danger', { onclick: deleteAll }, icon('trash'), t('Delete all')) : null),
      h('p.small.muted', { style: { margin: 0 } }, t('The scanner never stops on these frequencies. “Skip + blacklist” on the Listen page adds them here.')),
      h('div.setting', h('div', h('b', t('Default blacklist margin')), h('p', t('± kHz around a frequency added with “Skip + blacklist”.'))),
        h('input', { type: 'number', value: cfg().default_blacklist_margin_hz / 1000, min: 0, step: 0.5, style: { width: '110px' }, onchange: (e) => patchConfig({ default_blacklist_margin_hz: Math.round(Math.max(0, +e.target.value) * 1000) }) })),
      bulkBox,
      list.length
        ? h('div.table-wrap', h('table',
          h('thead', h('tr', selectAllHead(), sortTh('blacklist', 'freq', t('MHz')), sortTh('blacklist', 'margin', t('Margin')), sortTh('blacklist', 'label', t('Description')), sortTh('blacklist', 'added', t('Added')), h('th', ''))),
          h('tbody', list.map((b) => selectable(b, h('tr',
            checkbox(b),
            h('td.mono', fmtChannel(b.freq_hz)),
            h('td.mono', `±${(b.margin_hz / 1000).toFixed(1)} kHz`),
            h('td', b.label === 'Auto-lockout (SKIP)' ? t('Skipped from the Listen page') : b.label),
            h('td.mono.small', b.date_added),
            h('td', h('div.actions', h('button.icon.ghost', {
              title: t('Remove from blacklist'), onclick: async () => { await saveBlacklist(S.settings.blacklist.filter((x) => x !== b)); render(); },
            }, icon('trash'))))))))))
        : h('div.empty', t('The blacklist is empty.')));
  }

  render();
  offs.push(on('settings', render));
  loadSettings();  // pick up blacklist entries the scanner added meanwhile
  return { destroy() { offs.forEach((f) => f()); document.removeEventListener('keydown', onKey); } };
}
