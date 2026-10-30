// "Recordings": the library on the server - browse by frequency/band/language,
// search transcripts, play with a seekable waveform, edit transcripts, and
// favorite/transcribe/delete one or many.
import {
  LANGUAGE_NAMES, api, carrierHz, fmtChannel, confirmDialog, debounce, errorToast, fmtBytes, fmtDate, fmtDateShort, fmtDuration, fmtKhz, fmtTime,
  formDialog, h, icon, modal, mount, prefs, qs, qualityBadge, select, seg, slider, t, toast, toggle,
} from '../core.js';
import { S, cfg, on, patchConfig, saveChannels } from '../state.js';

const PAGE = 120;
// Kept across visits to the tab.
const F = { mode: 'frequencies', sel: [], q: '', range: 'all', minq: 0, sort: 'new', facetSort: 'count', facetQ: '', autoNext: localStorage.getItem('autonext') !== '0' };

function rangeSince(range) {
  const now = Date.now() / 1000;
  if (range === 'today') { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }
  return { '24h': now - 86400, '7d': now - 7 * 86400, '30d': now - 30 * 86400 }[range];
}

// Flight replays only make sense for aircraft voice: civil airband and NATO military UHF.
const isAviation = (khz) => (khz >= 118000 && khz <= 138000) || (khz >= 225000 && khz <= 400000);

function freqLabel(item) {
  return item.manual ? t('Manual recording') : `${fmtChannel(item.freq_khz * 1000)} MHz`;
}

export function transcriptStatus(tr, queued) {
  if (!tr) return queued ? t('Waiting for transcription…') : t('Not transcribed yet');
  return {
    no_speech: t('No speech detected (just noise)'), skipped_short: t('Too short to transcribe'),
    error: t('Transcription failed - try again'),
  }[tr.status] || '';
}

