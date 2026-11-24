import { api, errorToast, h, icon, loadLanguage, modal, mount, t, toast } from './core.js';
import { liveAudio } from './live-audio.js';
import { S, addonsInUse, emit, loadSettings, maybeAskUpdates, maybeShowLaunchNotice, on, startPolling } from './state.js';
import { channelsView } from './views/channels.js';
import { listenView } from './views/listen.js';
import { messagesView } from './views/messages.js';
import { recordingsView } from './views/recordings.js';
import { scheduleView } from './views/schedule.js';
import { settingsView } from './views/settings.js';
import { statsView } from './views/stats.js';

const app = document.getElementById('app');

const ROUTES = {
  listen: { label: () => t('Listen'), icon: 'listen', view: listenView },
  recordings: { label: () => t('Recordings'), icon: 'library', view: recordingsView },
  // What the add-on decoders found. Shown once add-ons are in use (Add-ons is the always-visible showcase); with
  // nothing received yet it explains itself and points to Add-ons.
  messages: { label: () => t('Messages'), icon: 'messages', view: messagesView, show: addonsInUse },
  channels: { label: () => t('Channels'), icon: 'channels', view: channelsView },
  schedule: { label: () => t('Schedule'), icon: 'clock', view: scheduleView },
  stats: { label: () => t('Statistics'), icon: 'stats', view: statsView },
  // Decoders and tools, plus transcription and digital voice (the same page component as Settings).
  addons: { label: () => t('Add-ons'), icon: 'addons', view: (root, hash) => settingsView(root, hash, 'addons') },
  settings: { label: () => t('Settings'), icon: 'settings', view: settingsView },
};

async function boot() {
  try {
    await loadLanguage();
  } catch {
    mount(app, h('div.center-page', h('div.card.login', h('h2', 'FalconSDR'), h('p.muted', 'Server unreachable - retrying…'))));
    setTimeout(boot, 3000);
    return;
  }
  // #token=... (opened by main.py on a helper computer) logs in without typing.
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    history.replaceState(null, '', location.pathname);
    try { await api.post('/api/login', { token: decodeURIComponent(m[1]) }); } catch { /* fall through to the form */ }
  }
  const session = await api.get('/api/session');
  if (!session.authenticated) return loginPage();
  if (session.mode === 'client') return clientPage();
  await loadSettings();
  shell();
  startPolling();
  await maybeShowLaunchNotice();   // the legal notice first, then (once per installation) the update question
  maybeAskUpdates();
}

// A new version: a small card, not a dialog - once per version, again a week after "Later", never for a skipped
// version. Remembered per browser (on a server every user decides for themselves); the top-bar chip and
// Settings -> Updates always show it anyway.
const NOTICE_KEY = 'update-notice';
const WEEK_MS = 7 * 24 * 3600 * 1000;
let noticeShownFor = '';
function readNotice() { try { return JSON.parse(localStorage.getItem(NOTICE_KEY)) || {}; } catch { return {}; } }
function writeNotice(value) { try { localStorage.setItem(NOTICE_KEY, JSON.stringify(value)); } catch { /* no storage: shown again next visit */ } }
function maybeUpdateCard() {
  const version = S.live && S.live.update;
  if (!version || noticeShownFor === version || document.querySelector('.modal-back')) return;   // never over a dialog
  if (location.hash.startsWith('#/settings/updates')) return;
  const seen = readNotice();
  if (seen.skip === version || (seen.version === version && Date.now() < (seen.until || 0))) return;
  noticeShownFor = version;
  const close = (remember) => { card.remove(); writeNotice(remember); };
  const later = () => close({ version, until: Date.now() + WEEK_MS });
  const card = h('div.update-card', { role: 'status' },
    h('b', t('FalconSDR {version} is available', { version })),
    h('span.small.muted', t('You have {current}. See what is new and how to update.', { current: S.settings.version })),
    h('div.row.wrap', { style: { gap: '8px' } },
      h('button.primary', { onclick: () => { later(); location.hash = '#/settings/updates'; } }, t("See what's new")),
      h('button.ghost', { onclick: later }, t('Later')),
      h('button.ghost', { onclick: () => close({ skip: version }) }, t('Skip this version'))));
  document.body.append(card);
}

function loginPage() {
  const input = h('input', { type: 'password', placeholder: t('Password'), autocomplete: 'current-password' });
  const submit = async (e) => {
    e.preventDefault();
    try {
      await api.post('/api/login', { token: input.value });
      location.reload();
    } catch { toast(t('Wrong password'), 'bad'); input.select(); }
  };
  mount(app, h('div.center-page', h('form.card.login', { onsubmit: submit },
    h('img', { src: 'icon.svg', alt: '' }),
    h('h2', 'FalconSDR'),
    h('p.muted', t('Enter the password of this receiver.')),
    input,
    h('button.primary.big', { type: 'submit' }, t('Log in')),
    h('p.faint.small', t('The password was set in Settings → Access. To remove it, delete "access_token" from config.json on the receiver.')))));
  input.focus();
}

