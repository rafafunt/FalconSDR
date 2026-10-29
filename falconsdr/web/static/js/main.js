import { api, errorToast, h, icon, loadLanguage, modal, mount, t, toast } from './core.js';
import { liveAudio } from './live-audio.js';
import { S, emit, loadSettings, maybeShowLaunchNotice, on, startPolling } from './state.js';
import { channelsView } from './views/channels.js';
import { listenView } from './views/listen.js';
import { recordingsView } from './views/recordings.js';
import { scheduleView } from './views/schedule.js';
import { settingsView } from './views/settings.js';
import { statsView } from './views/stats.js';

const app = document.getElementById('app');

const ROUTES = {
  listen: { label: () => t('Listen'), icon: 'listen', view: listenView },
  recordings: { label: () => t('Recordings'), icon: 'library', view: recordingsView },
  channels: { label: () => t('Channels'), icon: 'channels', view: channelsView },
  schedule: { label: () => t('Schedule'), icon: 'clock', view: scheduleView },
  stats: { label: () => t('Statistics'), icon: 'stats', view: statsView },
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
  maybeShowLaunchNotice();
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
            await api.post('/api/client/disconnect');
            toast(t('Switching to local mode…'));
            setTimeout(() => location.reload(), 2500);
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
  for (const [key, r] of Object.entries(ROUTES)) {
    nav.append(h('a', { href: `#/${key}`, 'data-route': key }, icon(r.icon), h('span', r.label())));
  }
  const updateStatus = () => {
    const live = S.live;
    const running = live && live.running;
    const signal = live && (live.state === 'SIGNAL_DETECTED' || live.state === 'LINGER');
    mount(status,
      live && live.recording ? h('span.chip.bad.live', { title: t('Recording') }, h('span.dot'), 'REC') : null,
      h('span.chip', { class: running ? (signal ? 'good live' : 'accent') : '' }, h('span.dot'),
        running ? (live.stopping ? t('Stopping…') : signal ? t('Signal') : t('Scanning')) : t('Stopped')),
      h('button.icon', {
        class: liveAudio.playing ? 'on' : '', title: liveAudio.playing ? t('Stop listening') : t('Listen live'),
        onclick: () => liveAudio.toggle().catch(errorToast),
      }, icon(liveAudio.playing ? 'headphones' : 'mute')));
  };
  on('live', updateStatus);
  liveAudio.onchange = () => { updateStatus(); emit('audio'); };
  updateStatus();

  const route = () => {
    const key = (location.hash.match(/^#\/(\w+)/) || [])[1] || 'listen';
    const r = ROUTES[key] || ROUTES.listen;
    nav.querySelectorAll('a').forEach((a) => {
      a.classList.toggle('active', a.dataset.route === key);
      if (a.dataset.route === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    if (current && current.destroy) current.destroy();
    content.scrollTop = 0;
    current = r.view(content, location.hash);
  };
  window.addEventListener('hashchange', route);
  route();
}

boot().catch((e) => { console.error(e); errorToast(e); });
