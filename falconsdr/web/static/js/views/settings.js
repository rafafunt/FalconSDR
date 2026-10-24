// "Settings": recording, recency boost, transcription & helper computers,
// band presets, location, language/listening, disk & retention, server
// connection, access password and advanced options.
import {
  api, confirmDialog, errorToast, fmtAgo, fmtBytes, fmtDate, fmtMHz, formDialog, h, icon, lang, modal, mount, qs, select, t, toast, toggle,
} from '../core.js';
import { S, cfg, confirmRecording, loadSettings, on, patchConfig } from '../state.js';

const SECTIONS = [
  ['recording', () => t('Recording')],
  ['scheduling', () => t('Recency boost')],
  ['transcription', () => t('Transcription & computers')],
  ['presets', () => t('Band presets')],
  ['location', () => t('Location')],
  ['appearance', () => t('Language & listening')],
  ['storage', () => t('Disk & retention')],
  ['connect', () => t('Connect to a server')],
  ['access', () => t('Access')],
  ['advanced', () => t('Advanced')],
];

function setting(title, desc, control) {
  return h('div.setting', h('div', h('b', title), desc ? h('p', desc) : null), control);
}

function numberInput(value, onchange, attrs = {}) {
  return h('input', { type: 'number', value, step: 'any', style: { width: '110px' }, ...attrs, onchange: (e) => onchange(+e.target.value) });
}

