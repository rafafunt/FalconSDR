// "Statistics": what was heard, when and how well.
import { api, errorToast, fmtAgo, fmtChannel, fmtDate, fmtDuration, h, mount, prefs, qs, qualityBadge, t, tabs, toggle } from '../core.js';
import { addonsInUse } from '../state.js';
import { addonStats } from './stats-addons.js';

// Two scopes: what was heard on the air (recordings) and what the add-ons decoded. #/stats is the first,
// #/stats/addons[/<id>] the second - offered only when add-ons are in use.
export function statsView(root, hash = '') {
  const m = hash.match(/^#\/stats\/addons(?:\/(\w+))?/);
  const hasAddons = addonsInUse();
  const scope = m && hasAddons ? 'addons' : 'voice';
  const body = h('div.col', { style: { gap: '16px' } }, h('div.empty', t('Loading…')));
  let utc = prefs.utc;
  let addons = null;
  mount(root, h('div.page',
    h('div.page-head', h('h1', t('Statistics')),
      toggle(t('UTC'), utc, (v) => { utc = prefs.utc = v; if (addons) { addons.destroy(); addons = addonStats({ body, sub: m && m[1], utc }); } else load(); })),
    hasAddons ? tabs([['voice', t('Voice: what was heard')], ['addons', t('Add-ons: what was decoded')]], scope, (v) => { location.hash = v === 'addons' ? '#/stats/addons' : '#/stats'; }) : null,
    body));
  if (scope === 'addons') {
    addons = addonStats({ body, sub: m[1], utc });
    return { destroy() { addons.destroy(); } };
  }

  function bars(values, labels, title) {
    const max = Math.max(1, ...values);
    const cols = `repeat(${values.length}, 1fr)`;
    return h('section.card',
      h('div.card-head', h('h2', title)),
      h('div.bars', { style: { gridTemplateColumns: cols } },
        values.map((v, i) => h('div.bar', { style: { height: `${(v / max) * 100}%` }, title: `${labels[i]}: ${v}` }))),
      h('div.bars-axis', { style: { gridTemplateColumns: cols } }, labels.map((l, i) => h('span', values.length > 24 && i % 5 ? '' : l))));
  }

  async function load() {
    let s;
    try { s = await api.get('/api/library/stats' + qs({ utc: utc ? 1 : '' })); } catch (e) { errorToast(e); return; }
    const sum = s.summary;
    if (!sum.count) { mount(body, h('div.card', h('div.empty', t('No recordings yet - statistics appear once something has been recorded.')))); return; }
    const days = s.per_day.slice(-45);
    const weekdays = [t('Mon'), t('Tue'), t('Wed'), t('Thu'), t('Fri'), t('Sat'), t('Sun')];
    mount(body,
      h('div.kpis',
        h('div.kpi', h('span', t('Recordings')), h('b', sum.count.toLocaleString())),
        h('div.kpi', h('span', t('Total time')), h('b', fmtDuration(sum.duration))),
        h('div.kpi', h('span', t('Frequencies')), h('b', sum.frequencies.toLocaleString())),
        h('div.kpi', h('span', t('Average quality')), h('b', Math.round(sum.quality))),
        h('div.kpi', h('span', t('Busiest')), h('b', sum.busiest ? fmtChannel(sum.busiest * 1000) : '–'), h('small', 'MHz')),
        h('div.kpi', h('span', t('Date range')), h('b', { style: { fontSize: '14px' } }, `${fmtDate(sum.from)} – ${fmtDate(sum.to)}`))),
      h('div.grid.two',
        bars(s.per_hour.map(([, c]) => c), s.per_hour.map(([hr]) => String(hr)), t('By hour of day')),
        bars(s.per_weekday, weekdays, t('By day of week'))),
      days.length > 1 ? bars(days.map(([, c]) => c), days.map(([d]) => d.slice(5)), t('Per day (last {n} days)', { n: days.length })) : null,
      h('section.card',
        h('div.card-head', h('h2', t('Frequencies')), h('span.faint.small', t('Click a row to open its recordings'))),
        h('div.table-wrap', h('table',
          h('thead', h('tr', h('th', t('MHz')), h('th', t('Name')), h('th.num', t('Count')), h('th', t('Quality')), h('th', t('Total time')), h('th', t('Last heard')))),
          h('tbody', s.frequencies.map((f) => h('tr', {
            style: { cursor: 'pointer' },
            onclick: () => { sessionStorage.setItem('openFreq', `freq:${f.freq_khz}`); location.hash = '#/recordings'; },
          },
          h('td.mono', f.freq_khz ? fmtChannel(f.freq_khz * 1000) : t('Manual recording')),
          h('td', f.name || h('span.faint', '–')),
          h('td.num', f.count.toLocaleString()),
          h('td', qualityBadge(f.quality)),
          h('td.mono', fmtDuration(f.duration)),
          h('td.small.muted', fmtAgo(f.last)))))))));
  }
  load();
  return {};
}
