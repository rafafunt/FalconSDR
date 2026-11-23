// "Statistics": what was heard, when and how well.
import {
  LANGUAGE_NAMES, api, errorToast, fmtAgo, fmtChannel, fmtDate, fmtDuration, h, icon, mount, prefs, qs, qualityBadge, t, tabs, toggle,
} from '../core.js';
import { addonsInUse } from '../state.js';
import { addonStats } from './stats-addons.js';

// Kept across visits to the tab (e.g. toggling UTC or bouncing to Recordings).
let selectedFilter = '';

// Two scopes: what was heard on the air (recordings) and what the add-ons decoded. #/stats is the first,
// #/stats/addons[/<id>] the second - offered only when add-ons are in use.
export function statsView(root, hash = '') {
  const m = hash.match(/^#\/stats\/addons(?:\/(\w+))?/);
  const hasAddons = addonsInUse();
  const scope = m && hasAddons ? 'addons' : 'voice';
  const filterBar = h('div.row.wrap');
  const body = h('div.col', { style: { gap: '16px' } }, h('div.empty', t('Loading…')));
  let utc = prefs.utc;
  let addons = null;
  let facets = null;
  mount(root, h('div.page',
    h('div.page-head', h('h1', t('Statistics')),
      toggle(t('UTC'), utc, (v) => { utc = prefs.utc = v; if (addons) { addons.destroy(); addons = addonStats({ body, sub: m && m[1], utc }); } else load(); })),
    hasAddons ? tabs([['voice', t('Voice: what was heard')], ['addons', t('Add-ons: what was decoded')]], scope, (v) => { location.hash = v === 'addons' ? '#/stats/addons' : '#/stats'; }) : null,
    scope === 'voice' ? filterBar : null,
    body));
  if (scope === 'addons') {
    addons = addonStats({ body, sub: m[1], utc });
    return { destroy() { addons.destroy(); } };
  }

  function renderFilter() {
    if (!facets || !facets.total) { filterBar.replaceChildren(); return; }
    const sel = h('select', {
      'aria-label': t('Filter statistics'),
      style: { maxWidth: 'min(420px, 100%)' },
      onchange: (e) => { selectedFilter = e.target.value; renderFilter(); load(); },
    });
    const opt = (val, label) => h('option', { value: val, selected: selectedFilter === val }, label);
    sel.append(opt('', `${t('All recordings')} (${facets.total.toLocaleString()})`));
    if (facets.favorites) sel.append(opt('fav', `★ ${t('Favorites')} (${facets.favorites.toLocaleString()})`));
    if (facets.bands && facets.bands.length) {
      const grp = h('optgroup', { label: t('Bands') });
      [...facets.bands].sort((a, b) => b.count - a.count).forEach((b) => {
        grp.append(opt(b.key, `${t(b.label)} (${b.count.toLocaleString()})`));
      });
      sel.append(grp);
    }
    if (facets.frequencies && facets.frequencies.length) {
      const grp = h('optgroup', { label: t('Frequencies') });
      [...facets.frequencies].sort((a, b) => b.count - a.count).forEach((f) => {
        const name = f.freq_khz === 0 ? t('Manual recording') : `${fmtChannel(f.freq_khz * 1000)} MHz${f.name ? ` · ${f.name}` : ''}`;
        grp.append(opt(f.key, `${name} (${f.count.toLocaleString()})`));
      });
      sel.append(grp);
    }
    if (facets.languages && facets.languages.length > 1) {
      const grp = h('optgroup', { label: t('Languages') });
      [...facets.languages].sort((a, b) => b.count - a.count).forEach((l) => {
        const label = l.language ? (LANGUAGE_NAMES[l.language] || l.language.toUpperCase()) : t('(no transcript)');
        grp.append(opt(l.key, `${label} (${l.count.toLocaleString()})`));
      });
      sel.append(grp);
    }
    mount(filterBar,
      h('span.small.muted', t('Scope:')),
      sel,
      selectedFilter ? h('button.ghost', { onclick: () => { selectedFilter = ''; renderFilter(); load(); } }, icon('x'), t('Show all')) : null,
      selectedFilter ? h('button', { onclick: () => { sessionStorage.setItem('openFreq', selectedFilter); location.hash = '#/recordings'; } }, icon('library'), t('Open recordings')) : null);
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
    try {
      const params = { utc: utc ? 1 : '' };
      if (selectedFilter) params.sel = [selectedFilter];
      s = await api.get('/api/library/stats' + qs(params));
    } catch (e) { errorToast(e); return; }
    const sum = s.summary;
    if (!sum.count) {
      mount(body, h('div.card', h('div.empty', selectedFilter
        ? t('No recordings match this filter.')
        : t('No recordings yet - statistics appear once something has been recorded.'))));
      return;
    }
    const days = s.per_day.slice(-45);
    const weekdays = [t('Mon'), t('Tue'), t('Wed'), t('Thu'), t('Fri'), t('Sat'), t('Sun')];
    let freqQ = '';
    let showAllFreqs = false;
    const freqTableHost = h('div.col', { style: { gap: '10px' } });
    const renderFreqTable = () => {
      const q = freqQ.trim().toLowerCase();
      const rows = q
        ? s.frequencies.filter((f) => `${f.freq_khz ? fmtChannel(f.freq_khz * 1000) : t('Manual recording')} ${f.name || ''}`.toLowerCase().includes(q))
        : s.frequencies;
      const limit = showAllFreqs || q ? rows.length : 100;
      const visible = rows.slice(0, limit);
      mount(freqTableHost,
        visible.length
          ? h('div.table-wrap', h('table',
            h('thead', h('tr', h('th', t('MHz')), h('th', t('Name')), h('th.num', t('Count')), h('th', t('Quality')), h('th', t('Total time')), h('th', t('Last heard')), h('th', ''))),
            h('tbody', visible.map((f) => h('tr', {
              style: { cursor: 'pointer' },
              onclick: () => { sessionStorage.setItem('openFreq', `freq:${f.freq_khz}`); location.hash = '#/recordings'; },
            },
            h('td.mono', f.freq_khz ? fmtChannel(f.freq_khz * 1000) : t('Manual recording')),
            h('td', f.name || h('span.faint', '–')),
            h('td.num', f.count.toLocaleString()),
            h('td', qualityBadge(f.quality)),
            h('td.mono', fmtDuration(f.duration)),
            h('td.small.muted', fmtAgo(f.last)),
            h('td', h('div.actions', selectedFilter === `freq:${f.freq_khz}` ? null : h('button.icon.ghost', {
              title: t('Show statistics for this frequency'),
              onclick: (e) => { e.stopPropagation(); selectedFilter = `freq:${f.freq_khz}`; renderFilter(); load(); },
            }, icon('stats')))))))))
          : h('div.empty', t('Nothing matches these filters.')),
        rows.length > limit
          ? h('div.row', { style: { justifyContent: 'center' } },
            h('button.ghost', { onclick: () => { showAllFreqs = true; renderFreqTable(); } }, t('Show all {n}', { n: rows.length.toLocaleString() })))
          : null);
    };
    renderFreqTable();
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
        h('div.card-head', h('h2', t('Frequencies')),
          s.frequencies.length > 15
            ? h('input', { type: 'search', placeholder: t('Filter…'), style: { width: 'min(220px, 100%)' }, oninput: (e) => { freqQ = e.target.value; renderFreqTable(); } })
            : null,
          h('span.faint.small', t('Click a row to open its recordings'))),
        freqTableHost));
  }
  api.get('/api/library/facets').then((f) => { facets = f; renderFilter(); }).catch(() => {});
  load();
  return {};
}