export function settingsView(root, hash) {
  const offs = [];
  const timers = [];
  const cards = {};
  const visible = SECTIONS.filter(([k]) => k !== 'connect' || S.settings.mode === 'local');
  const nav = h('nav', visible.map(([k, label]) => h('a', { href: `#/settings/${k}`, onclick: (e) => { e.preventDefault(); cards[k].scrollIntoView({ behavior: 'smooth', block: 'start' }); } }, label())));
  const content = h('div.col', { style: { gap: '16px' } });
  mount(root, h('div.page', h('div.page-head', h('h1', t('Settings'))), h('div.settings', nav, content)));
  for (const [k, label] of visible) {
    cards[k] = h('section.card', { id: `s-${k}` }, h('div.card-head', h('h2', label())), h('div.col'));
    content.append(cards[k]);
  }
  const body = (k) => cards[k].lastChild;

  // ---------------------------------------------------------- recording
  function renderRecording() {
    const r = cfg().recording;
    mount(body('recording'),
      setting(t('Auto-record transmissions'), t('Every transmission the scanner stops on is saved as its own file.'),
        toggle('', r.enabled, async (v, e) => {
          if (v && !(await confirmRecording())) { e.target.checked = false; return; }
          patchConfig({ recording: { enabled: v } });
        })),
      setting(t('Simultaneous recordings'), t('How many transmissions may be recorded at the same time. 0 = no limit.'),
        numberInput(r.max_concurrent_recordings, (v) => patchConfig({ recording: { max_concurrent_recordings: Math.max(0, Math.round(v)) } }), { min: 0, max: 50 })),
      setting(t('File format'), S.settings.capabilities.ffmpeg ? t('MP3 takes about 10× less space.') : t('MP3 needs ffmpeg installed on this machine.'),
        select([['wav', t('WAV (uncompressed)')], ['mp3', t('MP3 (compressed)')]], r.output_format, (v) => patchConfig({ recording: { output_format: v } }), { disabled: !S.settings.capabilities.ffmpeg && r.output_format !== 'mp3' })),
      setting(t('Recordings folder'), t('On the machine that scans. Existing recordings are not moved.'),
        h('div.row', { style: { gap: '8px', flexWrap: 'wrap' } },
          h('input', { value: r.output_dir, style: { width: 'min(420px, 100%)' }, onchange: (e) => patchConfig({ recording: { output_dir: e.target.value.trim() } }) }),
          h('button', { onclick: async () => {
            const dir = await pickFolder(r.output_dir);
            if (dir) { patchConfig({ recording: { output_dir: dir } }); renderRecording(); }
          } }, icon('folder'), t('Browse…')))));
  }

  // The folder lives on the scanning machine, so its own file system is browsed via the API.
  async function pickFolder(start) {
    let cur = null;
    const where = h('div.mono.small.ellipsis'), list = h('div.dirs');
    const open = async (path) => {
      try { cur = await api.get('/api/dirs' + qs({ path })); } catch (e) { errorToast(e); return; }
      where.textContent = cur.path;
      const go = (label, path, ic = 'folder') => h('button.ghost', { type: 'button', onclick: () => open(path) }, icon(ic), label);
      mount(list,
        cur.roots.length > 1 ? h('div.row', { style: { gap: '4px', flexWrap: 'wrap' } }, cur.roots.map((d) => go(d, d, 'disk'))) : null,
        cur.parent ? go('..', cur.parent, 'chevron') : null,
        cur.dirs.map((d) => go(d.name, d.path)),
        cur.dirs.length ? null : h('div.faint.small', t('No subfolders')));
    };
    await open(start);
    if (!cur) return null;
    return (await modal(t('Recordings folder'), h('div.col', { style: { gap: '10px' } }, where, list),
      [[t('Cancel'), false, 'ghost'], [t('Choose this folder'), true, 'primary']])) ? cur.path : null;
  }

  // ---------------------------------------------------------- storage
  async function renderStorage() {
    let st;
    try { st = await api.get('/api/library/storage'); } catch (e) { errorToast(e); return; }
    const r = cfg().recording;
    const used = st.total ? (st.total - st.free) / st.total : 0;
    const rec = st.total ? st.bytes / st.total : 0;
    const preview = h('div');
    const drawPreview = (s) => mount(preview, s.retention_pending
      ? h('div.row.wrap', h('span.chip.warn', t('{n} recordings ({size}) are past these limits', { n: s.retention_pending.toLocaleString(), size: fmtBytes(s.retention_bytes) })),
        h('button.danger', {
          onclick: async () => {
            if (!(await confirmDialog(t('Clean up now'), t('Delete {n} recordings now?', { n: s.retention_pending.toLocaleString() }), t('Delete'), true))) return;
            const res = await api.post('/api/library/retention');
            toast(t('Deleted {n}', { n: res.deleted }), 'good');
            renderStorage();
          },
        }, t('Clean up now')))
      : h('span.faint.small', r.retention_days || r.retention_max_gb ? t('Nothing to clean up right now.') : t('Automatic clean-up is off - recordings are kept forever.')));
    const refresh = async () => { try { drawPreview(await api.get('/api/library/storage')); } catch { /* ignore */ } };
    const later = () => setTimeout(refresh, 600);
    drawPreview(st);
    mount(body('storage'),
      h('div.col', { style: { gap: '6px' } },
        h('div.usage', h('span', { style: { width: `${rec * 100}%`, background: 'var(--accent)' } }), h('span', { style: { width: `${Math.max(0, used - rec) * 100}%`, background: 'var(--line-2)' } })),
        h('div.row.wrap.small.muted',
          h('span', h('b', { style: { color: 'var(--accent)' } }, '■ '), t('Recordings: {size}', { size: fmtBytes(st.bytes) })),
          h('span', t('Free: {size} of {total}', { size: fmtBytes(st.free), total: fmtBytes(st.total) })),
          h('span', t('{n} files', { n: st.count.toLocaleString() })),
          st.oldest ? h('span', t('oldest from {d}', { d: fmtDate(st.oldest) })) : null,
          st.mirrored ? h('span', t('{n} copied to another computer', { n: st.mirrored.toLocaleString() })) : null),
        h('div.faint.small.mono', st.root)),
      h('div.divider'),
      h('h3', t('Automatic clean-up')),
      setting(t('Delete recordings older than'), t('Days. 0 = never.'),
        h('div.row', numberInput(r.retention_days, (v) => { patchConfig({ recording: { retention_days: Math.max(0, Math.round(v)) } }); later(); }, { min: 0 }), h('span.muted', t('days')))),
      setting(t('Keep the library under'), t('When it grows bigger, the oldest recordings go first. 0 = no limit.'),
        h('div.row', numberInput(r.retention_max_gb, (v) => { patchConfig({ recording: { retention_max_gb: Math.max(0, v) } }); later(); }, { min: 0 }), h('span.muted', 'GB'))),
      setting(t('Never delete favorites'), '', toggle('', r.retention_keep_favorites, (v) => { patchConfig({ recording: { retention_keep_favorites: v } }); later(); })),
      setting(t('Only delete what another computer has copied'), t('Safe mode for hybrid setups: a recording is only removed from here once a connected computer holds a verified copy.'),
        toggle('', r.retention_require_mirrored, (v) => { patchConfig({ recording: { retention_require_mirrored: v } }); later(); })),
      preview,
      h('p.faint.small', { style: { margin: 0 } }, t('Clean-up runs every 10 minutes. You can also delete any selection by hand on the Recordings page.')));
  }

  // ---------------------------------------------------------- transcription & computers
  const workersBox = h('div.col');
  function renderTranscription() {
    const tc = cfg().transcription;
    const langs = h('input', { value: tc.candidate_languages.join(', '), style: { width: '140px' }, onchange: (e) => patchConfig({ transcription: { candidate_languages: e.target.value.split(/[,\s]+/).filter(Boolean) } }) });
    const advanced = h('details',
      h('summary.small.muted', { style: { cursor: 'pointer' } }, t('Whisper models and options')),
      h('div.col', { style: { marginTop: '12px' } },
        setting(t('General model'), t('Used for Polish and other languages.'), h('input', { value: tc.generic_model, onchange: (e) => patchConfig({ transcription: { generic_model: e.target.value.trim() } }) })),
        setting(t('English ATC model'), t('Fine-tuned for English air traffic control.'), h('input', { value: tc.english_atc_model, style: { width: 'min(420px, 100%)' }, onchange: (e) => patchConfig({ transcription: { english_atc_model: e.target.value.trim() } }) })),
        setting(t('Run on'), '', select([['auto', t('Automatic (GPU if available)')], ['cuda', 'GPU (CUDA)'], ['cpu', 'CPU']], tc.device, (v) => patchConfig({ transcription: { device: v } }))),
        setting(t('Expected languages'), t('ISO codes, e.g. en, pl. Detection picks between these.'), langs),
        setting(t('Skip recordings shorter than'), t('Seconds - a squelch crackle is not worth transcribing.'), numberInput(tc.min_duration_sec, (v) => patchConfig({ transcription: { min_duration_sec: Math.max(0, v) } }), { min: 0, step: 0.1 }))));
    mount(body('transcription'),
      setting(t('Transcribe new recordings automatically'), t('Transcription runs on whichever computers below have it switched on - the server itself, or your PC with a GPU.'),
        toggle('', tc.enabled, (v) => patchConfig({ transcription: { enabled: v } }))),
      advanced,
      h('div.divider'),
      h('div.row', h('h3', t('Computers')), h('span.spacer')),
      workersBox,
      h('details',
        h('summary.small.muted', { style: { cursor: 'pointer' } }, t('Add another computer (e.g. your PC with a GPU)')),
        h('div.col', { style: { marginTop: '10px' } },
          h('p.small.muted', { style: { margin: 0 } }, t('On that computer start FalconSDR, open Settings → Connect to a server and enter:')),
          h('code.cmd', location.origin),
          S.settings.password_set ? h('p.small.muted', { style: { margin: 0 } }, t('…and this server\'s password.')) : null,
          h('p.small.muted', { style: { margin: 0 } }, t('It then shows up here. Turn on transcription or local copies for it, and if it already has recordings of its own, send them here with one click.')))));
    loadWorkers();
  }

  async function loadWorkers() {
    let res;
    try { res = await api.get('/api/workers'); } catch { return; }
    if (!res.workers.length) { mount(workersBox, h('div.empty', t('No computers connected yet.'))); return; }
    mount(workersBox, res.workers.map((w) => workerCard(w, res.pending)));
  }

  function workerCard(w, pending) {
    const info = w.info || {};
    const s = w.settings;
    const update = (patch) => api.post('/api/workers/update', { name: w.name, settings: patch }).then(loadWorkers).catch(errorToast);
    const command = (c) => api.post('/api/workers/command', { name: w.name, command: c }).then(() => toast(t('Sent - the computer picks it up within a few seconds'))).catch(errorToast);
    const status = { idle: t('idle'), transcribing: t('transcribing'), copying: t('copying recordings'), uploading: t('uploading recordings'), installing: t('installing transcription…'), uninstalling: t('uninstalling transcription…') }[info.status] || info.status;
    const uninstall = async () => {
      const models = h('input', { type: 'checkbox', checked: true });
      const ok = await modal(t('Uninstall transcription'), h('div.col', { style: { gap: '10px' } },
        h('p', t('Removes the transcription software from {name}. Recordings and the transcripts made so far stay.', { name: w.name })),
        h('label.row', { style: { gap: '8px', alignItems: 'flex-start' } }, models,
          h('span', t('Also delete the downloaded Whisper models (several GB)'), h('br'), h('small.muted', t('Other programs using the same models would download them again.'))))),
      [[t('Cancel'), false, 'ghost'], [t('Uninstall'), true, 'danger solid']]);
      if (ok) command(models.checked ? 'uninstall_with_models' : 'uninstall');
    };
    const mirrorDir = h('input', { value: s.mirror_dir, placeholder: info.mirror_root || t('its own recordings folder'), style: { width: 'min(360px, 100%)' }, onchange: (e) => update({ mirror_dir: e.target.value }) });
    return h('div.worker',
      h('div.row.wrap',
        icon(info.embedded ? 'server' : 'cpu'),
        h('b', w.name), info.embedded ? h('span.chip', t('this server')) : null,
        h('span.chip', { class: w.online ? 'good' : '' }, h('span.dot'), w.online ? status : t('offline')),
        info.gpu ? h('span.chip.accent', info.gpu) : null,
        h('span.spacer'),
        !w.online && !info.embedded ? h('button.ghost.small', { onclick: () => api.post('/api/workers/forget', { name: w.name }).then(loadWorkers) }, t('Forget')) : null),
      info.last_seen && !w.online ? h('span.faint.small', t('Last seen {ago}', { ago: fmtAgo(new Date(info.last_seen * 1000).toISOString()) })) : null,
      setting(t('Transcribe here'), info.installed === false ? t('Whisper isn\'t installed on this computer yet.')
        : s.transcribe && !cfg().transcription.enabled ? h('span', { style: { color: 'var(--warn)' } }, t('Automatic transcription is off above - this computer only gets recordings you queue by hand.'))
          : (s.transcribe && pending ? t('{n} recordings waiting', { n: pending.toLocaleString() }) : ''),
        h('div.row.wrap',
          info.installed === false && w.online ? h('button', { onclick: () => command('install') }, icon('download'), t('Install')) : null,
          info.installed === false && w.online && info.gpu ? h('button', { onclick: () => command('install_gpu') }, t('Install with GPU support')) : null,
          info.installed === true && w.online && info.status === 'idle' ? h('button.ghost.danger', { onclick: () => uninstall() }, icon('trash'), t('Uninstall')) : null,
          toggle('', s.transcribe, (v) => update({ transcribe: v })))),
      info.embedded ? null : setting(t('Keep a copy of every recording'), t('Copied to this folder on that computer (verified by size).'),
        toggle('', s.mirror, (v) => update({ mirror: v }))),
      !info.embedded && s.mirror ? h('label.field', t('Folder on that computer'), mirrorDir) : null,
      !info.embedded && info.local_only ? h('div.row.wrap',
        h('span.chip.warn', t('{n} recordings exist only on that computer', { n: info.local_only.toLocaleString() })),
        h('button.primary', {
          onclick: async () => {
            if (!(await confirmDialog(t('Send recordings to the server'), t('Upload {n} recordings (with their transcripts and favorites) from {name} to this server? They stay on that computer too.', { n: info.local_only.toLocaleString(), name: w.name }), t('Upload')))) return;
            command('upload_local');
          },
        }, icon('upload'), t('Send them here'))) : null,
      !info.embedded && w.online ? h('div.row', h('span.spacer'), h('button.ghost.small', {
        onclick: async () => {
          if (!(await confirmDialog(t('Disconnect computer'), t('{name} stops working for this server and goes back to its own local mode.', { name: w.name }), t('Disconnect'), true))) return;
          command('detach');
        },
      }, t('Disconnect'))) : null,
      info.log && info.log.length ? h('pre', info.log.slice(-6).join('\n')) : null);
  }

  // ---------------------------------------------------------- connect (local mode)
  function renderConnect() {
    if (!cards.connect) return;
    const url = h('input', { placeholder: 'http://192.168.1.10:8765', style: { width: 'min(320px, 100%)' } });
    const pass = h('input', { type: 'password', placeholder: t('Password (if the server has one)') });
    const btn = h('button.primary', {
      onclick: async () => {
        btn.disabled = true;
        try {
          const res = await api.post('/api/client/connect', { url: url.value, token: pass.value });
          toast(t('Connected - opening the server…'), 'good');
          setTimeout(() => { location.href = `${res.url}/#token=${encodeURIComponent(res.token)}`; }, 1500);
        } catch (e) { errorToast(e); btn.disabled = false; }
      },
    }, icon('link'), t('Connect'));
    mount(body('connect'),
      h('p.muted', { style: { margin: 0 } }, t('Have a FalconSDR server elsewhere (e.g. an always-on receiver)? Connect this computer to it: it stops scanning on its own and instead transcribes and/or keeps copies of the server\'s recordings. You can come back to local mode any time.')),
      h('div.row.wrap', url, pass, btn));
  }

  // ---------------------------------------------------------- access
  function renderAccess() {
    const pass = h('input', { type: 'password', placeholder: t('New password'), autocomplete: 'new-password' });
    mount(body('access'),
      h('p.muted', { style: { margin: 0 } }, S.settings.password_set
        ? t('A password is set - everyone (including helper computers) needs it.')
        : t('No password: anyone who can reach this address on your network can use it. Set one to keep other people on the network out.')),
      h('div.row.wrap', pass,
        h('button.primary', {
          onclick: async () => {
            if (!pass.value.trim()) return;
            await api.post('/api/access', { token: pass.value.trim() });
            toast(t('Password set. Helper computers need it too - reconnect them.'), 'good');
            await loadSettings(); renderAccess();
          },
        }, S.settings.password_set ? t('Change password') : t('Set password')),
        S.settings.password_set ? h('button.ghost', {
          onclick: async () => {
            if (!(await confirmDialog(t('Remove password'), t('Anyone on the network will be able to use the receiver again.'), t('Remove'), true))) return;
            await api.post('/api/access', { token: '' });
            await loadSettings(); renderAccess();
          },
        }, t('Remove password')) : null,
        S.settings.password_set ? h('button.ghost', { onclick: async () => { await api.post('/api/logout'); location.reload(); } }, icon('logout'), t('Log out')) : null));
  }

  // ---------------------------------------------------------- location
  function renderLocation() {
    const loc = cfg().location;
    const coord = (key, value) => h('input', { type: 'number', step: '0.0001', value: value ?? '', placeholder: '–', style: { width: '120px' }, onchange: (e) => patchConfig({ location: { [key]: e.target.value === '' ? null : +e.target.value } }) });
    mount(body('location'),
      h('p.muted', { style: { margin: 0 } }, t('Used for the FlightRadar24 / ADSBExchange flight replay buttons next to aviation recordings (118-138 MHz and military UHF 225-400 MHz). Approximate is fine.')),
      h('div.row.wrap',
        h('label.field', t('Latitude'), coord('home_lat', loc.home_lat)),
        h('label.field', t('Longitude'), coord('home_lon', loc.home_lon)),
        navigator.geolocation ? h('button', {
          onclick: () => navigator.geolocation.getCurrentPosition(
            (p) => { patchConfig({ location: { home_lat: +p.coords.latitude.toFixed(3), home_lon: +p.coords.longitude.toFixed(3) } }); renderLocation(); },
            () => toast(t('The browser did not share a location.'), 'bad')),
        }, icon('globe'), t('Use this device\'s location')) : null));
  }

  // ---------------------------------------------------------- recency boost
  function renderScheduling() {
    const rb = cfg().recency_boost;
    mount(body('scheduling'),
      setting(t('Come back sooner after a transmission'), t('Replies usually follow within seconds, so a frequency that was just active gets extra visits for a while - without stopping the rest of the scan.'),
        toggle('', rb.enabled, (v) => patchConfig({ recency_boost: { enabled: v } }))),
      setting(t('For how long'), t('Seconds after the transmission ends.'), numberInput(rb.duration_s, (v) => patchConfig({ recency_boost: { duration_s: Math.max(0, v) } }), { min: 0 })),
      setting(t('Extra visit every'), t('Seconds.'), numberInput(rb.interval_s, (v) => patchConfig({ recency_boost: { interval_s: Math.max(0.5, v) } }), { min: 0.5, step: 0.5 })));
  }

  // ---------------------------------------------------------- presets
  function renderPresets() {
    const presets = S.settings.presets;
    const edit = async (p) => {
      const v = await formDialog(p ? t('Edit preset') : t('Add preset'), [
        { key: 'label', label: t('Name'), value: p ? t(p.label) : '' },
        { key: 'tooltip', label: t('Description'), value: p ? p.tooltip : '' },
        { key: 'start', label: t('Start (MHz)'), type: 'number', value: p ? p.start_hz / 1e6 : '' },
        { key: 'stop', label: t('Stop (MHz)'), type: 'number', value: p ? p.stop_hz / 1e6 : '' },
        { key: 'step', label: t('Step (kHz)'), type: 'number', value: p ? p.step_hz / 1e3 : 25 },
        { key: 'modulation', label: t('Modulation'), type: 'select', value: p ? p.modulation : 'AM', options: ['AM', 'NFM', 'WFM', 'SSB'].map((m) => [m, m]) },
      ]);
      if (!v || !(v.stop >= v.start) || !(v.step > 0)) return;
      try {
        await api.post('/api/presets/save', { id: p ? p.id : '', label: v.label.trim() || t('Custom range'), tooltip: v.tooltip.trim(), start_hz: Math.round(v.start * 1e6), stop_hz: Math.round(v.stop * 1e6), step_hz: Math.round(v.step * 1e3), modulation: v.modulation });
        await loadSettings(); renderPresets();
      } catch (e) { errorToast(e); }
    };
    const fileInput = h('input', {
      type: 'file', accept: '.json,application/json', class: 'hidden', onchange: async (e) => {
        try {
          const res = await api.post('/api/presets/import', JSON.parse(await e.target.files[0].text()));
          toast(t('Imported {n} presets', { n: res.count }), 'good');
          await loadSettings(); renderPresets();
        } catch (err) { errorToast(err); }
        e.target.value = '';
      },
    });
    const exportPresets = () => {
      const own = presets.filter((p) => p.user).map(({ user, ...p }) => p);
      if (!own.length) { toast(t('You don\'t have any custom presets to export yet.')); return; }
      const a = h('a', { href: URL.createObjectURL(new Blob([JSON.stringify(own, null, 2)], { type: 'application/json' })), download: 'band_presets.json' });
      a.click();
    };
    mount(body('presets'),
      h('p.muted', { style: { margin: 0 } }, t('The ready-made bands offered on the Listen page. Removing one doesn\'t touch bands you already added.')),
      h('div.table-wrap', h('table', h('tbody', presets.map((p) => h('tr',
        h('td', t(p.label), p.user ? h('span.chip', { style: { marginLeft: '8px' } }, t('custom')) : null),
        h('td.mono.small', p.start_hz === p.stop_hz ? fmtMHz(p.start_hz, 3) : `${fmtMHz(p.start_hz, 3)}–${fmtMHz(p.stop_hz, 3)}`),
        h('td.small', p.modulation),
        h('td', h('div.actions',
          h('button.icon.ghost', { title: t('Edit'), onclick: () => edit(p) }, icon('edit')),
          h('button.icon.ghost', {
            title: t('Delete'), onclick: async () => {
              if (!(await confirmDialog(t('Delete preset'), t('Delete preset “{label}”?', { label: t(p.label) }), t('Delete'), true))) return;
              await api.post('/api/presets/delete', { id: p.id }); await loadSettings(); renderPresets();
            },
          }, icon('trash'))))))))),
      h('div.row.wrap',
        h('button', { onclick: () => edit(null) }, icon('plus'), t('Add preset')),
        h('button.ghost', { onclick: () => fileInput.click() }, icon('upload'), t('Import…')),
        h('button.ghost', { onclick: exportPresets }, icon('download'), t('Export')),
        fileInput));
  }

  // ---------------------------------------------------------- appearance
  function renderAppearance() {
    mount(body('appearance'),
      setting(t('Language'), t('“Automatic” follows each browser\'s language.'),
        select([['auto', t('Automatic')], ...S.settings.languages.map((l) => [l.code, l.name])], cfg().language, (v) => {
          patchConfig({ language: v });
          setTimeout(() => location.reload(), 600);
        })),
      setting(t('Start listening when the scanner starts'), t('Stored in this browser.'),
        toggle('', localStorage.getItem('autolisten') !== '0', (v) => localStorage.setItem('autolisten', v ? '1' : '0'))));
  }

  // ---------------------------------------------------------- advanced
  function renderAdvanced() {
    mount(body('advanced'),
      setting(t('Debug mode'), t('Adds a “Simulation (no hardware)” device for testing without a receiver.'),
        toggle('', cfg().debug_mode, async (v) => { patchConfig({ debug_mode: v }); setTimeout(loadSettings, 700); })),
      setting(t('Reset to defaults'), t('Settings, Memory List, blacklist and custom presets back to a fresh install. Recordings, the password and connected computers are kept.'),
        h('button.danger', {
          onclick: async () => {
            if (!(await confirmDialog(t('Reset to defaults'), t('Reset all settings, the Memory List and the blacklist?'), t('Reset'), true))) return;
            await api.post('/api/reset');
            await loadSettings();
            toast(t('Done'), 'good');
            renderAll();
          },
        }, t('Reset…'))),
      S.settings.mode === 'local' && S.settings.capabilities.autostart !== null ? setting(t('Start with the system'), t('Starts FalconSDR in the background when you log in to this computer, without opening the browser.'),
        toggle('', S.settings.capabilities.autostart, async (v, e) => {
          try {
            S.settings.capabilities.autostart = (await api.post('/api/autostart', { enabled: v })).enabled;
          } catch (err) { errorToast(err); }
          e.target.checked = S.settings.capabilities.autostart;
        })) : null,
      S.settings.mode === 'local' ? setting(t('Quit FalconSDR'), t('Stops the scanner and closes the program running in the background. Start it again from the menu or with the same command.'),
        h('button', {
          onclick: async () => {
            if (!(await confirmDialog(t('Quit FalconSDR'), t('Stop scanning and close the program?'), t('Quit'), true))) return;
            await api.post('/api/quit');
            document.body.replaceChildren(h('div.center-page', h('div.card.login', h('h2', 'FalconSDR'), h('p.muted', t('Closed. You can close this tab.')))));
          },
        }, icon('logout'), t('Quit'))) : null,
      h('p.faint.small', { style: { margin: 0 } }, `FalconSDR ${S.settings.version} · ${S.settings.mode} · ${lang}`));
  }

  function renderAll() {
    renderRecording(); renderStorage(); renderTranscription(); renderConnect(); renderAccess();
    renderLocation(); renderScheduling(); renderPresets(); renderAppearance(); renderAdvanced();
  }
  renderAll();
  timers.push(setInterval(loadWorkers, 3000));
  const sub = (hash.match(/^#\/settings\/(\w+)/) || [])[1];
  if (sub && cards[sub]) setTimeout(() => cards[sub].scrollIntoView({ block: 'start' }), 50);
  return { destroy() { timers.forEach(clearInterval); offs.forEach((f) => f()); } };
}