export function recordingsView(root) {
  const offs = [];
  const preset = sessionStorage.getItem('openFreq');  // from Statistics
  if (preset) { F.sel = [preset]; F.mode = 'frequencies'; sessionStorage.removeItem('openFreq'); }
  let items = [], total = 0, loading = false, current = null, selected = new Set(), facets = null, anchor = null, lastRes = null;
  const audio = new Audio();
  audio.preload = 'metadata';

  // ---------------------------------------------------------- layout
  const facetList = h('div.list');
  const facetSearch = h('input', { placeholder: t('Filter…'), value: F.facetQ, oninput: (e) => { F.facetQ = e.target.value; renderFacets(); } });
  const aside = h('aside.card',
    seg([['frequencies', t('Frequencies')], ['bands', t('Bands')], ['languages', t('Languages')]], F.mode, (v) => { F.mode = v; renderFacets(); }),
    h('div.row', h('div.grow', facetSearch), select([['count', t('Most')], ['recent', t('Latest')], ['freq', t('A–Z')], ['quality', t('Best')]], F.facetSort, (v) => { F.facetSort = v; renderFacets(); }, { style: { width: 'auto' } })),
    facetList);

  const search = h('input', { type: 'search', placeholder: t('Search frequency, name, date or transcript…'), value: F.q, oninput: debounce((e) => { F.q = e.target.value; reload(); }, 300) });
  const toolbar = h('div.toolbar',
    h('div.search', icon('search'), search),
    select([['all', t('Any time')], ['today', t('Today')], ['24h', t('Last 24 h')], ['7d', t('Last 7 days')], ['30d', t('Last 30 days')]], F.range, (v) => { F.range = v; reload(); }),
    h('div.minq', slider({ name: t('Quality'), min: 0, max: 100, step: 5, value: F.minq, format: (v) => (+v ? `${v}+` : t('Any quality')), oninput: debounce((v) => { F.minq = v; reload(); }, 300) })),
    select([['new', t('Newest first')], ['old', t('Oldest first')], ['quality', t('Best quality')], ['long', t('Longest')]], F.sort, (v) => { F.sort = v; reload(); }));
  const statusLine = h('div.row.wrap.small.muted');
  const recordingNow = h('div.row.wrap.small.hidden');
  const bulk = h('div.bulk.hidden');
  const list = h('div.rec-list');
  const more = h('div.empty.hidden', h('button', { onclick: () => load() }, t('Load more')));
  const player = h('div.player.hidden');
  const main = h('section.card', toolbar, statusLine, recordingNow, bulk, list, more, player);

  mount(root, h('div.page', h('div.page-head', h('h1', t('Recordings'))), h('div.library', aside, main)));

  const io = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting) && items.length < total) load(); });
  io.observe(more);

  // ---------------------------------------------------------- data
  function params(extra = {}) {
    return { sel: F.sel, q: F.q, since: rangeSince(F.range), minq: F.minq || '', ...extra };
  }

  async function reload() {
    items = [];
    total = 0;
    selected.clear();
    list.replaceChildren(h('div.empty', t('Loading…')));
    await load(true);
  }

  async function load(first = false) {
    if (loading) return;
    loading = true;
    try {
      const res = await api.get('/api/library' + qs(params({ sort: F.sort, offset: items.length, limit: PAGE })));
      if (first) list.replaceChildren();
      items.push(...res.items);
      total = res.total;
      res.items.forEach((it) => list.append(row(it)));
      if (!items.length) list.replaceChildren(h('div.empty', icon('library'), h('div', F.q || F.sel.length ? t('Nothing matches these filters.') : t('No recordings yet. They appear here as soon as a transmission ends.'))));
      more.classList.toggle('hidden', items.length >= total);
      lastRes = res;
      renderStatus();
      renderBulk();
    } catch (e) { errorToast(e); }
    loading = false;
  }

  function renderStatus() {
    const res = lastRes;
    mount(statusLine,
        h('span', t('{n} recordings', { n: total.toLocaleString() })), '·', h('span', fmtBytes(res.total_bytes)),
        res.pending ? h('span.chip.accent', t('{n} waiting for transcription', { n: res.pending.toLocaleString() })) : null,
        h('span.spacer'),
        toggle(t('Auto-transcribe'), cfg().transcription.enabled, (v) => patchConfig({ transcription: { enabled: v } })),
        toggle(t('UTC'), prefs.utc, (v) => { prefs.utc = v; reload(); }),
        toggle(t('Auto-play next'), F.autoNext, (v) => { F.autoNext = v; localStorage.setItem('autonext', v ? '1' : '0'); }));
  }

  async function loadFacets() {
    try { facets = await api.get('/api/library/facets'); renderFacets(); } catch (e) { errorToast(e); }
  }

  // ---------------------------------------------------------- sidebar
  function facetRow(key, label, sub, count, quality) {
    const active = key === 'all' ? !F.sel.length : F.sel.includes(key);
    return h('div.facet', {
      class: active ? 'active' : '',
      onclick: (e) => {
        if (key === 'all') F.sel = [];
        else if (e.ctrlKey || e.metaKey) F.sel = F.sel.includes(key) ? F.sel.filter((k) => k !== key) : [...F.sel, key];
        else F.sel = F.sel.length === 1 && F.sel[0] === key ? [] : [key];
        renderFacets();
        reload();
      },
      title: t('Ctrl+click to pick several'),
    }, quality === undefined ? h('span') : qualityBadge(quality), h('div.label', label, sub ? h('small', sub) : null), h('span.count', count.toLocaleString()));
  }

  function renderFacets() {
    if (!facets) return;
    let rows = facets[F.mode].map((f) => {
      if (F.mode === 'frequencies') return { ...f, label: f.freq_khz === 0 ? h('span', t('Manual recording')) : h('b', fmtChannel(f.freq_khz * 1000)), sub: f.name || '', text: `${fmtChannel(f.freq_khz * 1000)} ${fmtKhz(f.freq_khz)} ${f.name}`, sortv: f.freq_khz };
      if (F.mode === 'bands') return { ...f, label: h('span', t(f.label)), sub: '', text: t(f.label), sortv: f.start_hz };
      return { ...f, label: h('span', f.language ? (LANGUAGE_NAMES[f.language] || f.language.toUpperCase()) : t('(no transcript)')), sub: '', text: f.language, sortv: f.language };
    });
    const q = F.facetQ.trim().toLowerCase();
    if (q) rows = rows.filter((r) => r.text.toLowerCase().includes(q));
    const by = {
      count: (a, b) => b.count - a.count, recent: (a, b) => b.last.localeCompare(a.last),
      freq: (a, b) => (a.sortv > b.sortv ? 1 : -1), quality: (a, b) => (b.quality ?? -1) - (a.quality ?? -1),
    }[F.facetSort];
    rows.sort(by);
    mount(facetList,
      facetRow('all', h('span', t('All recordings')), '', facets.total, undefined),
      facets.favorites ? facetRow('fav', h('span', '★ ', t('Favorites')), '', facets.favorites, undefined) : null,
      h('div.divider'),
      rows.map((r) => facetRow(r.key, r.label, r.sub, r.count, r.quality)));
  }

  // ---------------------------------------------------------- list
  function row(it) {
    const tr = it.transcript;
    const text = tr && tr.text ? tr.text : transcriptStatus(tr, it.queued);
    const el = h('div.rec', { class: (current && current.id === it.id ? 'active ' : '') + (selected.has(it.id) ? 'selected' : ''), onclick: (e) => pick(it, e) },
      h('input.chk', { type: 'checkbox', checked: selected.has(it.id), 'aria-label': t('Select'), onclick: (e) => { e.stopPropagation(); pick(it, e, true); } }),
      h('div.when', h('b', fmtTime(it.time)), fmtDateShort(it.time)),
      h('div.what', h('b', freqLabel(it)), it.name ? h('span.name', it.name) : null, h('p', { class: tr && tr.text ? '' : 'none' }, text)),
      h('span.dur', fmtDuration(it.duration)),
      h('span.qcell', qualityBadge(it.quality)),
      h('button.star', { class: it.favorite ? 'on' : '', title: t('Favorite'), onclick: (e) => { e.stopPropagation(); setFavorite([it.id], !it.favorite); } }, icon('star')));
    el.dataset.id = it.id;
    return el;
  }

  // Finder-style selection: click opens, Ctrl/Cmd+click toggles, Shift+click selects a range.
  function pick(it, e, fromCheckbox = false) {
    if (e.shiftKey && anchor) {
      const a = items.findIndex((x) => x.id === anchor), b = items.findIndex((x) => x.id === it.id);
      items.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => selected.add(x.id));
    } else if (e.ctrlKey || e.metaKey || fromCheckbox) {
      selected.has(it.id) ? selected.delete(it.id) : selected.add(it.id);
      anchor = it.id;
    } else {
      selected.clear();
      anchor = it.id;
      open(it);
    }
    syncSelection();
  }

  function syncSelection() {
    list.querySelectorAll('.rec').forEach((r) => {
      const on = selected.has(r.dataset.id);
      r.classList.toggle('selected', on);
      r.querySelector('input').checked = on;
    });
    renderBulk();
  }

  function refreshRow(it) {
    const old = list.querySelector(`.rec[data-id="${CSS.escape(it.id)}"]`);
    if (old) old.replaceWith(row(it));
  }

  function renderBulk() {
    bulk.classList.toggle('hidden', !selected.size);
    if (!selected.size) return;
    mount(bulk, h('div.row.wrap',
      h('b', t('{n} selected', { n: selected.size.toLocaleString() })),
      selected.size < total ? h('button.ghost', { onclick: selectAll }, t('Select all {n}', { n: total.toLocaleString() })) : null,
      h('span.spacer'),
      h('button', { onclick: () => setFavorite([...selected], true) }, icon('star'), t('Favorite')),
      h('button', { onclick: () => transcribe([...selected]) }, icon('text'), t('Transcribe')),
      h('button.danger', { onclick: () => remove([...selected]) }, icon('trash'), t('Delete')),
      h('button.ghost', { onclick: () => { selected.clear(); syncSelection(); } }, t('Clear'))));
  }

  async function selectAll() {
    const ids = await api.get('/api/library/ids' + qs(params()));
    ids.forEach((id) => selected.add(id));
    syncSelection();
  }

  // ---------------------------------------------------------- actions
  async function setFavorite(ids, favorite) {
    try {
      await api.post('/api/library/favorite', { ids, favorite });
      items.filter((i) => ids.includes(i.id)).forEach((i) => { i.favorite = favorite; refreshRow(i); });
      if (current && ids.includes(current.id)) renderPlayer();
      loadFacets();
    } catch (e) { errorToast(e); }
  }

  async function transcribe(ids, language) {
    try {
      await api.post('/api/library/transcribe', { ids, language: language || null });
      items.filter((i) => ids.includes(i.id)).forEach((i) => { i.queued = true; refreshRow(i); });
      toast(ids.length === 1 ? t('Queued for transcription') : t('{n} recordings queued for transcription', { n: ids.length }));
      if (current && ids.includes(current.id)) { current.queued = true; renderPlayer(); }
    } catch (e) { errorToast(e); }
  }

  async function remove(ids) {
    const ok = await confirmDialog(t('Delete recordings'), ids.length === 1 ? t('Permanently delete this recording?') : t('Permanently delete {n} recordings?', { n: ids.length.toLocaleString() }), t('Delete'), true);
    if (!ok) return;
    try {
      const res = await api.post('/api/library/delete', { ids });
      toast(t('Deleted {n}', { n: res.deleted }), 'good');
      if (current && ids.includes(current.id)) closePlayer();
      ids.forEach((id) => selected.delete(id));
      reload();
      loadFacets();
    } catch (e) { errorToast(e); }
  }

  // ---------------------------------------------------------- player
  let peaks = [];
  const wave = h('canvas');
  const waveBox = h('div.wave', { onclick: (e) => { const r = waveBox.getBoundingClientRect(); if (audio.duration) audio.currentTime = ((e.clientX - r.left) / r.width) * audio.duration; } }, wave);
  const playBtn = h('button.primary.icon', { title: t('Play or pause (Space)'), onclick: () => (audio.paused ? audio.play() : audio.pause()) });
  const timeText = h('span.mono.small.muted');

  function open(it, autoplay = true) {
    current = it;
    list.querySelectorAll('.rec.active').forEach((r) => r.classList.remove('active'));
    const el = list.querySelector(`.rec[data-id="${CSS.escape(it.id)}"]`);
    if (el) { el.classList.add('active'); el.scrollIntoView({ block: 'nearest' }); }
    audio.src = '/api/recording?id=' + encodeURIComponent(it.id);
    if (autoplay) audio.play().catch(() => {});
    if ('mediaSession' in navigator) navigator.mediaSession.metadata = new MediaMetadata({ title: [freqLabel(it), it.name].filter(Boolean).join(' · '), artist: `${fmtDateShort(it.time)} ${fmtTime(it.time)}`, album: 'FalconSDR' });
    peaks = [];
    api.get('/api/recording/peaks?id=' + encodeURIComponent(it.id)).then((p) => { if (current === it) { peaks = p; drawWave(); } }).catch(() => {});
    renderPlayer();
  }

  function closePlayer() {
    audio.pause();
    current = null;
    player.classList.add('hidden');
  }

  function step(dir) {
    if (!current) return;
    const i = items.findIndex((x) => x.id === current.id);
    const next = items[i + dir];
    if (next) open(next);
    else if (dir > 0 && items.length < total) load().then(() => { const n = items[i + 1]; if (n) open(n); });
  }

  function drawWave() {
    const dpr = window.devicePixelRatio || 1;
    const W = (wave.width = waveBox.clientWidth * dpr), H = (wave.height = waveBox.clientHeight * dpr);
    const ctx = wave.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    const pos = audio.duration ? audio.currentTime / audio.duration : 0;
    const styles = getComputedStyle(document.documentElement);
    const played = styles.getPropertyValue('--accent').trim();
    const ahead = styles.getPropertyValue('--secondary').trim();  // must stay visible on the card in both themes
    const n = peaks.length || 1;
    const bw = W / n;
    for (let i = 0; i < peaks.length; i++) {
      const v = Math.max(0.02, peaks[i]);
      ctx.fillStyle = (i + 1) / n <= pos ? played : ahead;
      ctx.fillRect(i * bw, (H - v * H * 0.92) / 2, Math.max(1, bw - dpr * 0.6), v * H * 0.92);
    }
    if (!peaks.length) {
      ctx.fillStyle = ahead;
      ctx.fillRect(0, H / 2 - dpr, W, 2 * dpr);
    }
    // playhead
    ctx.fillStyle = played;
    ctx.fillRect(Math.min(W - 2 * dpr, W * pos), 0, 2 * dpr, H);
    timeText.textContent = `${fmtDuration(audio.currentTime)} / ${fmtDuration(audio.duration || current?.duration)}`;
  }

  function renderPlayer() {
    const it = current;
    if (!it) return;
    player.classList.remove('hidden');
    const tr = it.transcript;
    const loc = cfg().location;
    const when = new Date(it.time);
    const pad = (n) => String(n).padStart(2, '0');
    const ymd = `${when.getUTCFullYear()}-${pad(when.getUTCMonth() + 1)}-${pad(when.getUTCDate())}`;
    const hm = `${pad(when.getUTCHours())}:${pad(when.getUTCMinutes())}`;
    const langSel = select([['', t('Detect language')], ['pl', 'Polski'], ['en', 'English']], '', () => {});
    const textBox = h('div.transcript', { class: tr && tr.text ? '' : 'none' }, tr && tr.text ? tr.text : transcriptStatus(tr, it.queued));
    mount(player,
      h('div.row.wrap',
        h('div.grow',
          h('div', h('b.mono', { style: { fontSize: '16px' } }, freqLabel(it)), it.name ? h('span.muted', ` · ${it.name}`) : null),
          h('div.small.muted', `${fmtDate(it.time)} ${fmtTime(it.time)}${prefs.utc ? ' UTC' : ''}`, ' · ', fmtDuration(it.duration),
            it.snr !== null ? ` · SNR ${it.snr} dB` : '')),
        qualityBadge(it.quality),
        h('button.icon.star', { class: it.favorite ? 'on' : '', title: t('Favorite'), onclick: () => setFavorite([it.id], !it.favorite) }, icon('star')),
        h('button.icon.ghost', { title: t('Close'), onclick: closePlayer }, icon('x'))),
      waveBox,
      h('div.row.wrap',
        h('button.icon', { title: t('Previous'), 'aria-label': t('Previous'), onclick: () => step(-1) }, icon('prev')),
        playBtn,
        h('button.icon', { title: t('Next'), 'aria-label': t('Next'), onclick: () => step(1) }, icon('next')),
        timeText,
        h('span.spacer'),
        it.manual ? null : act(icon('plus'), t('Save frequency'), () => saveFrequency(it), t('Add this frequency to the Memory List')),
        // Plain names, no logos: both are trademarks (Flightradar24 terms 10.3; ADS-B Exchange forbids using
        // its icon without the wordmark). The name stays visible on phones too - two same icons would be indistinguishable.
        ...(isAviation(it.freq_khz) ? [
          site('Flightradar24', `https://www.flightradar24.com/${ymd}/${hm}/20x/${loc.home_lat},${loc.home_lon}/9`),
          site('ADS-B Exchange', `https://globe.adsbexchange.com/?replay=${ymd}-${hm}&lat=${loc.home_lat}&lon=${loc.home_lon}&zoom=8.8`)] : []),
        h('a.btn.ghost', { href: `/api/recording?id=${encodeURIComponent(it.id)}&download=1`, title: t('Download'), 'aria-label': t('Download') }, icon('download')),
        h('button.icon.ghost.danger', { title: t('Delete'), 'aria-label': t('Delete'), onclick: () => remove([it.id]) }, icon('trash'))),
      h('div.col', { style: { gap: '8px' } },
        h('div.row.wrap', h('h3', t('Transcript')),
          tr && tr.language && ['ok', 'low_confidence'].includes(tr.status) ? h('span.chip', LANGUAGE_NAMES[tr.language] || tr.language) : null,
          tr && tr.status === 'low_confidence' ? h('span.chip.warn', t('low confidence')) : null,
          tr && tr.engine === 'manual' ? h('span.chip', t('edited')) : null,
          h('span.spacer'),
          act(icon('edit'), t('Edit'), () => editTranscript(it)),
          langSel,
          act(icon('refresh'), tr ? t('Re-transcribe') : t('Transcribe'), () => transcribe([it.id], langSel.value))),
        textBox));
    updatePlayButton();
    requestAnimationFrame(drawWave);
  }

  function openMap(url) {
    const loc = cfg().location;
    if (loc.home_lat === null || loc.home_lon === null) {
      toast(t('Set your receiver location in Settings first.'));
      location.hash = '#/settings/location';
      return;
    }
    window.open(url, '_blank', 'noopener');
  }

  function site(name, url) {
    return h('button.ghost', { onclick: () => openMap(url), title: `${name} - ${t('Flight replay at this moment')}`, 'aria-label': name },
      icon('external'), h('span.lbl.keep', name));
  }

  // Icon + label; on phones only the icon shows (the label stays for screen readers and the tooltip).
  function act(iconEl, label, onclick, title) {
    return h('button.ghost', { onclick, title: title || label, 'aria-label': label }, iconEl, h('span.lbl', label));
  }

  function updatePlayButton() { mount(playBtn, icon(audio.paused ? 'play' : 'pause')); }

  async function editTranscript(it) {
    const area = h('textarea', { value: it.transcript ? it.transcript.text : '', rows: 6, style: { width: '100%' } });
    const ok = await modal(t('Edit transcript'), area, [[t('Cancel'), false, 'ghost'], [t('Save'), true, 'primary']]);
    if (!ok) return;
    try {
      await api.post('/api/library/transcript', { id: it.id, text: area.value });
      it.transcript = { ...(it.transcript || { language: '' }), text: area.value.trim(), status: 'ok', engine: 'manual' };
      refreshRow(it);
      renderPlayer();
    } catch (e) { errorToast(e); }
  }

  async function saveFrequency(it) {
    const channels = S.settings.channels;
    const hz = carrierHz(it.freq_khz / 1000);  // filenames keep whole kHz; back onto the exact raster
    const existing = channels.find((c) => c.freq_hz === hz);
    const folders = [...new Set([...cfg().channel_folders, ...channels.map((c) => c.folder).filter(Boolean)])];
    const dl = h('datalist#folders', folders.map((f) => h('option', { value: f })));
    document.body.append(dl);
    const v = await formDialog(t('Save {f} MHz', { f: fmtChannel(hz) }), [
      { key: 'name', label: t('Name'), value: existing ? existing.name : '', placeholder: t('e.g. Downtown Tower') },
      { key: 'folder', label: t('Folder'), value: existing ? existing.folder : '', list: 'folders' },
    ]);
    dl.remove();
    if (!v) return;
    if (existing) Object.assign(existing, { name: v.name.trim(), folder: v.folder.trim() });
    else channels.push({ freq_hz: hz, name: v.name.trim(), modulation: 'AM', enabled: true, folder: v.folder.trim(), weight: 0 });
    if (v.folder.trim() && !cfg().channel_folders.includes(v.folder.trim())) patchConfig({ channel_folders: [...cfg().channel_folders, v.folder.trim()] });
    await saveChannels(channels);
    toast(t('Saved to the Memory List'), 'good');
    items.filter((x) => x.freq_khz === it.freq_khz).forEach((x) => { x.name = v.name.trim(); refreshRow(x); });
    renderPlayer();
  }

  audio.addEventListener('play', updatePlayButton);
  audio.addEventListener('pause', updatePlayButton);
  audio.addEventListener('timeupdate', drawWave);
  audio.addEventListener('ended', () => { updatePlayButton(); if (F.autoNext) step(1); });
  // Lock screen / headset / notification controls (Android keeps playing in the background).
  const MEDIA_ACTIONS = { play: () => audio.play(), pause: () => audio.pause(), nexttrack: () => step(1), previoustrack: () => step(-1) };
  const setMediaActions = (on) => { if ('mediaSession' in navigator) for (const [a, fn] of Object.entries(MEDIA_ACTIONS)) { try { navigator.mediaSession.setActionHandler(a, on ? fn : null); } catch { /* unsupported action */ } } };
  audio.addEventListener('play', () => setMediaActions(true));

  const onKey = (e) => {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) || document.querySelector('.modal-back')) return;
    if (e.key === 'Delete' || e.key === 'Backspace') {
      const ids = selected.size ? [...selected] : current ? [current.id] : [];
      if (ids.length) { e.preventDefault(); remove(ids); }
      return;
    }
    if (e.key === 'Escape' && selected.size) { selected.clear(); syncSelection(); return; }
    if (e.key === 'a' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); selectAll(); return; }
    if (!current) return;
    if (e.key === ' ') { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }
    else if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); step(1); }
    else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); step(-1); }
  };
  document.addEventListener('keydown', onKey);

  // New recordings slide in at the top while the newest ones are shown -
  // no reload, so scroll position, selection and the player stay put.
  const freshTimer = setInterval(async () => {
    if (F.sort !== 'new' || loading || !lastRes || document.hidden) return;
    try {
      const res = await api.get('/api/library' + qs(params({ sort: 'new', limit: 40 })));
      const known = new Map(items.map((x) => [x.id, x]));
      // Quality (seconds after closing) and transcripts (later) land on rows already shown.
      res.items.forEach((x) => {
        const it = known.get(x.id);
        if (it && (it.quality !== x.quality || JSON.stringify(it.transcript) !== JSON.stringify(x.transcript))) {
          Object.assign(it, { quality: x.quality, snr: x.snr, duration: x.duration, transcript: x.transcript, queued: x.queued });
          refreshRow(it);
          if (current === it) renderPlayer();
        }
      });
      const fresh = res.items.filter((x) => !known.has(x.id) && (!items.length || x.time > items[0].time));
      if (!fresh.length) return;
      if (!items.length) list.replaceChildren();
      items.unshift(...fresh);
      total = res.total;
      lastRes = { ...lastRes, total_bytes: res.total_bytes, pending: res.pending };
      [...fresh].reverse().forEach((x) => { const r = row(x); r.classList.add('fresh'); list.prepend(r); });
      renderStatus();
      loadFacets();
    } catch { /* offline */ }
  }, 4000);

  // Files still being written aren't in the list yet (they appear once the
  // transmission's hold time ends) - say what's being recorded right now.
  const renderRecordingNow = () => {
    const live = S.live;
    const freqs = live && live.recording && ['SIGNAL_DETECTED', 'LINGER'].includes(live.state) ? live.frequencies : [];
    recordingNow.classList.toggle('hidden', !freqs.length);
    if (freqs.length) {
      mount(recordingNow, h('span.chip.bad.live', h('span.dot'), t('Recording now')),
        freqs.map(([f, n]) => h('span.chip', h('b.mono', fmtChannel(f)), n ? ` ${n}` : '')),
        h('span.muted', t('appears in the list when the transmission ends')));
    }
  };
  offs.push(on('live', renderRecordingNow));
  offs.push(on('settings', () => current && renderPlayer()));
  reload();
  loadFacets();
  return {
    destroy() {
      // removeAttribute + load(): `audio.src = ''` would load the page itself
      audio.pause(); audio.removeAttribute('src'); audio.load();
      io.disconnect(); clearInterval(freshTimer); setMediaActions(false);
      document.removeEventListener('keydown', onKey); offs.forEach((f) => f());
    },
  };
}
