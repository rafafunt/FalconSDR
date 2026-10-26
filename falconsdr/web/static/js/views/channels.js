// "Channels": the Memory List (named frequencies in folders, each with its
// own modulation, priority weight and on/off) and the blacklist.
import { carrierHz, confirmDialog, fmtChannel, fmtMHz, formDialog, h, icon, mount, seg, t, toast, typedAs833 } from '../core.js';
import { S, cfg, loadSettings, on, patchConfig, saveBlacklist, saveChannels } from '../state.js';
import { modLabel, modOptions } from './bands.js';
let tab = 'memory';

export function channelsView(root) {
  const offs = [];
  const body = h('div.col');
  const page = h('div.page',
    h('div.page-head', h('h1', t('Channels')),
      seg([['memory', t('Memory List')], ['blacklist', t('Blacklist')]], tab, (v) => { tab = v; sel.clear(); render(); })),
    body);
  mount(root, page);
  const collapsed = new Set(JSON.parse(localStorage.getItem('collapsedFolders') || '[]'));
  let query = '';

  // Row selection like Recordings: click, Ctrl/⌘+click, Shift+click; Delete
  // removes, Esc clears, Ctrl+A selects everything shown.
  const sel = new Set();
  const rowOf = new Map();
  let anchor = null;
  const shown = () => [...rowOf.keys()];
  const syncSel = () => rowOf.forEach((tr, item) => tr.classList.toggle('selected', sel.has(item)));
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

  function render() {
    rowOf.clear();
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
    const v = await formDialog(isNew ? t('Add channel') : t('Edit channel'), [
      { key: 'freq', label: t('Frequency (MHz)'), type: 'number', value: ch ? fmtChannel(ch.freq_hz) : '', hint: t('Airband: type the channel as published, e.g. 126.805') },
      { key: 'name', label: t('Name'), value: ch ? ch.name : '', placeholder: t('e.g. Downtown Tower') },
      { key: 'mod', label: t('Modulation'), type: 'select', value: ch ? ch.modulation : 'AM', options: modOptions() },
      { key: 'folder', label: t('Folder'), type: 'select', value: ch ? ch.folder : '', options: [['', t('(no folder)')], ...fl.map((f) => [f, f])] },
      { key: 'weight', label: t('Priority weight'), type: 'number', value: ch ? ch.weight : 0, hint: t('0 = normal. Higher = checked more often.') },
    ], isNew ? t('Add') : t('Save'));
    if (!v || !(v.freq > 0)) return;
    const hz = carrierHz(v.freq);
    const channels = S.settings.channels;
    if (channels.some((c) => c.freq_hz === hz && c !== ch)) { toast(t('{f} MHz is already in the list', { f: fmtChannel(hz) }), 'bad'); return; }
    const data = { freq_hz: hz, ch833: typedAs833(v.freq), name: v.name.trim(), modulation: v.mod, folder: v.folder, weight: v.weight || 0 };
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

  function channelRow(ch) {
    const save = () => saveChannels(S.settings.channels);
    return selectable(ch, h('tr', { ondblclick: (e) => { if (!e.target.closest('input, button, label')) editChannel(ch); } },
      h('td', h('label.switch', h('input', { type: 'checkbox', checked: ch.enabled, title: t('Scan this channel'), onchange: (e) => { ch.enabled = e.target.checked; save(); } }))),
      h('td.mono', { title: `${fmtMHz(ch.freq_hz, 4)} MHz` }, fmtChannel(ch.freq_hz)),
      h('td', ch.name || h('span.faint', t('(unnamed)'))),
      h('td', h('span.chip', modLabel(ch.modulation))),
      h('td', h('input', { type: 'number', min: 0, max: 20, step: 0.5, value: ch.weight, style: { width: '74px' }, title: t('Priority weight'), onchange: (e) => { ch.weight = +e.target.value || 0; save(); } })),
      h('td', h('div.actions',
        h('button.icon.ghost', { title: t('Edit'), onclick: () => editChannel(ch) }, icon('edit')),
        h('button.icon.ghost', {
          title: t('Delete'), onclick: async () => {
            if (!(await confirmDialog(t('Delete channel'), t('Delete {f} MHz {name}?', { f: fmtChannel(ch.freq_hz), name: ch.name }), t('Delete'), true))) return;
            await saveChannels(S.settings.channels.filter((c) => c !== ch));
            render();
          },
        }, icon('trash'))))));
  }

  function memory() {
    const q = query.trim().toLowerCase();
    const match = (c) => !q || `${fmtChannel(c.freq_hz)} ${fmtMHz(c.freq_hz, 4)} ${c.name} ${c.modulation} ${c.folder}`.toLowerCase().includes(q);
    const channels = S.settings.channels.filter(match).sort((a, b) => a.freq_hz - b.freq_hz);
    const groups = [['', channels.filter((c) => !c.folder)], ...folders().map((f) => [f, channels.filter((c) => c.folder === f)])];
    const search = h('input', { type: 'search', placeholder: t('Search frequency, name, folder…'), value: query, oninput: (e) => { query = e.target.value; const pos = e.target.selectionStart; render(); const el = body.querySelector('input[type=search]'); el.focus(); el.setSelectionRange(pos, pos); } });
    const rows = [];
    for (const [folder, list] of groups) {
      if (folder) {
        if (q && !list.length) continue;
        const isCollapsed = collapsed.has(folder) && !q;
        rows.push(h('tr.folder-row', {
          onclick: () => { isCollapsed ? collapsed.delete(folder) : collapsed.add(folder); localStorage.setItem('collapsedFolders', JSON.stringify([...collapsed])); render(); },
        }, h('td', { colSpan: 5 }, h('div.row', icon('folder'), folder, h('span.faint.small', `${list.length}`))),
        h('td', h('div.actions', { onclick: (e) => e.stopPropagation() },
          h('button.icon.ghost', { title: t('Rename folder'), onclick: () => renameFolder(folder) }, icon('edit')),
          h('button.icon.ghost', { title: t('Delete folder'), onclick: () => deleteFolder(folder) }, icon('trash'))))));
        if (isCollapsed) continue;
      }
      rows.push(...list.map(channelRow));
    }
    const enabled = S.settings.channels.filter((c) => c.enabled).length;
    return h('section.card',
      h('div.row.wrap',
        h('div.grow', search),
        h('button.primary', { onclick: () => editChannel(null) }, icon('plus'), t('Add channel')),
        h('button', { onclick: newFolder }, icon('folder'), t('New folder'))),
      h('div.row.wrap.small.muted',
        t('{n} channels, {e} scanned', { n: S.settings.channels.length, e: enabled }),
        cfg().memory_list_enabled ? null : h('span.chip.warn', t('The Memory List is switched off on the Listen page')),
        h('span.spacer'),
        h('button.ghost.small', { onclick: () => loadCurated('military') }, t('Add Polish military frequencies')),
        h('button.ghost.small', { onclick: () => loadCurated('fire') }, t('Add Polish fire brigade frequencies'))),
      S.settings.channels.length
        ? h('div.table-wrap', h('table',
          h('thead', h('tr', h('th', ''), h('th', t('MHz')), h('th', t('Name')), h('th', t('Mode')), h('th', t('Weight')), h('th', ''))),
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
    const list = S.settings.blacklist.filter((b) => !q || `${fmtChannel(b.freq_hz)} ${fmtMHz(b.freq_hz, 4)} ${b.label} ${b.date_added}`.toLowerCase().includes(q)).sort((a, b) => a.freq_hz - b.freq_hz);
    const search = h('input', { type: 'search', placeholder: t('Search blacklist…'), value: bq, oninput: (e) => { bq = e.target.value; render(); const el = body.querySelector('input[type=search]'); el.focus(); } });
    return h('section.card',
      h('div.row.wrap', h('div.grow', search), h('button.primary', { onclick: addBlacklist }, icon('plus'), t('Add'))),
      h('p.small.muted', { style: { margin: 0 } }, t('The scanner never stops on these frequencies. “Skip + blacklist” on the Listen page adds them here.')),
      h('div.setting', h('div', h('b', t('Default blacklist margin')), h('p', t('± kHz around a frequency added with “Skip + blacklist”.'))),
        h('input', { type: 'number', value: cfg().default_blacklist_margin_hz / 1000, min: 0, step: 0.5, style: { width: '110px' }, onchange: (e) => patchConfig({ default_blacklist_margin_hz: Math.round(Math.max(0, +e.target.value) * 1000) }) })),
      list.length
        ? h('div.table-wrap', h('table',
          h('thead', h('tr', h('th', t('MHz')), h('th', t('Margin')), h('th', t('Description')), h('th', t('Added')), h('th', ''))),
          h('tbody', list.map((b) => selectable(b, h('tr',
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
