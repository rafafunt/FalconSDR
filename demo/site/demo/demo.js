// Online demo around the unchanged app: starts the virtual receiver (/sw.js),
// then boots the app with a "this is a demo" banner, a welcome dialog and
// hints - Skip / Skip + blacklist light up when the scanner sits on a birdie.
import { h, icon, lang, modal, mount, t } from '../js/core.js';
import { liveAudio } from '../js/live-audio.js';
import { S, cfg, confirmRecording, emit, on, patchConfig } from '../js/state.js';

const DOWNLOAD = 'https://github.com/rafafunt/FalconSDR/releases/latest';
const SOURCE = 'https://github.com/rafafunt/FalconSDR';

const TEXT = {
  en: {
    long: 'Online demo on a simulated receiver - every signal and sound here is synthetic. To listen to real radio, download FalconSDR and connect an RTL-SDR receiver.',
    short: 'Simulated receiver',
    download: 'Download FalconSDR',
    downloadShort: 'Get the app',
    about: 'About the demo',
    title: 'FalconSDR demo',
    intro: 'The real FalconSDR interface on a virtual receiver in your browser. Signals, voices and music here are synthetic.',
    tips: [
      'Press Start - the scanner stops only on transmissions.',
      'Stuck on a steady whistle? That is a birdie - the demo shows how to get rid of it.',
      'Try other bands (FM radio, marine, military…), a lower Trigger level, or the 10 MHz receiver in “SDR device”.',
      'Turn on “Auto-record transmissions”, then look at Recordings and Statistics.',
    ],
    real: 'For real radio: download FalconSDR (Windows, macOS, Linux) and plug in an RTL-SDR receiver. Changes made here stay in this browser.',
    start: 'Start the demo',
    reset: 'Reset the demo',
    resetAsk: 'Bring back the starting settings and sample recordings? Everything you changed in the demo is lost.',
    cancel: 'Cancel',
    unsupported: 'This browser can\'t run the demo. Open the link in Chrome, Safari, Edge or Firefox - or download FalconSDR.',
    recOff: 'Auto-recording is off, so new transmissions aren\'t saved - the list holds sample recordings. With it on, every transmission the scanner stops on lands here, transcribed.',
    recOn: 'Turn on auto-recording',
    credits: 'Voices: Piper TTS with the VCTK (University of Edinburgh) and LibriTTS-R (Google) voices, both CC BY 4.0, and Polish voices under CC0. Music generated for the demo.',
    license: 'FalconSDR is free software (GNU GPL v3 or later), and so is this demo.',
    source: 'Source code',
    licenseText: 'Licence text',
    privacy: 'Privacy',
    birdie: '{freq} MHz is most likely a birdie: interference made by the receiver itself, a steady carrier with nothing on it. The scanner would sit on it forever. “{skip}” only moves on - it comes back on the next pass. “{bl}” gets rid of it for good (undo in Channels → Blacklist).',
  },
  pl: {
    long: 'Demo online na symulowanym odbiorniku - wszystkie sygnały i dźwięki są tu sztuczne. Żeby słuchać prawdziwego radia, pobierz FalconSDR i podłącz odbiornik RTL-SDR.',
    short: 'Symulowany odbiornik',
    download: 'Pobierz FalconSDR',
    downloadShort: 'Pobierz',
    about: 'O demo',
    title: 'Demo FalconSDR',
    intro: 'Prawdziwy interfejs FalconSDR na wirtualnym odbiorniku w Twojej przeglądarce. Sygnały, głosy i muzyka są tu sztuczne.',
    tips: [
      'Naciśnij Start - skaner zatrzymuje się tylko na transmisjach.',
      'Utknął na stałym gwizdzie? To birdie - demo pokaże, jak się go pozbyć.',
      'Wypróbuj inne pasma (radio FM, morskie, wojskowe…), niższy próg wyzwalania albo odbiornik 10 MHz w „Urządzenie SDR”.',
      'Włącz „Automatycznie nagrywaj transmisje”, a potem zajrzyj do Nagrań i Statystyk.',
    ],
    real: 'Do prawdziwego radia: pobierz FalconSDR (Windows, macOS, Linux) i podłącz odbiornik RTL-SDR. Zmiany zrobione tutaj zostają w tej przeglądarce.',
    start: 'Uruchom demo',
    reset: 'Zresetuj demo',
    resetAsk: 'Przywrócić początkowe ustawienia i przykładowe nagrania? Wszystko, co zmieniłeś w demo, zniknie.',
    cancel: 'Anuluj',
    unsupported: 'Ta przeglądarka nie uruchomi demo. Otwórz link w Chrome, Safari, Edge lub Firefoksie - albo pobierz FalconSDR.',
    recOff: 'Automatyczne nagrywanie jest wyłączone, więc nowe transmisje się nie zapisują - na liście są przykładowe nagrania. Po włączeniu trafia tu każda transmisja, na której zatrzyma się skaner, razem z transkrypcją.',
    recOn: 'Włącz automatyczne nagrywanie',
    credits: 'Głosy: Piper TTS z głosami VCTK (University of Edinburgh) i LibriTTS-R (Google), oba na licencji CC BY 4.0, oraz polskimi głosami na CC0. Muzyka wygenerowana na potrzeby demo.',
    license: 'FalconSDR to wolne oprogramowanie (GNU GPL v3 lub nowsza), tak samo jak to demo.',
    source: 'Kod źródłowy',
    licenseText: 'Tekst licencji',
    privacy: 'Prywatność',
    birdie: '{freq} MHz to najpewniej birdie: zakłócenie wytwarzane przez sam odbiornik, stała nośna bez żadnej treści. Skaner stałby na nim w nieskończoność. „{skip}” tylko przechodzi dalej - przy następnym obiegu wróci. „{bl}” usuwa go na dobre (cofniesz w Kanały → Czarna lista).',
  },
};
const guess = (navigator.language || 'en').slice(0, 2);
const L = () => TEXT[S.settings ? lang : guess] || TEXT.en;

