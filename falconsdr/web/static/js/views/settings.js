// "Settings": recording, band presets, location, language/listening, disk & retention, server
// connection, access password and advanced options.
import {
  api, confirmDialog, errorToast, fmtAgo, fmtBytes, fmtDate, fmtDuration, fmtMHz, formDialog, h, icon, lang, modal, mount, prefs, qs, seg, select, t, toast, toggle,
} from '../core.js';
import { S, cfg, confirmRecording, loadSettings, on, patchConfig, patchConfigNow } from '../state.js';
import { decodersCard, receiverName } from './addons.js';
import { notifyCard } from './notify.js';
import { modLabel, modOptions } from './bands.js';

// Transcription and digital voice are add-ons: they live on the Add-ons page (same component, scope 'addons').
const ADDON_SECTIONS = [
  ['decoders', () => t('Decoders & tools')],
  ['transcription', () => t('Transcription & computers')],
  ['digital', () => t('Digital voice (DMR, C4FM)')],
  ['notify', () => t('Notifications')],
];
const SECTIONS = [
  ['recording', () => t('Recording')],
  ['addons', () => t('Transcription & add-ons')],     // a signpost: these settings moved to the Add-ons page
  ['presets', () => t('Band presets')],
  ['location', () => t('Location')],
  ['appearance', () => t('Appearance & language')],
  ['storage', () => t('Disk & retention')],
  ['connect', () => t('Connect to a server')],
  ['access', () => t('Access')],
  ['updates', () => t('Updates')],
  ['receiver', () => t('Receiver health')],
  ['advanced', () => t('Advanced')],
];

function setting(title, desc, control) {
  if (control && control.querySelector) {
    const field = control.matches('input, select') ? control : control.querySelector('input, select');
    if (field && !field.getAttribute('aria-label')) field.setAttribute('aria-label', title);
  }
  return h('div.setting', h('div', h('b', title), desc ? h('p', desc) : null), control);
}

// Whisper models offered by name (faster-whisper downloads one on first use; sizes are approximate).
// Functions, not constants: t() must run after the catalog is loaded. The last choice, "Other", takes any
// Hugging Face repo or local folder.
const OTHER_MODEL = '__other__';
function mainModels() {
  return [
    ['large-v3', `large-v3 - ${t('best quality, all languages')} (≈3 GB)`],
    ['large-v3-turbo', `large-v3-turbo - ${t('almost as good, several times faster')} (≈1.6 GB)`],
    ['medium', `medium - ${t('lighter, all languages')} (≈1.5 GB)`],
    ['small', `small - ${t('for weak computers')} (≈0.5 GB)`],
    ['base', `base - ${t('rough, very fast')} (≈0.15 GB)`],
  ];
}
function englishModels() {
  return [
    ['', t('None - the main model also does English')],
    ['jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper', `jacktol medium.en - ${t('English, tuned for air traffic control')} (≈1.5 GB)`],
    ['distil-large-v3', `distil-large-v3 - ${t('English only, fast and accurate')} (≈1.5 GB)`],
    ['medium.en', `medium.en - ${t('English only')} (≈1.5 GB)`],
    ['small.en', `small.en - ${t('English only, for weak computers')} (≈0.5 GB)`],
  ];
}

// A list of ready-made models plus "Other…" with a free text field for anything else.
function modelPicker(models, value, onpick) {
  const known = models.some(([v]) => v === value);
  const input = h('input', {
    value: known ? '' : value, placeholder: t('Hugging Face repo (user/name) or a folder'),
    style: { width: 'min(420px, 100%)', display: known ? 'none' : '' }, onchange: (e) => onpick(e.target.value.trim()),
  });
  const pick = select([...models, [OTHER_MODEL, t('Other…')]], known ? value : OTHER_MODEL, (v) => {
    if (v === OTHER_MODEL) { input.style.display = ''; input.focus(); } else { input.style.display = 'none'; onpick(v); }
  });
  return h('div.col', { style: { gap: '6px', alignItems: 'stretch' } }, pick, input);
}

function numberInput(value, onchange, attrs = {}) {
  return h('input', { type: 'number', value, step: 'any', style: { width: '110px' }, ...attrs, onchange: (e) => onchange(+e.target.value) });
}

