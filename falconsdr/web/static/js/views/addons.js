// "Add-ons" page, the decoders and tools card: ACARS and APRS (our own decoders on the scanner's channels)
// and rtl_433 / VDL2 (external programs that take the receiver for themselves). Transcription and digital
// voice live on the same page (settings.js, scope 'addons').
import { api, errorToast, h, icon, t, toast, toggle } from '../core.js';
import { S, cfg, loadSettings } from '../state.js';

const mhz = (hz) => (hz / 1e6).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
const parseMhz = (text) => text.split(/[\s,;]+/).filter(Boolean).map((v) => Math.round(parseFloat(v.replace(',', '.')) * 1e6)).filter((v) => v > 0);

function setting(title, desc, control) {
  return h('div.setting', h('div', h('b', title), desc ? h('p', desc) : null), control);
}

export function decodersCard(box) {
  let rows = [];
  let busy = false;
  let timer = null;

  async function act(path, body) {
    busy = true;
    try {
      const r = await api.post(path, body);
      rows = r.addons;
      await loadSettings();     // the navigation shows "Messages" as soon as something is on
    } catch (e) { errorToast(e); }
    busy = false;
    render();
  }

  async function load() {
    try { rows = (await api.get('/api/addons')).addons; } catch { return; }
    if (!busy) render();
  }

  function frequencyEditor(a) {
    const input = h('input', { value: a.frequencies.map(mhz).join(', '), style: { width: '260px' }, 'aria-label': t('Channels (MHz)') });
    const save = h('button', { onclick: () => act('/api/addons/configure', { id: a.id, frequencies: parseMhz(input.value) }) }, t('Save'));
    return h('div.row.wrap', input, save);
  }

  function card(a) {
    const external = a.kind === 'external';
    const status = [
      external ? h('span.chip', t('separate program')) : h('span.chip', t('built in')),
      a.experimental ? h('span.chip.warn', t('experimental')) : null,
      a.running ? h('span.chip.good.live', h('span.dot'), t('running')) : null,
      external && !a.installed ? h('span.chip.bad', t('not installed')) : null,
    ];
    const parts = [
      h('div.row.wrap', h('b', { style: { fontSize: '15px' } }, a.name), ...status),
      h('p.muted', { style: { margin: '4px 0 0' } }, a.summary),
      h('p.faint.small', { style: { margin: '2px 0 0' } }, t('Licence: {license}', { license: a.license })),
    ];
    if (!external) {
      parts.push(setting(t('Read these channels'), t('While scanning, a transmission on one of these channels is decoded here instead of being played or recorded. Scan a band that contains them.'),
        toggle('', a.enabled, (v) => act('/api/addons/configure', { id: a.id, enabled: v }))));
      if (a.enabled) {
        parts.push(setting(t('Channels (MHz)'), '', frequencyEditor(a)));
        parts.push(h('p.faint.small', { style: { margin: 0 } },
          t('Channels heard: {slots} · messages decoded: {events}', { slots: a.stats.slots, events: a.stats.events })));
        if (a.experimental) parts.push(h('p.faint.small', { style: { margin: 0 } }, t('Only tested on synthetic signals so far. If channels are heard but nothing is decoded, please report it.')));
      }
    } else if (!a.installed) {
      parts.push(h('p', { style: { margin: '8px 0' } }, a.install_hint));
      const input = h('input', { placeholder: t('Path of the program'), value: a.path, style: { width: '320px' } });
      parts.push(setting(t('Program'), t('Where it is installed, if FalconSDR does not find it by itself.'),
        h('div.row.wrap', input, h('button', { onclick: () => act('/api/addons/configure', { id: a.id, path: input.value }) }, t('Save')))));
      if (a.homepage) parts.push(h('a', { href: a.homepage, target: '_blank', rel: 'noopener' }, t('Project page')));
    } else {
      parts.push(h('p.faint.small', { style: { margin: '4px 0' } }, t('Found: {path}', { path: a.program })));
      if (a.frequencies.length) parts.push(setting(t('Channels (MHz)'), '', frequencyEditor(a)));
      const args = h('input', { value: a.extra_args, placeholder: t('Extra options, for example -g 40'), style: { width: '320px' } });
      parts.push(setting(t('Extra options'), t('Added to the command line.'),
        h('div.row.wrap', args, h('button', { onclick: () => act('/api/addons/configure', { id: a.id, extra_args: args.value }) }, t('Save')))));
      parts.push(h('div.row.wrap',
        a.running
          ? h('button.primary', { onclick: () => act('/api/addons/stop') }, icon('stop'), t('Stop and resume scanning'))
          : h('button.primary', { onclick: () => act('/api/addons/run', { id: a.id }) }, icon('play'), t('Start (pauses the scanner)')),
        h('span.faint.small', t('It needs the receiver for itself: the scanner stops while it runs.'))));
      if (a.error) parts.push(h('span', { style: { color: 'var(--red)' } }, a.error));
    }
    return h('div.col', { style: { gap: '8px', paddingBottom: '12px' } }, ...parts);
  }

  function render() {
    const listen = cfg().addons.listen_to_data;
    box.replaceChildren(
      h('p.muted', { style: { margin: 0 } }, t('Optional decoders. The built-in ones read the channels your scanner stops on; the separate programs are installed by you and use the receiver on their own - FalconSDR only starts and stops them and shows what they decode in Messages.')),
      ...rows.map(card),
      setting(t('Hear and record data channels too'), t('Normally ACARS and APRS channels are only decoded - their buzz is not played or recorded.'),
        toggle('', listen, async (v) => { await act('/api/addons/configure', { id: 'acars', listen_to_data: v }); })));
  }

  load();
  timer = setInterval(load, 3000);
  return () => clearInterval(timer);
}