const banner = document.getElementById('demo-banner');
function renderBanner() {
  const x = L();
  mount(banner, h('div.demo-banner',
    h('span.pill', 'DEMO'),
    h('span.text', h('span.long', x.long), h('span.short', x.short)),
    h('a.btn.primary', { href: DOWNLOAD, target: '_blank', rel: 'noopener' }, h('span.long', x.download), h('span.short', x.downloadShort)),
    h('button.ghost', { onclick: () => welcome(true), 'aria-label': x.about, title: x.about }, h('span.long', x.about), h('span.short', '?'))));
}

async function welcome(fromBanner = false) {
  const x = L();
  const body = h('div.col.demo-welcome', { style: { gap: '12px' } },
    h('p', x.intro), h('ul', x.tips.map((tip) => h('li', tip))), h('p.muted', x.real), h('p.faint.small', x.credits),
    h('p.faint.small', x.license, ' ', ...[[x.source, SOURCE], [x.licenseText, '/LICENSE.txt'], [x.privacy, '/privacy.html']]
      .flatMap(([label, href], i) => [i ? ' · ' : '', h('a', { href, target: '_blank', rel: 'noopener' }, label)])));
  const buttons = [[x.download, 'download', 'ghost'], [x.start, 'start', 'primary']];
  if (fromBanner) buttons.unshift([x.reset, 'reset', 'ghost danger']);
  const shown = modal(x.title, body, buttons);
  // The dialog focuses its last button, which scrolls a long one to the bottom on phones.
  [...document.querySelectorAll('.modal')].pop().scrollTop = 0;
  const res = await shown;
  try { localStorage.setItem('demo-welcome', '1'); } catch { /* private mode */ }
  if (res === 'download') window.open(DOWNLOAD, '_blank', 'noopener');
  if (res === 'reset' && (await modal(x.reset, x.resetAsk, [[x.cancel, false, 'ghost'], [x.reset, true, 'danger solid']]))) {
    await fetch('/api/demo/reset', { method: 'POST', headers: { 'X-FalconSDR': '1' } });
    location.reload();
  }
}

// Skip / Skip + blacklist light up while the scanner sits on a birdie.
function birdieHint(live) {
  const skip = document.querySelector(`button[title="${t('Leave this frequency for now')}"]`);
  const bl = document.querySelector(`button[title="${t('Leave it and never stop on it again (adds it to the blacklist)')}"]`);
  const on = !!(live && live.running && live.demo_birdie && skip);
  [skip, bl].forEach((b) => b && b.classList.toggle('demo-pulse', on));
  let note = document.querySelector('.demo-hint');
  if (!on) { if (note) note.remove(); return; }
  const freq = document.querySelector('.hero .freq')?.firstChild?.textContent || '';
  const text = L().birdie.replace('{freq}', freq).replace('{skip}', t('Skip')).replace('{bl}', t('Skip + blacklist'));
  if (!note) {
    note = h('div.notice.demo-hint', { role: 'status' }, icon('ban'), h('span', { style: { flex: '1 1 220px' } }));
    skip.closest('.controls').after(note);
  }
  note.querySelector('span').textContent = text;
}

// Recordings: say why nothing new shows up while auto-recording is off (the app itself doesn't).
function recordingsHint() {
  const page = location.hash.startsWith('#/recordings') && document.querySelector('main .page');
  let note = document.querySelector('.demo-rec-hint');
  const show = page && S.settings && !cfg().recording.enabled;
  if (!show) { if (note) note.remove(); return; }
  if (note && page.contains(note)) return;
  note = h('div.notice.demo-rec-hint', icon('record'), h('span', { style: { flex: '1 1 260px' } }, L().recOff),
    h('button.primary.small', {
      onclick: async () => {
        if (!(await confirmRecording())) return;
        patchConfig({ recording: { enabled: true } });
        recordingsHint();
      },
    }, L().recOn));
  page.querySelector('.page-head').after(note);
}