// This computer only helps another server: show what it's doing and how to get there.
async function clientPage() {
  const body = h('div.col');
  const card = h('div.card.login', { style: { width: 'min(560px, 100%)', textAlign: 'left' } },
    h('div.row', h('img', { src: 'icon.svg', alt: '', style: { width: '44px', height: '44px', margin: 0 } }),
      h('div', h('h2', 'FalconSDR'), h('span.muted', t('This computer works for a FalconSDR server')))),
    body);
  mount(app, h('div.center-page', card));
  const refresh = async () => {
    let st;
    try { st = await api.get('/api/client/status'); } catch { return; }
    const info = st.info || {};
    mount(body,
      h('div.row.wrap',
        h('span.chip', { class: st.connected ? 'good live' : 'bad' }, h('span.dot'), st.connected ? t('Connected') : t('Not connected')),
        h('span.chip', st.name),
        info.gpu ? h('span.chip.accent', icon('cpu'), info.gpu) : null),
      h('div.mono.small.muted', st.url),
      h('p.muted', t('Transcription and copying are set up per computer in the server\'s Settings → Transcription & computers.')),
      info.log && info.log.length ? h('pre.small.muted', { style: { maxHeight: '160px', overflow: 'auto', margin: 0 } }, info.log.slice(-8).join('\n')) : null,
      h('div.row.wrap',
        h('button.primary.big', { onclick: () => { location.href = `${st.url}/#token=${encodeURIComponent(st.token)}`; } }, icon('external'), t('Open the server panel')),
        h('button.ghost', {
          onclick: async () => {
            if (!(await modal(t('Disconnect from the server?'), t('This computer goes back to scanning on its own. Recordings stay on the server.'), [[t('Cancel'), false, 'ghost'], [t('Disconnect'), true, 'danger solid']]))) return;
            try {
              await api.post('/api/client/disconnect');
              toast(t('Switching to local mode…'));
              setTimeout(() => location.reload(), 2500);
            } catch (e) { errorToast(e); }
          },
        }, t('Disconnect'))));
  };
  refresh();
  setInterval(refresh, 3000);
}

let current = null;

function shell() {
  const nav = h('nav.nav');
  const status = h('div.status');
  const content = h('main');
  mount(app, h('div.shell',
    h('header.topbar',
      h('a.brand', { href: '#/listen' }, h('img', { src: 'icon.svg', alt: '' }), 'FalconSDR', h('small', S.settings.mode === 'server' ? t('server') : '')),
      nav, status),
    content));
  const markActive = () => {
    const key = (location.hash.match(/^#\/(\w+)/) || [])[1] || 'listen';
    nav.querySelectorAll('a').forEach((a) => {
      a.classList.toggle('active', a.dataset.route === key);
      if (a.dataset.route === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  };
  const renderNav = () => {
    nav.replaceChildren(...Object.entries(ROUTES).filter(([, r]) => !r.show || r.show())
      .map(([key, r]) => h('a', { href: `#/${key}`, 'data-route': key }, icon(r.icon), h('span', r.label()))));
    markActive();
  };
  let hadAddons = addonsInUse();
  renderNav();
  const syncNav = () => {
    const now = addonsInUse();
    if (now !== hadAddons) { hadAddons = now; renderNav(); }
  };
  on('settings', () => { hadAddons = addonsInUse(); renderNav(); });
  on('config', syncNav);
  on('live', syncNav);
  const updateStatus = () => {
    const live = S.live;
    const running = live && live.running;
    const signal = live && (live.state === 'SIGNAL_DETECTED' || live.state === 'LINGER');
    mount(status,
      live && live.update ? h('a.chip.accent', { href: '#/settings/updates', title: t('FalconSDR {version} is available', { version: live.update }) },
        icon('download'), live.update) : null,
      live && live.recording ? h('span.chip.bad.live', { title: t('Recording') }, h('span.dot'), 'REC') : null,
      live && live.addon
        ? h('span.chip.good.live', h('span.dot'), t('Add-on running'))
        : h('span.chip', { class: running ? (signal ? 'good live' : 'accent') : '' }, h('span.dot'),
          running ? (live.stopping ? t('Stopping…') : signal ? t('Signal') : t('Scanning')) : t('Stopped')),
      h('button.icon', {
        class: liveAudio.playing ? 'on' : '', title: liveAudio.playing ? t('Stop listening') : t('Listen live'),
        onclick: () => liveAudio.toggle().catch(errorToast),
      }, icon(liveAudio.playing ? 'headphones' : 'mute')));
  };
  on('live', updateStatus);
  on('live', maybeUpdateCard);
  // Notifications: a notice in the app whenever a rule fires (the first poll only sets the starting point).
  let notifySeq = null;
  on('live', async (live) => {
    if (live.notify_seq === undefined) return;
    if (notifySeq === null) { notifySeq = live.notify_seq; return; }
    if (live.notify_seq <= notifySeq) return;
    const since = notifySeq;
    notifySeq = live.notify_seq;
    try { (await api.get(`/api/notify/recent?since=${since}`)).items.slice(-3).forEach((i) => toast(`${i.title}: ${i.message}`)); } catch { /* the list shows it */ }
  });
  liveAudio.onchange = () => { updateStatus(); emit('audio'); };
  updateStatus();

  const route = () => {
    const key = (location.hash.match(/^#\/(\w+)/) || [])[1] || 'listen';
    const r = ROUTES[key] || ROUTES.listen;
    markActive();
    if (current && current.destroy) current.destroy();
    content.scrollTop = 0;
    current = r.view(content, location.hash);
  };
  window.addEventListener('hashchange', route);
  route();
}

boot().catch((e) => { console.error(e); errorToast(e); });
