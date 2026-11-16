// "Messages": what the add-on decoders found - ACARS and APRS from the scanner, rtl_433 sensors and VDL2
// from their programs - newest first, filterable, with the full decoded fields one click away.
import { api, fmtTime, h, mount, t } from '../core.js';
import { S, cfg, on } from '../state.js';

const NAMES = () => ({ acars: 'ACARS', aprs: 'APRS', rtl_433: t('Sensors'), vdl2: 'VDL2' });
const MAX_ROWS = 600;

export function messagesView(root) {
  let rows = [];
  let lastId = 0;
  let filter = '';
  let query = '';
  let paused = false;
  let timer = null;
  const open = new Set();

  const list = h('div.msg-list');
  const chips = h('div.row.wrap');
  const search = h('input', { type: 'search', placeholder: t('Search…'), oninput: (e) => { query = e.target.value.toLowerCase(); draw(); } });
  const pauseBtn = h('button.ghost', { onclick: () => { paused = !paused; pauseBtn.textContent = paused ? t('Resume') : t('Pause'); } }, t('Pause'));
  const clearBtn = h('button.ghost', { onclick: async () => { await api.post('/api/addons/clear', {}); rows = []; draw(); } }, t('Clear'));
  const banner = h('div');
  mount(root, h('div.page', h('div.page-head', h('h1', t('Messages'))),
    h('section.card', banner, h('div.row.wrap', chips, h('span.spacer'), search, pauseBtn, clearBtn), list)));

  const link = (e) => (e.lat !== undefined && e.lon !== undefined
    ? h('a', { href: `https://www.openstreetmap.org/?mlat=${e.lat}&mlon=${e.lon}#map=12/${e.lat}/${e.lon}`, target: '_blank', rel: 'noopener', onclick: (ev) => ev.stopPropagation() }, t('Map')) : null);

  function drawChips() {
    const names = NAMES();
    const have = new Set(rows.map((r) => r.addon));
    const ids = Object.keys(names).filter((id) => have.has(id) || cfg().addons.enabled.includes(id));
    mount(chips, ...['', ...ids].map((id) => h('button', { class: id === filter ? 'primary' : 'ghost', onclick: () => { filter = id; draw(); } }, id ? names[id] : t('All'))));
  }

  function draw() {
    drawChips();
    const names = NAMES();
    const shown = rows.filter((r) => (!filter || r.addon === filter) && (!query || JSON.stringify(r).toLowerCase().includes(query))).slice(-MAX_ROWS).reverse();
    mount(list, ...(shown.length ? shown.map((e) => {
      const detail = open.has(e.id) ? h('pre.msg-detail', JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !['id', 'summary'].includes(k))), null, 2)) : null;
      const toggleOpen = () => { if (open.has(e.id)) open.delete(e.id); else open.add(e.id); draw(); };
      return h('div.msg', { class: open.has(e.id) ? 'open' : '', tabindex: 0, role: 'button', onclick: toggleOpen, onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggleOpen(); } } },
        h('div.row', { style: { gap: '10px', alignItems: 'baseline' } },
          h('span.mono.small.muted', fmtTime(new Date(e.ts * 1000).toISOString())),
          h('span.chip', names[e.addon] || e.addon),
          e.freq_hz ? h('span.mono.small.muted', `${(e.freq_hz / 1e6).toFixed(4)}`) : null,
          h('span.grow', e.summary), link(e)),
        detail);
    }) : [h('div.empty', t('Nothing decoded yet. Turn an add-on on in Add-ons and scan a band that contains its channels.'))]));
  }

  async function poll() {
    if (paused) return;
    try {
      const r = await api.get(`/api/addons/events?since=${lastId}`);
      if (r.events.length) {
        rows = rows.concat(r.events).slice(-MAX_ROWS * 2);
        lastId = r.events[r.events.length - 1].id;
        draw();
      }
    } catch { /* the next poll tries again */ }
  }

  const drawBanner = () => mount(banner, S.live && S.live.addon
    ? h('p', { style: { margin: '0 0 8px' } }, h('span.chip.good.live', h('span.dot'), t('{name} is using the receiver', { name: NAMES()[S.live.addon] || S.live.addon })), ' ',
      h('a', { href: '#/addons' }, t('Stop it in Add-ons'))) : null);
  const off = on('live', drawBanner);
  draw();
  drawBanner();
  poll();
  timer = setInterval(poll, 2000);
  return { destroy() { clearInterval(timer); if (off) off(); } };
}