// The next step lights up: Start while stopped, then Listen while the sound is off.
function controlsHint() {
  setTimeout(paintControlsHint, 0);  // after the Listen view, which resets the buttons' classes
}
function paintControlsHint() {
  const [startBtn, listenBtn] = document.querySelectorAll('.hero .controls button.big');
  const running = !!(S.live && S.live.running);
  if (startBtn) startBtn.classList.toggle('demo-pulse', !!S.live && !running);
  if (listenBtn) listenBtn.classList.toggle('demo-pulse', running && !liveAudio.playing);
}

// Picking a receiver sets its capture width, like choosing the hardware would.
let lastDriver = null;
function deviceRate() {
  const driver = cfg().device.driver, changed = lastDriver !== null && driver !== lastDriver;
  lastDriver = driver;  // before patchConfig: it emits 'config' again
  const d = changed && S.settings.devices.find((x) => x.driver === driver);
  if (d && d.rate) { patchConfig({ device: { sample_rate_hz: d.rate } }); emit('settings'); }
}

async function useServiceWorker() {
  if (!('serviceWorker' in navigator) || new URLSearchParams(location.search).has('inpage')) throw new Error('no service worker');
  const timeout = (ms) => new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms));
  await Promise.race([navigator.serviceWorker.register('/sw.js', { scope: '/' }), timeout(5000)]);
  const reg = await Promise.race([navigator.serviceWorker.ready, timeout(5000)]);
  if (!navigator.serviceWorker.controller) {
    // First visit (or a hard reload, which bypasses the worker): wait to be taken over.
    await Promise.race([new Promise((resolve) => {
      navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true });
      reg.active.postMessage('claim');
    }), timeout(5000)]);
  }
}

// No service workers (in-app browsers on iOS - Messenger, Facebook, Instagram -
// and private windows in Firefox): the same simulator runs in the page, and
// /api/* fetches, <audio> sources and download links are routed to it.
async function inPage() {
  await new Promise((resolve, reject) => document.head.append(h('script', { src: '/sw.js', onload: resolve, onerror: reject })));
  const pageFetch = window.fetch.bind(window);
  const isApi = (u) => new URL(u, location.href).origin === location.origin && new URL(u, location.href).pathname.startsWith('/api/');
  window.fetch = (input, init) => {
    const req = new Request(input, init);
    return isApi(req.url) ? window.handle(req, new URL(req.url)) : pageFetch(input, init);
  };
  const blobUrl = async (u) => URL.createObjectURL(await (await window.fetch(u)).blob());
  const src = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
  const play = HTMLMediaElement.prototype.play;
  Object.defineProperty(HTMLMediaElement.prototype, 'src', {
    configurable: true,
    get() { return src.get.call(this); },
    set(v) {
      if (typeof v !== 'string' || !v || !isApi(v)) { this._demo = null; src.set.call(this, v); return; }
      const job = (this._demo = { playing: false });
      blobUrl(v).then((u) => {
        if (this._demo !== job) return;
        src.set.call(this, u);
        job.ready = true;
        if (job.playing) play.call(this).catch(() => {});
      });
    },
  });
  HTMLMediaElement.prototype.play = function () {
    if (this._demo && !this._demo.ready) { this._demo.playing = true; return Promise.resolve(); }
    return play.call(this);
  };
  document.addEventListener('click', async (e) => {
    const a = e.target.closest && e.target.closest('a[href^="/api/"]');
    if (!a) return;
    e.preventDefault();
    const u = new URL(a.href);
    h('a', { href: await blobUrl(a.href), download: (u.searchParams.get('id') || 'recording.wav').split('/').pop() }).click();
  }, true);
}

async function start() {
  // The app sets the real language once it has loaded; the banner and this dialog come first.
  document.documentElement.lang = TEXT[guess] ? guess : 'en';
  renderBanner();
  try { await useServiceWorker(); } catch {
    try { await inPage(); } catch {
      mount(document.getElementById('app'), h('div.center-page', h('div.card.login', h('h2', 'FalconSDR'), h('p', L().unsupported),
        h('a.btn.primary', { href: DOWNLOAD }, L().download))));
      return;
    }
  }
  let seen = false;
  try { seen = !!localStorage.getItem('demo-welcome'); } catch { /* private mode */ }
  if (!seen) await welcome();
  on('settings', renderBanner);
  on('live', birdieHint);
  on('live', recordingsHint);
  on('live', controlsHint);
  on('audio', controlsHint);
  on('config', deviceRate);
  on('settings', () => { lastDriver = cfg().device.driver; });
  on('config', recordingsHint);
  window.addEventListener('hashchange', () => setTimeout(recordingsHint, 0));
  await import('../js/main.js');
}

start().catch((e) => { console.error(e); mount(document.getElementById('app'), h('div.center-page', h('div.card.login', h('p', L().unsupported)))); });