export function settingsView(root, hash, scope = 'settings') {
  const moved = (hash.match(/^#\/settings\/(transcription|digital)/) || [])[1];
  if (scope === 'settings' && moved) {   // old links (a greyed-out modulation, bookmarks)
    location.replace(`#/addons/${moved}`);
    return { destroy() {} };
  }
  const offs = [];
  const timers = [];
  let dead = false;
  const cards = {};
  const visible = (scope === 'addons' ? ADDON_SECTIONS : SECTIONS).filter(([k]) => k !== 'connect' || S.settings.mode === 'local');
  const nav = h('nav', visible.map(([k, label]) => h('a', { href: `#/${scope}/${k}`, onclick: (e) => { e.preventDefault(); cards[k].scrollIntoView({ behavior: 'smooth', block: 'start' }); } }, label())));
  const content = h('div.col', { style: { gap: '16px' } });
  mount(root, h('div.page', h('div.page-head', h('h1', scope === 'addons' ? t('Add-ons') : t('Settings'))),
    h('div.settings', nav, content)));
  for (const [k, label] of visible) {
    cards[k] = h('section.card', { id: `s-${k}` }, h('div.card-head', h('h2', label())), h('div.col'));
    content.append(cards[k]);
  }
  // The render functions below are shared by both pages; a section this page does not show renders into nothing.
  const body = (k) => (cards[k] ? cards[k].lastChild : document.createElement('div'));

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
            try {
              const res = await api.post('/api/library/retention');
              toast(t('Deleted {n}', { n: res.deleted }), 'good');
              renderStorage();
            } catch (e) { errorToast(e); }
          },
        }, t('Clean up now')))
      : h('span.faint.small', r.retention_days || r.retention_max_gb ? t('Nothing to clean up right now.') : t('Automatic clean-up is off - recordings are kept forever.')));
    const refresh = async () => { try { const next = await api.get('/api/library/storage'); if (!dead) drawPreview(next); } catch { /* ignore */ } };
    const saveAndRefresh = async (patch) => { await patchConfigNow(patch); if (!dead) await refresh(); };
    drawPreview(st);
    mount(body('storage'),
      h('div.col', { style: { gap: '6px' } },
        h('div.usage', h('span', { style: { width: `${rec * 100}%`, background: 'var(--accent)' } }), h('span', { style: { width: `${Math.max(0, used - rec) * 100}%`, background: 'var(--fill-strong)' } })),
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
        h('div.row', numberInput(r.retention_days, (v) => saveAndRefresh({ recording: { retention_days: Math.max(0, Math.round(v)) } }), { min: 0 }), h('span.muted', t('days')))),
      setting(t('Keep the library under'), t('When it grows bigger, the oldest recordings go first. 0 = no limit.'),
        h('div.row', numberInput(r.retention_max_gb, (v) => saveAndRefresh({ recording: { retention_max_gb: Math.max(0, v) } }), { min: 0 }), h('span.muted', 'GB'))),
      setting(t('Never delete favorites'), '', toggle('', r.retention_keep_favorites, (v) => saveAndRefresh({ recording: { retention_keep_favorites: v } }))),
      setting(t('Only delete what another computer has copied'), t('Safe mode for hybrid setups: a recording is only removed from here once a connected computer holds a verified copy.'),
        toggle('', r.retention_require_mirrored, (v) => saveAndRefresh({ recording: { retention_require_mirrored: v } }))),
      preview,
      h('p.faint.small', { style: { margin: 0 } }, t('Clean-up runs every 10 minutes. You can also delete any selection by hand on the Recordings page.')));
  }

  // ---------------------------------------------------------- transcription & computers
  const workersBox = h('div.col');
  // A command sent to a computer: it is picked up at its next heartbeat and only then does it show a status,
  // so the card says "waiting" at once instead of leaving a toast and silence. name -> {since, log}
  const waiting = new Map();
  const BUSY = ['installing', 'uninstalling', 'uploading', 'downloading'];
  const lastLog = (info) => (info.log && info.log.length ? info.log[info.log.length - 1] : '');
  let lastWorkers = null;
  let fastPoll = false;
  function renderTranscription() {
    const tc = cfg().transcription;
    const langs = h('input', { value: tc.candidate_languages.join(', '), style: { width: '140px' }, onchange: (e) => patchConfig({ transcription: { candidate_languages: e.target.value.split(/[,\s]+/).filter(Boolean) } }) });
    const advanced = h('details',
      h('summary.small.muted', { style: { cursor: 'pointer' } }, t('Advanced')),
      h('div.col', { style: { marginTop: '12px' } },
        setting(t('Run on'), '', select([['auto', t('Automatic (GPU if available)')], ['cuda', 'GPU (CUDA)'], ['cpu', 'CPU']], tc.device, (v) => patchConfig({ transcription: { device: v } }))),
        setting(t('Skip recordings shorter than'), t('Seconds - a squelch crackle is not worth transcribing.'), numberInput(tc.min_duration_sec, (v) => patchConfig({ transcription: { min_duration_sec: Math.max(0, v) } }), { min: 0, step: 0.1 }))));
    mount(body('transcription'),
      setting(t('Transcribe new recordings automatically'), t('Transcription runs on whichever computers below have it switched on - the server itself, or your PC with a GPU.'),
        toggle('', tc.enabled, (v) => patchConfig({ transcription: { enabled: v } }))),
      setting(t('Main model'), t('Transcribes every recording and detects its language - English too, unless an English model is set below. A bigger model is more accurate and slower; the first use downloads it.'),
        modelPicker(mainModels(), tc.generic_model, (v) => { if (v) patchConfig({ transcription: { generic_model: v } }); })),
      setting(t('English model (optional)'), t('Only for English, e.g. tuned for air traffic control. None: the main model does English too - one download, less memory.'),
        modelPicker(englishModels(), tc.english_atc_model, (v) => patchConfig({ transcription: { english_atc_model: v } }))),
      setting(t('Expected languages'), t('ISO codes, e.g. en, pl. Detection picks between these.'), langs),
      setting(t('Fixed language'), t('Empty: the language of each recording is detected (best for air traffic control, where languages mix). An ISO code, e.g. pl: always that language, no detection - about 40 % less CPU time.'),
        h('input', { value: tc.fixed_language, placeholder: t('detect'), style: { width: '140px' }, onchange: (e) => patchConfig({ transcription: { fixed_language: e.target.value.trim().toLowerCase() } }) })),
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
    for (const w of res.workers) {
      const wt = waiting.get(w.name);
      if (!wt) continue;
      const info = w.info || {};
      const started = BUSY.includes(info.status);
      // A quick command can be over before we ever see its status: a new log line says it answered.
      const answered = info.status === 'idle' && lastLog(info) !== wt.log && !lastLog(info).includes('command from the server');
      if (!w.online || started || answered || Date.now() - wt.since > 300000) waiting.delete(w.name);
    }
    fastPoll = waiting.size > 0 || res.workers.some((w) => BUSY.includes((w.info || {}).status));
    // Don't rebuild the cards under someone who is typing in one of them.
    if (workersBox.contains(document.activeElement) && document.activeElement.matches('input:not([type=checkbox])')) return;
    lastWorkers = res;
    renderWorkers();
  }

  function renderWorkers() {
    if (!lastWorkers) return;
    if (!lastWorkers.workers.length) { mount(workersBox, h('div.empty', t('No computers connected yet.'))); return; }
    mount(workersBox, lastWorkers.workers.map((w) => workerCard(w, lastWorkers.pending)));
  }

  function workerCard(w, pending) {
    const info = w.info || {};
    const s = w.settings;
    const update = (patch) => api.post('/api/workers/update', { name: w.name, settings: patch }).then(loadWorkers).catch(errorToast);
    const command = (c) => {
      waiting.set(w.name, { since: Date.now(), log: lastLog(info) });
      fastPoll = true;
      renderWorkers();
      return api.post('/api/workers/command', { name: w.name, command: c }).then(loadWorkers)
        .catch((e) => { waiting.delete(w.name); renderWorkers(); errorToast(e); });
    };
    const wt = waiting.get(w.name);
    const busy = !!wt || BUSY.includes(info.status);
    const status = { idle: t('idle'), transcribing: t('transcribing'), copying: t('copying recordings'), uploading: t('uploading recordings'), installing: t('installing transcription…'), uninstalling: t('uninstalling transcription…'), downloading: t('downloading the model…') }[info.status] || info.status;
    // What is going on right now, for the operations that take a while.
    const forS = info.status_for_s ? ` (${fmtDuration(info.status_for_s)})` : '';
    const progress = wt && !BUSY.includes(info.status)
      ? h('div.small', { style: { color: Date.now() - wt.since > 40000 ? 'var(--orange)' : 'var(--secondary)' } },
        Date.now() - wt.since > 40000
          ? t('{name} hasn\'t answered yet ({s}) - is FalconSDR running there and connected?', { name: w.name, s: fmtDuration((Date.now() - wt.since) / 1000) })
          : t('Sent to {name} - waiting for it to pick the command up (a few seconds)…', { name: w.name }))
      : (info.status === 'downloading' && info.download
        ? h('div.small', { style: { color: 'var(--secondary)' } }, t('Downloading the model {model}: {size} so far - the first transcription waits for it. This can take several minutes.', { model: info.download.model, size: fmtBytes(info.download.bytes) }))
        : (BUSY.includes(info.status)
          ? h('div.small', { style: { color: 'var(--secondary)' } }, `${status}${forS}`, info.log && info.log.length ? h('span.faint', ` - ${lastLog(info)}`) : null)
          : null));
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
        h('span.chip', { class: (w.online ? 'good' : '') + (w.online && busy ? ' live' : '') }, h('span.dot'), w.online ? (wt && !BUSY.includes(info.status) ? t('command sent…') : status) : t('offline')),
        info.gpu ? h('span.chip.accent', info.gpu) : null,
        h('span.spacer'),
        !w.online && !info.embedded ? h('button.ghost.small', { onclick: () => api.post('/api/workers/forget', { name: w.name }).then(loadWorkers).catch(errorToast) }, t('Forget')) : null),
      info.last_seen && !w.online ? h('span.faint.small', t('Last seen {ago}', { ago: fmtAgo(new Date(info.last_seen * 1000).toISOString()) })) : null,
      progress,
      setting(t('Transcribe here'), info.removal_pending ? h('span', { style: { color: 'var(--orange)' } }, t('Transcription is uninstalled - restart FalconSDR on this computer to remove its files.'))
        : info.installed === false ?t('Whisper isn\'t installed on this computer yet.')
        : s.transcribe && !cfg().transcription.enabled ? h('span', { style: { color: 'var(--orange)' } }, t('Automatic transcription is off above - this computer only gets recordings you queue by hand.'))
          : (s.transcribe && pending ? t('{n} recordings waiting', { n: pending.toLocaleString() }) : ''),
        h('div.row.wrap',
          info.removal_pending && w.online ? h('button.primary', { disabled: busy, onclick: () => command('restart') }, icon('refresh'), t('Restart now')) : null,
          info.installed === false && !info.removal_pending && w.online ? h('button', { disabled: busy, onclick: () => command('install') }, icon('download'), t('Install')) : null,
          info.installed === false && !info.removal_pending && w.online && info.gpu ? h('button', { disabled: busy, onclick: () => command('install_gpu') }, t('Install with GPU support')) : null,
          info.installed === true && w.online && (info.status === 'idle' || wt) ? h('button.ghost.danger', { disabled: busy, onclick: () => uninstall() }, icon('trash'), t('Uninstall')) : null,
          toggle('', s.transcribe, (v) => update({ transcribe: v })))),
      info.embedded ? null : setting(t('Keep a copy of every recording'), t('Copied to this folder on that computer (verified by size).'),
        toggle('', s.mirror, (v) => update({ mirror: v }))),
      !info.embedded && s.mirror ? h('label.field', t('Folder on that computer'), mirrorDir) : null,
      !info.embedded && info.local_only ? h('div.row.wrap',
        h('span.chip.warn', t('{n} recordings exist only on that computer', { n: info.local_only.toLocaleString() })),
        h('button.primary', {
          disabled: busy,
          onclick: async () => {
            if (!(await confirmDialog(t('Send recordings to the server'), t('Upload {n} recordings (with their transcripts and favorites) from {name} to this server? They stay on that computer too.', { n: info.local_only.toLocaleString(), name: w.name }), t('Upload')))) return;
            command('upload_local');
          },
        }, icon('upload'), t('Send them here'))) : null,
      !info.embedded && w.online ? h('div.row', h('span.spacer'), h('button.ghost.small', {
        disabled: busy,
        onclick: async () => {
          if (!(await confirmDialog(t('Disconnect computer'), t('{name} stops working for this server and goes back to its own local mode.', { name: w.name }), t('Disconnect'), true))) return;
          command('detach');
        },
      }, t('Disconnect'))) : null,
      info.log && info.log.length ? h('pre', info.log.slice(-6).join('\n')) : null);
  }

  // ---------------------------------------------------------- digital voice (optional add-on)
  let digitalPoll = null;
  async function renderDigital() {
    let st;
    try { st = await api.get('/api/digital'); } catch (e) { errorToast(e); return; }
    if (dead) return;
    const windows = S.settings.capabilities.platform === 'win32';
    const install = async () => {
      // Shown once, here, at the moment of choosing it - not as a banner elsewhere.
      const ok = await modal(t('Install digital voice'), h('div.col', { style: { gap: '10px' } },
        h('p', h('span.chip.warn', t('Experimental')), ' ', t('Digital voice is new in FalconSDR and hasn\'t been tested with every system and receiver yet.')),
        h('p', t('If something doesn\'t work, please report it - it helps a lot:'), ' ',
          h('a', { href: 'https://github.com/rafafunt/FalconSDR/issues', target: '_blank', rel: 'noopener' }, 'github.com/rafafunt/FalconSDR/issues')),
        h('p.small.muted', windows ? t('Downloads the dsd-fme decoder (about 40 MB) from its authors.')
          : t('Downloads the dsd-fme decoder from its authors and compiles it on this machine - a few minutes.'))),
      [[t('Cancel'), false, 'ghost'], [t('Install'), true, 'primary']]);
      if (!ok) return;
      try { await api.post('/api/digital/install'); } catch (e) { errorToast(e); return; }
      renderDigital();
    };
    const uninstall = async () => {
      if (!(await confirmDialog(t('Uninstall digital voice'), t('Removes the dsd-fme decoder. DMR and C4FM channels then play as plain NFM.'), t('Uninstall'), true))) return;
      try { await api.post('/api/digital/uninstall'); } catch (e) { errorToast(e); return; }
      await loadSettings(); if (!dead) renderDigital();
    };
    clearTimeout(digitalPoll);
    if (st.running) { if (!dead) digitalPoll = setTimeout(renderDigital, 1500); }
    else if (st.installed !== S.settings.capabilities.digital) {
      await loadSettings();  // the modulation pickers follow
      if (dead) return;
      if (st.installed) toast(t('Digital voice installed - pick DMR or C4FM as a band\'s or channel\'s modulation.'), 'good');
    }
    mount(body('digital'),
      h('p.muted', { style: { margin: 0 } }, t('Optional add-on: listens to DMR and Yaesu System Fusion (C4FM) voice using dsd-fme, an independent open-source decoder that FalconSDR downloads from its authors. It runs on the machine that scans. Encrypted calls stay muted - nothing is ever decrypted.')),
      setting(t('Decoder'),
        st.installed ? t('Installed. Choose DMR or C4FM as the modulation of a band or a Memory List channel.')
          : st.running ? t('Installing…') : t('Not installed.'),
        h('div.row.wrap',
          st.installed ? h('span.chip.good', h('span.dot'), t('installed')) : null,
          !st.installed && !st.running ? h('button.primary', { onclick: install }, icon('download'), t('Install')) : null,
          st.installed && !st.running ? h('button.ghost.danger', { onclick: uninstall }, icon('trash'), t('Uninstall')) : null)),
      st.error ? h('div.col', { style: { gap: '6px' } },
        h('span', { style: { color: 'var(--red)' } }, t('Installation failed: {error}', { error: st.error })),
        st.hint ? h('p.small.muted', { style: { margin: 0 } }, t('It needs a compiler and a few libraries. Install them with this command (in a terminal on this machine), then press Install again:')) : null,
        st.hint ? h('code.cmd', st.hint) : null) : null,
      st.log.length ? h('pre.log', st.log.filter((l) => l.trim()).join('\n')) : null);
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
            try {
              await api.post('/api/access', { token: pass.value.trim() });
              toast(t('Password set. Helper computers need it too - reconnect them.'), 'good');
              await loadSettings(); renderAccess();
            } catch (e) { errorToast(e); }
          },
        }, S.settings.password_set ? t('Change password') : t('Set password')),
        S.settings.password_set ? h('button.ghost', {
          onclick: async () => {
            if (!(await confirmDialog(t('Remove password'), t('Anyone on the network will be able to use the receiver again.'), t('Remove'), true))) return;
            try {
              await api.post('/api/access', { token: '' });
              await loadSettings(); renderAccess();
            } catch (e) { errorToast(e); }
          },
        }, t('Remove password')) : null,
        S.settings.password_set ? h('button.ghost', { onclick: async () => { try { await api.post('/api/logout'); location.reload(); } catch (e) { errorToast(e); } } }, icon('logout'), t('Log out')) : null));
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
        { key: 'modulation', label: t('Modulation'), type: 'select', value: p ? p.modulation : 'AM', options: modOptions() },
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
        h('td.small', modLabel(p.modulation)),
        h('td', h('div.actions',
          h('button.icon.ghost', { title: t('Edit'), onclick: () => edit(p) }, icon('edit')),
          h('button.icon.ghost', {
            title: t('Delete'), onclick: async () => {
              if (!(await confirmDialog(t('Delete preset'), t('Delete preset “{label}”?', { label: t(p.label) }), t('Delete'), true))) return;
              try {
                await api.post('/api/presets/delete', { id: p.id }); await loadSettings(); renderPresets();
              } catch (e) { errorToast(e); }
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
      setting(t('Theme'), t('“Automatic” follows this device\'s light or dark setting. Stored in this browser.'),
        seg([['auto', t('Automatic')], ['light', t('Light')], ['dark', t('Dark')]], prefs.theme, (v) => { prefs.theme = v; })),
      setting(t('Language'), t('“Automatic” follows each browser\'s language.'),
        select([['auto', t('Automatic')], ...S.settings.languages.map((l) => [l.code, l.name])], cfg().language, async (v) => {
          await patchConfigNow({ language: v });
          location.reload();
        })),
      setting(t('Start listening when the scanner starts'), t('Stored in this browser.'),
        toggle('', localStorage.getItem('autolisten') !== '0', (v) => localStorage.setItem('autolisten', v ? '1' : '0'))));
  }

  // ---------------------------------------------------------- receiver health + test (sdr_probe.py)
  // Thresholds from issue #6: 0.78-2.16 % lost samples was an audible buzz.
  const pct = (lost, of) => (of >= 10 ? (100 * lost) / of : null);
  // `tr` = t on screen; the copied/GitHub report passes English (an issue is read by the maintainer, in English).
  const english = (s) => s;
  function verdict(r, tr = t) {
    const loss = Math.max(pct(r.gap_s, r.steady_s) ?? 0, pct(r.stream_lost_s, r.stream_s) ?? 0);
    if (r.chunks_dropped > 0 || loss >= 1) return ['bad', tr('Losing data')];
    if (loss >= 0.3 || r.rt_priority === 'refused') return ['warn', tr('Check')];
    return ['good', tr('OK')];
  }
  function healthLines(r, tr = t) {
    const loss = [pct(r.gap_s, r.steady_s), pct(r.stream_lost_s, r.stream_s)].filter((v) => v !== null);
    return [
      `${tr('Lost IQ blocks')}: ${r.chunks_dropped} / ${r.chunks_read}`,
      `${tr('Lost samples')}: ${loss.length ? `${Math.max(...loss).toFixed(2)} %` : tr('not measured yet')}`,
      `${tr('Slowest read')}: ${r.read_max_ms} ms (${tr('one block')} ${r.chunk_ms} ms, ${tr('slower')}: ${r.slow_reads})`,
      `${tr('Queue')}: ${r.queue} / ${r.max_queued}`,
      r.rt_priority === 'unavailable' ? null : `${tr('Real-time priority')}: ${r.rt_priority === 'ok' ? tr('granted') : tr('not granted - check “ulimit -r”')}`,
      ...Object.entries(r.extra || {}).map(([k, v]) => `${k}: ${v}`),   // a backend's own figures (SDRplay: model, API, overloads)
    ].filter(Boolean);
  }
  let health = [], system = '', probe = { running: false, lines: [], code: null };
  let lastHealth = null;   // [time, receivers]: the counters vanish when the scanner stops - which the test needs first
  const healthBox = h('div.col', { style: { gap: '10px' } });
  const report = h('textarea.mono.small', { readonly: true, rows: 10, style: { width: '100%', display: 'none' } });
  const probeBtn = h('button', { onclick: startProbe }, t('Test the receiver'));
  const copyBtn = h('button', { onclick: copyReport, style: { display: 'none' } }, t('Copy report'));
  const issueBtn = h('button', { onclick: reportIssue }, icon('external'), t('Report a problem'));
  function reportText() {
    const [when, rows] = health.length ? [null, health] : (lastHealth || [null, []]);
    return [
      `FalconSDR ${S.settings.version} · ${S.settings.mode} mode`,
      `System: ${system}`,
      ...(rows.length ? ['', when ? `While scanning (${when}):` : 'Scanning now:'] : []),
      ...rows.flatMap((r) => [`Receiver ${r.receiver + 1} [${r.backend}]: ${verdict(r, english)[1]}`,
        ...healthLines(r, english).map((l) => `  ${l}`)]),
      ...(probe.lines.length ? ['', 'Receiver test (--probe-sdr):', ...probe.lines] : []),
    ].join('\n');
  }
  function paintHealth() {
    mount(healthBox, health.length ? health.map((r) => {
      const [kind, label] = verdict(r);
      return h('div', h('div.row', { style: { gap: '8px' } }, h('b', receiverName(r.receiver)), h(`span.chip.${kind}`, h('span.dot'), label)),
        h('p.small.muted.mono', { style: { margin: '4px 0 0', whiteSpace: 'pre-line' } }, healthLines(r).join('\n')));
    }) : h('p.muted', { style: { margin: 0 } }, t('Shown while the scanner is running.')));
    probeBtn.disabled = probe.running;
    probeBtn.textContent = !probe.running ? t('Test the receiver')
      : probe.steps ? t('Testing… step {n} of {m} · {s} s', { n: probe.step, m: probe.steps, s: probe.elapsed_s })
        : t('Starting the test… {s} s', { s: probe.elapsed_s ?? 0 });
    const shown = probe.running || probe.lines.length > 0;
    report.style.display = shown ? '' : 'none';
    copyBtn.style.display = shown || health.length ? '' : 'none';
    issueBtn.disabled = probe.running;
    if (shown) report.value = probe.lines.join('\n');
  }
  async function loadHealth() {
    try {
      ({ receivers: health, system } = await api.get('/api/health'));
      if (health.length) lastHealth = [new Date().toTimeString().slice(0, 8), health];
    } catch { return; }
    if (!dead) paintHealth();
  }
  let probeTimer = null;
  async function startProbe() {
    try { probe = await api.post('/api/probe'); } catch (e) { errorToast(e); return; }
    paintHealth();
    startProbeWatch();
  }
  function startProbeWatch(onDone) {   // also when the page opens while a test runs
    clearInterval(probeTimer);
    probeTimer = setInterval(async () => {   // every second while it runs: the step and the seconds move
      try { probe = await api.get('/api/probe'); } catch { return; }
      if (!probe.running) { clearInterval(probeTimer); if (onDone) onDone(); }
      if (!dead) paintHealth();
    }, 1000);
    timers.push(probeTimer);
  }
  // From the "Report a problem" question: stops the scanner for the test and starts it again afterwards.
  async function testForReport() {
    const wasRunning = !!(S.live && S.live.running);
    try {
      if (wasRunning) await api.post('/api/scanner/stop');
      for (let tries = 0; ; tries += 1) {   // the receiver is free only once every engine has finished
        try { probe = await api.post('/api/probe'); break; } catch (e) {
          if (tries >= 30 || dead) throw e;
          await new Promise((r) => setTimeout(r, 500));
        }
      }
    } catch (e) { errorToast(e); if (wasRunning) api.post('/api/scanner/start').catch(() => {}); return; }
    paintHealth();
    startProbeWatch(async () => {
      if (wasRunning) await api.post('/api/scanner/start').catch(errorToast);
      toast(t('Test finished - now click “Report a problem”.'), 'good');
    });
  }
  // GitHub's issue form (.github/ISSUE_TEMPLATE/receiver.yml) takes the report as a URL parameter; nothing is sent
  // until the user submits it there. A URL over ~8 kB is refused, so a long report is cut (Copy report has it all).
  async function reportIssue() {
    // A report without the test's measurements is the kind that needs a round of questions - offer the test first.
    if (probe.code === null) {
      const choice = await modal(t('Test the receiver first?'), h('div.col', { style: { gap: '10px' } },
        h('p', t('The test measures your receiver in about a minute and answers most of the questions we would otherwise have to ask you. It helps us the most.')),
        S.live && S.live.running ? h('p.small.muted', t('The scanner stops for the test and starts again afterwards.')) : null),
      [[t('Cancel'), null, 'ghost'], [t('Report without the test'), 'skip'], [t('Test the receiver first'), 'test', 'primary']]);
      if (choice === 'test') { testForReport(); return; }
      if (choice !== 'skip') return;
    }
    let report = reportText();
    if (report.length > 5000) report = `${report.slice(0, 5000)}\n[... cut - paste the rest from "Copy report"]`;
    const url = `https://github.com/rafafunt/FalconSDR/issues/new?${new URLSearchParams({ template: 'receiver.yml', report })}`;
    window.open(url, '_blank', 'noopener');
  }
  function copyReport() { copyText(reportText()); }
  function copyText(text) {
    // navigator.clipboard needs HTTPS or localhost; the UI is often plain http://IP (gotcha 40): copy a selection then.
    const viaSelection = () => {
      const tmp = h('textarea', { style: { position: 'fixed', opacity: '0' } });
      tmp.value = text;
      document.body.append(tmp);
      tmp.select();
      const ok = document.execCommand('copy');
      tmp.remove();
      if (!ok) throw new Error('copy');
    };
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).catch(viaSelection)
      .then(() => toast(t('Copied'), 'good'), () => toast(t('Could not copy - select the text and copy it by hand.'), 'bad'));
  }
  function renderReceiver() {
    mount(body('receiver'),
      healthBox,
      setting(t('Test the receiver'), t('Stop the scanner first. Measures the gain steps, retuning and lost samples in about a minute. “Report a problem” opens a bug report on GitHub with this report filled in - nothing is sent until you submit it there.'),
        h('div.row.wrap', probeBtn, copyBtn, issueBtn)),
      report);
    paintHealth();
  }

  // ---------------------------------------------------------- updates (updates.py)
  // What each kind of installation can do with an update; functions, so t() runs after the catalog is loaded.
  const KIND_HELP = () => ({
    'windows-setup': t('Installed with the Windows installer: FalconSDR downloads the new installer, checks it and installs it. Recordings in progress are closed first, settings and recordings are kept.'),
    appimage: t('AppImage: FalconSDR downloads the new version, checks it, replaces its own file and starts again. Settings and recordings are kept.'),
    'windows-zip': t('Portable ZIP: download it, quit FalconSDR and unpack it over the old folder. Settings and recordings are kept.'),
    macos: t('Download it, quit FalconSDR and replace the app with the new one. Settings and recordings are kept.'),
    deb: t('Download the package, then install it from its folder with:'),
    rpm: t('Download the package, then install it from its folder with:'),
    aur: t('Update it with your AUR helper:'),
    git: t('Get the new version, then restart FalconSDR:'),
    source: t('Download the new version from the release page.'),
  });
  let upd = null;
  let updTimer = null;
  async function loadUpdates(force = false) {
    try { upd = force ? await api.post('/api/updates/check') : await api.get('/api/updates'); } catch (e) {
      if (force) errorToast(e);
      return;
    }
    if (!dead) paintUpdates();
  }
  function updateActions(u) {
    const a = u.action, inst = u.install;
    if (a.type === 'install') {
      if (inst.state === 'downloading') return h('p.small', t('Downloading… {n} %', { n: inst.progress }));
      if (inst.state === 'installing') return h('p.small', t('Installing - FalconSDR restarts by itself in a moment.'));
      return h('div.col', { style: { gap: '6px', alignItems: 'flex-start' } },
        inst.state === 'error' ? h('p.small', { style: { color: 'var(--red)', margin: 0 } }, `${t('The update failed')}: ${inst.error}`) : null,
        h('button.primary', {
          onclick: async () => {
            try { upd = await api.post('/api/updates/install'); } catch (e) { errorToast(e); return; }
            paintUpdates();
            clearInterval(updTimer);
            updTimer = setInterval(() => loadUpdates(), 1000);   // progress; fails quietly once the program restarts
            timers.push(updTimer);
          },
        }, icon('download'), t('Install and restart')));
    }
    const download = a.asset ? h('a.btn.primary', { href: a.asset.url, target: '_blank', rel: 'noopener' }, icon('download'),
      `${t('Download')} (${fmtBytes(a.asset.size)})`) : null;
    const command = a.command ? h('div.row', { style: { gap: '8px', alignItems: 'stretch' } }, h('code.cmd', { style: { flex: 1 } }, a.command),
      h('button', { onclick: () => copyText(a.command) }, t('Copy'))) : null;
    const page = !download ? h('a.btn', { href: u.latest.url, target: '_blank', rel: 'noopener' }, icon('external'), t('Open the release page')) : null;
    return h('div.col', { style: { gap: '8px', alignItems: 'flex-start' } }, download, command, a.type === 'command' ? null : page);
  }
  function paintUpdates() {
    const u = upd;
    const check = cfg().updates.check;
    const when = u && u.checked_at ? fmtAgo(new Date(u.checked_at * 1000).toISOString()) : '';
    const line = !u ? '' : [
      `${t('Installed')}: ${u.current}`,
      u.error ? `${t('Could not check')}: ${u.error}` : u.available ? null : u.latest ? t('This is the newest version.') : null,
      when ? `${t('Checked')} ${when}` : null,
    ].filter(Boolean).join(' · ');
    mount(body('updates'),
      setting(t('Check for updates'), t('Once a day FalconSDR asks GitHub (github.com) for the number of the latest version. Nothing about you, your receiver or your recordings is sent.'),
        toggle('', !!check, async (v) => {
          await patchConfigNow({ updates: { check: v } });
          if (v) setTimeout(() => loadUpdates(), 3000);
          paintUpdates();
        })),
      setting(t('Version'), line, h('button', { onclick: (e) => { e.currentTarget.disabled = true; loadUpdates(true); } }, icon('refresh'), t('Check now'))),
      u && u.available ? h('div.update-box',
        h('b', t('FalconSDR {version} is available', { version: u.latest.version })),
        u.latest.notes ? h('div.update-notes', u.latest.notes.replace(/^#+\s*/gm, '')) : null,   // Markdown headings as plain lines
        h('a.small', { href: u.latest.url, target: '_blank', rel: 'noopener' }, t('All changes on GitHub')),
        h('p.small', { style: { margin: 0 } }, KIND_HELP()[u.kind] || ''),
        updateActions(u)) : null);
  }

  // ---------------------------------------------------------- advanced
  function renderAdvanced() {
    mount(body('advanced'),
      setting(t('Debug mode'), t('Adds a “Simulation (no hardware)” device for testing without a receiver.'),
        toggle('', cfg().debug_mode, async (v) => { await patchConfigNow({ debug_mode: v }); if (!dead) await loadSettings(); })),
      setting(t('Continuous RTL-SDR reading'), t('While the scanner stays on a frequency, an RTL-SDR read through librtlsdr (Windows, macOS and the receiver) is read as a continuous stream, which removes the buzz caused by lost samples. If it crashes the driver on your receiver, FalconSDR switches it off by itself and says so in the Activity list; the buzz may come back. Takes effect the next time scanning starts.'),
        toggle('', cfg().device.async_read, (v) => patchConfig({ device: { async_read: v } }))),
      setting(t('Reset to defaults'), t('Settings, Memory List, blacklist and custom presets back to a fresh install. Recordings, the password and connected computers are kept.'),
        h('button.danger', {
          onclick: async () => {
            if (!(await confirmDialog(t('Reset to defaults'), t('Reset all settings, the Memory List and the blacklist?'), t('Reset'), true))) return;
            try {
              await api.post('/api/reset');
              await loadSettings();
              toast(t('Done'), 'good');
              if (!dead) renderAll();
            } catch (e) { errorToast(e); }
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
            try {
              await api.post('/api/quit');
              document.body.replaceChildren(h('div.center-page', h('div.card.login', h('h2', 'FalconSDR'), h('p.muted', t('Closed. You can close this tab.')))));
            } catch (e) { errorToast(e); }
          },
        }, icon('logout'), t('Quit'))) : null,
      h('p.faint.small', { style: { margin: 0 } }, `FalconSDR ${S.settings.version} · ${S.settings.mode} · ${lang}`));
  }

  let stopDecoders = null;
  let stopNotify = null;
  function renderAddonsSignpost() {
    mount(body('addons'),
      h('p.muted', { style: { margin: 0 } }, t('Transcription, digital voice and the decoders (ACARS, APRS, VDL2, rtl_433) now have their own page.')),
      h('div.row.wrap',
        h('button.primary', { onclick: () => { location.hash = '#/addons/transcription'; } }, icon('settings'), t('Go to transcription settings')),
        h('button', { onclick: () => { location.hash = '#/addons'; } }, t('All add-on settings'))),
      h('p.faint.small', { style: { margin: 0 } },
        t('For example: the Whisper model and the computers that transcribe · DMR and C4FM digital voice · ACARS and APRS decoders · rtl_433 sensors on 433 or 868 MHz · VDL2.')));
  }

  function renderAll() {
    if (scope === 'addons') {
      renderTranscription(); renderDigital();
      if (!stopDecoders) stopDecoders = decodersCard(body('decoders'));
      if (!stopNotify) stopNotify = notifyCard(body('notify'));
      return;
    }
    renderRecording(); renderAddonsSignpost(); renderStorage(); renderConnect(); renderAccess();
    renderLocation(); renderPresets(); renderAppearance(); paintUpdates(); loadUpdates(); renderReceiver(); renderAdvanced();
  }
  renderAll();
  if (scope === 'settings') {
    loadHealth();
    api.get('/api/probe').then((p) => { probe = p; if (!dead) paintHealth(); if (p.running) startProbeWatch(); }, () => {});
    timers.push(setInterval(loadHealth, 2000));
  }
  // Every second while something is going on with a computer (so a click shows its effect), else every 3 s.
  let pollTick = 0;
  if (scope === 'addons') timers.push(setInterval(() => { pollTick += 1; if (fastPoll || pollTick % 3 === 0) loadWorkers(); }, 1000));
  const sub = (hash.match(/^#\/(?:settings|addons)\/(\w+)/) || [])[1];
  let stopFollowing = () => {};
  if (sub && cards[sub]) {
    cards[sub].classList.add('spotlight');  // arrived from a link (e.g. a greyed-out modulation) - show where
    // Room below the last card, or it cannot be scrolled up to the top of the page.
    content.append(h('div', { 'aria-hidden': 'true', style: { minHeight: '70vh' } }));
    // The cards above (decoders, workers) fill in after the page opens and push this one down: stay on it until
    // the page has settled or the user scrolls.
    const go = () => cards[sub].scrollIntoView({ block: 'start' });
    setTimeout(go, 50);
    const observer = new ResizeObserver(go);
    observer.observe(content);
    const events = ['wheel', 'touchstart', 'keydown', 'mousedown'];
    const stop = () => { observer.disconnect(); events.forEach((e) => window.removeEventListener(e, stop)); };
    events.forEach((e) => window.addEventListener(e, stop, { passive: true }));
    const timeout = setTimeout(stop, 4000);
    stopFollowing = () => { clearTimeout(timeout); stop(); };
  }
  return { destroy() { dead = true; timers.forEach(clearInterval); clearTimeout(digitalPoll); offs.forEach((f) => f()); if (stopDecoders) stopDecoders(); if (stopNotify) stopNotify(); stopFollowing(); } };
}
