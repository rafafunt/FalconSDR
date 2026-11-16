// Statistics of the add-ons: an overview that compares how busy each one was (the only comparison that means
// something - an aircraft and a thermometer are not alike), and one page per add-on with figures that fit its data.
import { api, errorToast, fmtAgo, h, icon, mount, select, t } from '../core.js';
import { cfg } from '../state.js';
import { NAMES } from './messages.js';

const ORDER = ['vdl2', 'acars', 'aprs', 'rtl_433'];
const COLORS = { acars: 'var(--accent)', aprs: 'var(--green-fill)', vdl2: 'var(--orange-fill)', rtl_433: 'var(--purple)' };
const iso = (ts) => new Date(ts * 1000).toISOString();
const num = (n) => Number(n).toLocaleString();
const pad = (n) => String(n).padStart(2, '0');

export function addonStats({ body, sub, utc }) {
  let days = +(localStorage.getItem('statsDays') || 7);
  let snap = null;
  let reloadTimer = null;

  // -- time helpers (UTC or local, as the page's switch says) ----------------------------------------------
  const hourOf = (ts) => (utc ? new Date(ts * 1000).getUTCHours() : new Date(ts * 1000).getHours());
  const dayKey = (ts) => { const d = new Date(ts * 1000); return utc ? d.toISOString().slice(0, 10) : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const hourLabel = (ts) => `${pad(hourOf(ts))}:00`;

  function bars(values, labels, title, color = 'var(--accent)') {
    const max = Math.max(1, ...values);
    const cols = `repeat(${values.length}, 1fr)`;
    return h('section.card',
      h('div.card-head', h('h2', title)),
      h('div.bars', { style: { gridTemplateColumns: cols } },
        values.map((v, i) => h('div.bar', { style: { height: `${(v / max) * 100}%`, background: color }, title: `${labels[i]}: ${num(v)}` }))),
      h('div.bars-axis', { style: { gridTemplateColumns: cols } }, labels.map((l, i) => h('span', values.length > 24 && i % Math.ceil(values.length / 12) ? '' : l))));
  }

  // One column per slot, split by add-on (the overview's comparison of activity).
  function stacked(slots, ids, title) {
    const max = Math.max(1, ...slots.map((s) => ids.reduce((a, id) => a + (s.values[id] || 0), 0)));
    const cols = `repeat(${slots.length}, 1fr)`;
    return h('section.card',
      h('div.card-head', h('h2', title), h('div.row.wrap', ids.map((id) => h('span.small.muted', h('span.swatch', { style: { background: COLORS[id] } }), NAMES()[id])))),
      h('div.bars', { style: { gridTemplateColumns: cols } }, slots.map((s) => {
        const total = ids.reduce((a, id) => a + (s.values[id] || 0), 0);
        return h('div.bar-stack', { style: { height: `${(total / max) * 100}%` }, title: `${s.label}: ${ids.map((id) => `${NAMES()[id]} ${num(s.values[id] || 0)}`).join(', ')}` },
          ids.filter((id) => s.values[id]).map((id) => h('div', { style: { flex: s.values[id], background: COLORS[id] } })));
      })),
      h('div.bars-axis', { style: { gridTemplateColumns: cols } }, slots.map((s, i) => h('span', slots.length > 24 && i % Math.ceil(slots.length / 12) ? '' : s.label))));
  }

  function table(head, rows, onRow) {
    return h('div.table-wrap', h('table', h('thead', h('tr', head.map(([label, cls]) => h(`th${cls ? '.' + cls : ''}`, label)))),
      h('tbody', rows.map((r) => h('tr', onRow ? { style: { cursor: 'pointer' }, title: t('Click to see its messages'), onclick: () => onRow(r) } : {}, r.cells)))));
  }
  const toMessages = (query) => () => { sessionStorage.setItem('msgQuery', query); location.hash = '#/messages'; };

  // -- slots of the timeline: hours for a day or two, days beyond ------------------------------------------
  function slotsFor(addons) {
    const hourly = days && days <= 2;
    const map = new Map();
    for (const [id, a] of Object.entries(addons)) {
      for (const [hour, n] of a.timeline) {
        const key = hourly ? hour : dayKey(hour);
        const s = map.get(key) || { key, label: hourly ? hourLabel(hour) : String(key).slice(5), values: {} };
        s.values[id] = (s.values[id] || 0) + n;
        map.set(key, s);
      }
    }
    return [...map.values()].sort((a, b) => (a.key < b.key ? -1 : 1));
  }

  const hoursOfDay = (a) => {
    const v = Array(24).fill(0);
    for (const [hour, n] of a.timeline) v[hourOf(hour)] += n;
    return v;
  };

  // -- overview ----------------------------------------------------------------------------------------------
  function overview(shown) {
    const tiles = shown.map((id) => {
      const a = snap.addons[id] || { n: 0, link: 0, last: 0, distinct: 0 };
      return h('div.kpi.clickable', { tabindex: 0, role: 'button', onclick: () => { location.hash = `#/stats/addons/${id}`; },
        onkeydown: (e) => { if (e.key === 'Enter') location.hash = `#/stats/addons/${id}`; } },
      h('span', h('span.swatch', { style: { background: COLORS[id] } }), NAMES()[id]),
      h('b', num(a.n)),
      h('small', a.last ? t('last {when}', { when: fmtAgo(iso(a.last)) }) : t('nothing yet')),
      id === 'vdl2' && a.link ? h('small', { style: { display: 'block' } }, t('{n} acknowledgements and polls not counted', { n: num(a.link) })) : null);
    });
    const slots = slotsFor(Object.fromEntries(shown.map((id) => [id, snap.addons[id]]).filter(([, a]) => a)));
    return [h('div.kpis', ...tiles),
      slots.length ? stacked(slots, shown.filter((id) => snap.addons[id]), days && days <= 2 ? t('Messages per hour') : t('Messages per day')) : null];
  }

  // -- one add-on ----------------------------------------------------------------------------------------------
  function detail(id) {
    const a = snap.addons[id];
    if (!a) return [h('div.card', h('div.empty', t('Nothing decoded by {name} in this period.', { name: NAMES()[id] })))];
    const hod = hoursOfDay(a);
    const busiest = hod.indexOf(Math.max(...hod));
    const slots = slotsFor({ [id]: a });
    const out = [];
    const kpis = [h('div.kpi', h('span', t('Messages')), h('b', num(a.n)))];
    if (id === 'vdl2' || id === 'acars') {
      kpis.push(h('div.kpi', h('span', t('Aircraft')), h('b', num(a.distinct))),
        h('div.kpi', h('span', t('Frequencies')), h('b', num(a.freq.length))),
        h('div.kpi', h('span', t('Busiest hour')), h('b', `${pad(busiest)}:00`)));
      if (id === 'vdl2') kpis.push(h('div.kpi', h('span', t('Acknowledgements and polls')), h('b', num(a.link))));
    } else if (id === 'aprs') {
      kpis.push(h('div.kpi', h('span', t('Stations')), h('b', num(a.distinct))), h('div.kpi', h('span', t('Positions')), h('b', num(a.pos))),
        h('div.kpi', h('span', t('Farthest')), h('b', a.farthest ? `${a.farthest.km} km` : '–'), a.farthest ? h('small', a.farthest.who) : h('small', t('set your location in Settings'))));
    } else {
      const withT = a.sensors.filter((s) => s.unit === '°C' && s.vmax !== null);
      const hot = withT.length ? withT.reduce((x, y) => (y.vmax > x.vmax ? y : x)) : null;
      const cold = withT.length ? withT.reduce((x, y) => (y.vmin < x.vmin ? y : x)) : null;
      const kinds = new Set(a.sensors.map((s) => s.category));
      kpis.push(h('div.kpi', h('span', t('Devices')), h('b', num(a.sensors.length))),
        h('div.kpi', h('span', t('Kinds of device')), h('b', num(kinds.size))));
      if (hot) kpis.push(h('div.kpi', h('span', t('Warmest')), h('b', `${hot.vmax} °C`), h('small', hot.device)),
        h('div.kpi', h('span', t('Coldest')), h('b', `${cold.vmin} °C`), h('small', cold.device)));
    }
    out.push(h('div.kpis', ...kpis));
    if (slots.length > 1) out.push(bars(slots.map((s) => s.values[id] || 0), slots.map((s) => s.label), days && days <= 2 ? t('Per hour') : t('Per day'), COLORS[id]));
    out.push(bars(hod, hod.map((_, i) => String(i)), t('By hour of day'), COLORS[id]));

    if (id === 'vdl2' || id === 'acars') {
      out.push(h('div.grid.two',
        h('section.card', h('div.card-head', h('h2', t('Aircraft')), h('span.faint.small', t('Click a row to see its messages'))),
          table([[t('Aircraft')], [t('Messages'), 'num'], [t('Last heard')]], a.who.slice(0, 30).map(([name, n, last]) => ({ name, cells: [h('td.mono', name), h('td.num', num(n)), h('td.small.muted', fmtAgo(iso(last)))] })),
            (r) => toMessages(r.name)())),
        h('section.card', h('div.card-head', h('h2', t('Frequencies'))),
          table([[t('MHz')], [t('Messages'), 'num'], [t('Share')], [t('Level')]], a.freq.map(([f, n, lvl]) => ({ cells: [h('td.mono', (f / 1e6).toFixed(4)), h('td.num', num(n)),
            h('td', h('div.chan-bar', h('div', { style: { width: `${Math.max(3, 100 * n / a.freq[0][1])}%` } }))), h('td.small.muted', lvl === null ? '–' : `${lvl} dB`)] }))))));
      if (a.labels.length) {
        out.push(h('section.card', h('div.card-head', h('h2', t('Message labels')), h('span.faint.small', t('The two-character ACARS label says what kind of message it is'))),
          h('div.row.wrap', a.labels.map(([label, n]) => h('span.chip', `${label} · ${num(n)}`)))));
      }
    } else if (id === 'aprs') {
      out.push(h('div.grid.two',
        h('section.card', h('div.card-head', h('h2', t('Stations')), h('span.faint.small', t('Click a row to see its messages'))),
          table([[t('Station')], [t('Packets'), 'num'], [t('Last heard')]], a.who.slice(0, 30).map(([name, n, last]) => ({ name, cells: [h('td.mono', name), h('td.num', num(n)), h('td.small.muted', fmtAgo(iso(last)))] })),
            (r) => toMessages(r.name)())),
        h('section.card', h('div.card-head', h('h2', t('Packet types'))),
          table([[t('Type')], [t('Packets'), 'num']], a.types.map(([ty, n]) => ({ cells: [h('td', ty), h('td.num', num(n))] }))))));
    } else {
      const cat = { thermometer: t('Thermometer'), weather: t('Weather station'), tpms: t('Tyre pressure'), energy: t('Power and energy'), meter: t('Utility meter'),
        security: t('Door, motion or alarm'), remote: t('Remote control'), tank: t('Tank level'), soil: t('Soil'), air: t('Air quality'), lightning: t('Lightning'), other: t('Device') };
      out.push(h('section.card', h('div.card-head', h('h2', t('Devices')), h('span.faint.small', t('Click a row to see its readings'))),
        table([[t('Device')], [t('Kind')], [t('Readings'), 'num'], [t('Main reading: min / average / max')], [t('Battery')], [t('Last heard')]],
          a.sensors.map((s) => ({ name: s.device, cells: [h('td.mono', s.device), h('td.small.muted', cat[s.category] || ''), h('td.num', num(s.n)),
            h('td.mono', s.vmin === null ? '–' : `${s.label ? t(s.label) + ': ' : ''}${s.vmin} / ${s.vavg} / ${s.vmax}${s.unit ? ' ' + s.unit : ''}`),
            h('td', s.batt === null || s.batt === undefined ? '–' : h('span.chip', { class: s.batt ? 'good' : 'warn' }, s.batt ? t('battery OK') : t('battery low'))),
            h('td.small.muted', fmtAgo(iso(s.last)))] })), (r) => toMessages(r.name)())));
    }
    return out;
  }

  // -- page --------------------------------------------------------------------------------------------------
  function draw() {
    if (!snap) return;
    const ids = ORDER.filter((id) => snap.addons[id] || cfg().addons.enabled.includes(id));
    const current = sub && ids.includes(sub) ? sub : '';
    const chips = h('div.row.wrap', { style: { gap: '8px' } },
      h('button', { class: !current ? 'primary' : 'ghost', onclick: () => { location.hash = '#/stats/addons'; } }, t('Overview')),
      ...ids.map((id) => h('button', { class: id === current ? 'primary' : 'ghost', onclick: () => { location.hash = `#/stats/addons/${id}`; } },
        h('span.swatch', { style: { background: COLORS[id] } }), NAMES()[id])),
      h('span.spacer'),
      select([['1', t('Last 24 hours')], ['7', t('Last 7 days')], ['30', t('Last 30 days')], ['0', t('Everything stored')]], String(days),
        (v) => { days = +v; localStorage.setItem('statsDays', String(days)); load(); }, { style: { width: 'auto' }, 'aria-label': t('Time range') }));
    const any = Object.values(snap.addons).some((a) => a.n || a.link);
    mount(body, chips,
      ...(!any && !current ? [h('div.card', h('div.empty', t('Nothing decoded in this period.'), ' ', h('a', { href: '#/addons' }, t('Add-ons'))))]
        : current ? detail(current) : overview(ids)),
      h('p.faint.small', { style: { margin: 0 } }, t('Counts of what was decoded, kept for up to {n} days (the messages themselves for a few days).', { n: 90 }),
        snap.days_available ? ' ' + t('Stored so far: {n} days.', { n: snap.days_available }) : ''));
  }

  async function load() {
    try { snap = await api.get(`/api/addons/stats?days=${days}`); } catch (e) { errorToast(e); return; }
    draw();
  }

  load();
  reloadTimer = setInterval(load, 15000);
  return { reload: draw, destroy: () => clearInterval(reloadTimer) };
}
