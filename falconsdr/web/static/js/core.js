// Shared helpers: API calls, translation, DOM building, icons, dialogs, formatting.

// ---------------------------------------------------------------- API
export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function request(method, path, body) {
  const opts = { method, headers: { 'X-FalconSDR': '1' }, cache: 'no-store' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (r.status === 401 && !path.startsWith('/api/login')) {
    location.reload();  // session gone - back to the login screen
  }
  if (!r.ok) throw new ApiError(r.status, (data && data.error) || r.statusText);
  return data;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
};

export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x)); else u.append(k, v);
  }
  const s = u.toString();
  return s ? '?' + s : '';
}

// ---------------------------------------------------------------- i18n
let catalog = {};
export let lang = 'en';

export async function loadLanguage() {
  const want = (navigator.language || 'en').slice(0, 2);
  const res = await api.get('/api/i18n' + qs({ lang: want }));
  catalog = res.catalog || {};
  lang = res.lang;
  document.documentElement.lang = lang;
}

// English source text in, translated text out; {name} placeholders filled from vars.
export function t(text, vars) {
  let s = catalog[text] || text;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}

// ---------------------------------------------------------------- DOM
// h('div.card#id', {onclick, title, ...}, child, [children], 'text')
export function h(tag, attrs, ...children) {
  if (attrs === null || typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs)) {
    children.unshift(attrs);
    attrs = {};
  }
  const [name, ...rest] = tag.split(/(?=[.#])/);
  const el = name === 'svg' || name === 'path' ? document.createElementNS('http://www.w3.org/2000/svg', name) : document.createElement(name || 'div');
  for (const part of rest) {
    if (part[0] === '.') el.classList.add(part.slice(1));
    else el.id = part.slice(1);
  }
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className += ' ' + v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k in el && !(el instanceof SVGElement) && k !== 'list') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  // Icon-only buttons: their tooltip doubles as the screen-reader label.
  if ((name === 'button' || name === 'a') && attrs.title && !attrs['aria-label'] && !el.textContent.trim()) el.setAttribute('aria-label', attrs.title);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// ---------------------------------------------------------------- icons
const ICONS = {
  listen: 'M4.9 19.1a10 10 0 0 1 0-14.2M19.1 4.9a10 10 0 0 1 0 14.2M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  library: 'M2 10v3M6 6v11M10 3v18M14 8v7M18 5v13M22 10v3',
  channels: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  stats: 'M3 3v18h18M7 16v-3M12 16V8M17 16v-6',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  play: 'M6 4l14 8-14 8z',
  pause: 'M7 4h3v16H7zM14 4h3v16h-3z',
  stop: 'M6 6h12v12H6z',
  skip: 'M5 4l10 8-10 8zM19 5v14',
  ban: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M5.6 5.6l12.8 12.8',
  record: 'M12 12m-6 0a6 6 0 1 0 12 0a6 6 0 1 0-12 0',
  star: 'M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z',
  trash: 'M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6',
  download: 'M12 3v12M7 10l5 5 5-5M5 21h14',
  upload: 'M12 21V9M7 14l5-5 5 5M5 3h14',
  search: 'M11 11m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0M21 21l-5-5',
  plus: 'M12 5v14M5 12h14',
  edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4',
  x: 'M6 6l12 12M18 6L6 18',
  check: 'M5 12l5 5L20 7',
  chevron: 'M6 9l6 6 6-6',
  prev: 'M19 4L9 12l10 8zM5 5v14',
  next: 'M5 4l10 8-10 8zM19 5v14',
  volume: 'M4 9v6h4l5 4V5L8 9zM16 9a4 4 0 0 1 0 6M19 6a8 8 0 0 1 0 12',
  mute: 'M4 9v6h4l5 4V5L8 9zM17 9l5 6M22 9l-5 6',
  headphones: 'M3 18v-6a9 9 0 0 1 18 0v6M21 19a2 2 0 0 1-2 2h-1v-6h3zM3 19a2 2 0 0 0 2 2h1v-6H3z',
  refresh: 'M3 12a9 9 0 0 1 15.5-6.3L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.3L3 16M3 21v-5h5',
  external: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  cpu: 'M6 6h12v12H6zM9 9h6v6H9zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4',
  server: 'M3 4h18v7H3zM3 13h18v7H3zM7 7.5h.01M7 16.5h.01',
  waves: 'M2 12h2l2-6 3 12 3-14 3 16 3-10 2 4h2',
  folder: 'M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z',
  disk: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  text: 'M4 6h16M4 12h16M4 18h10',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  link: 'M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1',
  clock: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 7v5l3 2',
  globe: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18',
};

export function icon(name, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', name === 'play' || name === 'pause' || name === 'stop' || name === 'record' || name === 'skip' || name === 'prev' || name === 'next' ? 'currentColor' : 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('class', cls ? `ic ${cls}` : 'ic');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name] || '');
  svg.append(p);
  return svg;
}

// ---------------------------------------------------------------- widgets
export function toggle(label, checked, onchange, { hint, disabled } = {}) {
  const input = h('input', { type: 'checkbox', checked, disabled, onchange: (e) => onchange(e.target.checked, e) });
  return h('label.switch', input, h('span.label', label, hint ? h('small', hint) : null));
}

export function select(options, value, onchange, attrs = {}) {
  const el = h('select', { ...attrs, onchange: (e) => onchange(e.target.value, e) },
    options.map(([v, label]) => h('option', { value: v, selected: String(v) === String(value) }, label)));
  return el;
}

export function seg(options, value, onchange) {
  const wrap = h('div.seg');
  for (const [v, label] of options) {
    wrap.append(h('button', {
      class: String(v) === String(value) ? 'active' : '',
      onclick: () => { [...wrap.children].forEach((b) => b.classList.remove('active')); btn(v).classList.add('active'); onchange(v); },
      'data-v': v,
    }, label));
  }
  const btn = (v) => [...wrap.children].find((b) => b.dataset.v === String(v));
  return wrap;
}

// Range slider with a live value read-out; onchange fires (debounced by caller) while dragging.
export function slider({ name, min, max, step = 1, value, unit = '', format, oninput, badge }) {
  const fmt = format || ((v) => `${(+v).toFixed(step < 1 ? 1 : 0)} ${unit}`.trim());
  const out = h('span.value', fmt(value));
  const input = h('input', { type: 'range', min, max, step, value, oninput: (e) => { out.textContent = fmt(e.target.value); oninput(+e.target.value); } });
  const el = h('div.slider', { class: badge ? 'override' : '' }, h('span.name', { 'data-badge': badge || '' }, name), out, input);
  el.set = (v) => { input.value = v; out.textContent = fmt(v); };
  el.input = input;
  return el;
}

export function debounce(fn, ms = 300) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

export function toast(message, kind = '') {
  const el = h('div.toast', { class: kind }, message);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 3500);
}

export function errorToast(e) {
  toast(e && e.message ? e.message : String(e), 'bad');
}

// A modal dialog; resolves with the clicked button's value (or null on Esc/backdrop).
export function modal(title, body, buttons = [[t('Close'), null]]) {
  return new Promise((resolve) => {
    const close = (v) => { back.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    const back = h('div.modal-back', { onclick: (e) => { if (e.target === back) close(null); } },
      h('div.modal', { role: 'dialog' },
        h('h2', title),
        typeof body === 'string' ? h('p', body) : body,
        h('div.actions', buttons.map(([label, value, cls]) => h('button', { class: cls || '', onclick: () => close(value) }, label)))));
    document.body.append(back);
    document.addEventListener('keydown', onKey);
    const focus = back.querySelector('input, textarea, select') || back.querySelector('.actions button:last-child');
    if (focus) focus.focus();
  });
}

export async function confirmDialog(title, text, okLabel, danger = false) {
  return (await modal(title, text, [[t('Cancel'), false, 'ghost'], [okLabel || t('OK'), true, danger ? 'danger solid' : 'primary']])) === true;
}

// Form dialog: fields = [{key, label, type, value, options, hint}] -> values object or null.
export async function formDialog(title, fields, okLabel) {
  const inputs = {};
  const form = h('form.col', { onsubmit: (e) => { e.preventDefault(); back.querySelector('.actions button.primary').click(); } },
    fields.map((f) => {
      let input;
      if (f.type === 'select') input = select(f.options, f.value, () => {});
      else if (f.type === 'textarea') input = h('textarea', { value: f.value ?? '' });
      else input = h('input', { type: f.type || 'text', value: f.value ?? '', step: f.step || 'any', placeholder: f.placeholder || '', list: f.list || null });
      inputs[f.key] = input;
      return h('label.field', f.label, input, f.hint ? h('span.hint', f.hint) : null);
    }));
  const promise = modal(title, form, [[t('Cancel'), false, 'ghost'], [okLabel || t('Save'), true, 'primary']]);
  const back = form.closest('.modal-back');
  if (!(await promise)) return null;
  const out = {};
  for (const f of fields) out[f.key] = f.type === 'number' ? parseFloat(inputs[f.key].value) : inputs[f.key].value;
  return out;
}

// ---------------------------------------------------------------- formatting
export const prefs = {
  get utc() { return localStorage.getItem('utc') === '1'; },
  set utc(v) { localStorage.setItem('utc', v ? '1' : '0'); },
};

export function fmtMHz(hz, digits = 4) { return (hz / 1e6).toFixed(digits); }
export function fmtKhz(khz) { return (khz / 1000).toFixed(3); }

// Airband (118-137 MHz) voice is 8.33 kHz-channelled in the EU: pilots dial a
// channel *name* - carrier 126.8000 is "126.805", 126.8083 "126.810", 126.8167
// "126.815". A carrier on the 25 kHz grid is ambiguous: many stations (most ACC
// sectors, e.g. 130.875) are still published as 25 kHz channels, so it shows as
// the 25 kHz name unless a Memory List channel there was typed as 8.33 (ch833).
// The 121.500 guard stays a 25 kHz channel. Mirrors models.airband_channel().
const GUARD_HZ = 121_500_000;
let ch833Blocks = new Set();
export function setCh833(channels) { ch833Blocks = new Set(channels.filter((c) => c.ch833).map((c) => Math.floor(c.freq_hz / 25000) * 25000)); }
export function airbandChannel(hz) {
  if (hz < 118e6 || hz > 137e6 || Math.abs(hz - GUARD_HZ) < 500) return '';
  const block = Math.floor(hz / 25000) * 25000;
  const k = Math.round((hz - block) / (25000 / 3));
  if (k > 2 || Math.abs(hz - block - k * 25000 / 3) > 500) return '';
  if (k === 0 && !ch833Blocks.has(block)) return (block / 1e6).toFixed(3);
  return ((block + [5000, 10000, 15000][k]) / 1e6).toFixed(3);
}

// True when a typed airband channel is an 8.33 name on a 25 kHz-grid carrier
// ("126.805"), as opposed to the 25 kHz channel at that carrier ("126.800").
export function typedAs833(mhz) {
  const hz = Math.round(mhz * 1e6);
  return hz >= 118e6 && hz <= 137e6 && hz % 25000 === 5000;
}

// A single channel as people know it: the 8.33 channel name in the airband, MHz elsewhere.
export function fmtChannel(hz, digits = 4) {
  return airbandChannel(hz) || fmtMHz(hz, hz >= 118e6 && hz <= 137e6 ? 3 : digits);
}

// What someone types (a published 8.33 channel name like 126.805, or a carrier)
// -> the exact carrier in Hz, snapped to the 8.33 raster in the airband.
export function carrierHz(mhz) {
  const hz = Math.round(mhz * 1e6);
  if (hz < 118e6 || hz > 137e6 || Math.abs(hz - GUARD_HZ) < 500) return hz;
  const block = Math.floor(hz / 25000) * 25000, off = hz - block;
  if ([5000, 10000, 15000].includes(off)) return Math.round(block + (off / 5000 - 1) * 25000 / 3);
  const snapped = Math.round(block + Math.round(off / (25000 / 3)) * 25000 / 3);
  return Math.abs(snapped - hz) <= 1500 ? snapped : hz;  // 134.474 is 134.475, not a second channel
}

export function fmtDuration(sec) {
  if (sec === null || sec === undefined) return '–';
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m}:${String(s).padStart(2, '0')}`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function fmtBytes(n) {
  if (n === null || n === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
}

function dtParts(iso) {
  const d = new Date(iso);
  const opts = prefs.utc ? { timeZone: 'UTC' } : {};
  return { d, opts };
}

export function fmtTime(iso, withSeconds = true) {
  const { d, opts } = dtParts(iso);
  return d.toLocaleTimeString(lang, { ...opts, hour: '2-digit', minute: '2-digit', second: withSeconds ? '2-digit' : undefined });
}

export function fmtDate(iso) {
  const { d, opts } = dtParts(iso);
  return d.toLocaleDateString(lang, { ...opts, day: 'numeric', month: 'short', year: 'numeric' });
}

export function fmtDateShort(iso) {
  const { d, opts } = dtParts(iso);
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  if (same && !prefs.utc) return t('Today');
  return d.toLocaleDateString(lang, { ...opts, day: 'numeric', month: 'short' });
}

export function fmtAgo(iso) {
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return t('just now');
  if (sec < 3600) return t('{n} min ago', { n: Math.round(sec / 60) });
  if (sec < 86400) return t('{n} h ago', { n: Math.round(sec / 3600) });
  return t('{n} days ago', { n: Math.round(sec / 86400) });
}

export function qualityBadge(q) {
  if (q === null || q === undefined) return h('span.q', '–');
  return h('span.q', { class: q >= 70 ? 'hi' : q >= 40 ? 'mid' : 'lo', title: t('Quality') }, Math.round(q));
}

export const LANGUAGE_NAMES = {
  pl: 'Polski', en: 'English', de: 'Deutsch', fr: 'Français', ru: 'Русский', uk: 'Українська', tr: 'Türkçe',
  ar: 'العربية', no: 'Norsk', fi: 'Suomi', sv: 'Svenska', es: 'Español', it: 'Italiano', nl: 'Nederlands',
  cs: 'Čeština', sk: 'Slovenčina', pt: 'Português', el: 'Ελληνικά', hu: 'Magyar', ro: 'Română',
};
