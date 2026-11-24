// "Add-ons" page, the decoders and tools card: ACARS and APRS (our own decoders on the scanner's channels)
// and rtl_433 / VDL2 (external programs that take the receiver for themselves). Transcription and digital
// voice live on the same page (settings.js, scope 'addons').
import { api, errorToast, h, icon, select, t, toast, toggle } from '../core.js';
import { S, cfg, loadSettings, patchConfigNow } from '../state.js';
import { newBand } from './bands.js';

const mhz = (hz) => (hz / 1e6).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
// "131.525, 131.725" separates (comma and space); "131,525" (comma between digits) is a decimal comma.
const parseMhz = (text) => text.replace(/,\s+/g, ' ').split(/[\s;]+/).filter(Boolean).map((v) => Math.round(parseFloat(v.replace(',', '.')) * 1e6)).filter((v) => v > 0);

const gcd = (a, b) => (b ? gcd(b, a % b) : a);
// Is this frequency inside a scanned band or a Memory List channel? (A schedule may still switch it off.)
const scanned = (hz) => cfg().bands.some((b) => b.enabled !== false && hz >= b.start_hz - 5000 && hz <= b.stop_hz + 5000)
  || (S.settings.channels || []).some((c) => c.enabled !== false && Math.abs(c.freq_hz - hz) <= 5000);

function addToScan(a) {
  const f = [...a.frequencies].sort((x, y) => x - y);
  const step = a.id === 'tetra' ? 25000 : (f.length > 1 ? f.slice(1).reduce((g, v, i) => gcd(g, v - f[i]), 0) : 12500);
  const mod = a.id === 'acars' ? 'AM' : a.id === 'tetra' ? 'TETRA' : 'NFM';
  const band = newBand({ start_hz: f[0], stop_hz: f[f.length - 1], step_hz: step, modulation: mod, label: a.name });
  return patchConfigNow({ bands: [...cfg().bands, band] });
}

const devName = (d) => {
  const args = String(d.device_args || '');
  return (args.match(/label=([^,]+)/) || args.match(/serial=([^,]+)/) || [])[1] || d.driver || '';
};
export const receiverName = (i) => {
  if (i === 0) return t('Receiver 1 (main)');
  const extra = (cfg().device.extra_devices || [])[i - 1];
  return extra ? `${t('Receiver {n}', { n: i + 1 })} (${devName(extra)})` : t('Receiver {n}', { n: i + 1 });
};

function setting(title, desc, control) {
  if (control && control.querySelector) {
    const field = control.matches('input, select') ? control : control.querySelector('input, select');
    if (field && !field.getAttribute('aria-label')) field.setAttribute('aria-label', title);
  }
  return h('div.setting', h('div', h('b', title), desc ? h('p', desc) : null), control);
}

export function decodersCard(box) {
  let rows = [];
  let busy = false;
  let starting = '';   // the add-on being started: stopping the scanner and waiting for the receiver takes seconds
  let timer = null;
  let dead = false;

  async function act(path, body) {
    busy = true;
    if (path === '/api/addons/run') {
      starting = body.id;
      toast(t('Starting… the scanner stops first and the receiver needs a moment.'));
      render();
    }
    try {
      const r = await api.post(path, body);
      rows = r.addons;
      await loadSettings();     // the navigation shows "Messages" as soon as something is on
    } catch (e) { errorToast(e); }
    busy = false;
    starting = '';
    if (!dead) render();
  }

  // The TETRA speech codec: ETSI's own code, fetched from etsi.org and compiled here - never shipped by FalconSDR.
  function codecInstall(a) {
    const st = a.codec_install;
    if (!st) return null;
    const parts = [];
    if (a.codec_installed) {
      parts.push(h('div.row.wrap', h('span.chip.good', t('Speech codec installed')),
        h('button.ghost.danger', { onclick: () => act('/api/addons/uninstall', { id: 'tetra_codec' }) }, icon('trash'), t('Remove the codec'))));
    } else {
      parts.push(h('p.faint.small', { style: { margin: 0 } },
        t('Clear (unencrypted) calls need ETSI\'s speech codec. FalconSDR does not contain it: one click downloads it from etsi.org to this computer and compiles it here (a C compiler is needed). ETSI publishes this code for implementers of TETRA, but that covers the code only, not patents - the codec may be patented in your country, check before you use it. Encrypted calls are never decoded.')),
        h('div.row.wrap',
          st.running ? h('span.chip.accent.live', h('span.dot'), t('Installing…'))
            : h('button.primary', { onclick: () => act('/api/addons/install', { id: 'tetra_codec' }) }, icon('download'), t('Download the speech codec from ETSI'))));
    }
    if (st.log && st.log.length && !a.codec_installed) parts.push(h('pre.msg-detail', st.log.join('\n')));
    if (st.error) parts.push(h('span', { style: { color: 'var(--red)' } }, t('Installation failed: {error}', { error: st.error })));
    if (st.hint) parts.push(h('div.col', { style: { gap: '4px' } },
      h('span.small', t('Install what the build needs first (this needs administrator rights, so FalconSDR does not run it):')), h('code', st.hint)));
    if (st.link) parts.push(h('a', { href: st.link, target: '_blank', rel: 'noopener' }, st.link));
    return h('div.col', { style: { gap: '8px', margin: '8px 0' } }, ...parts);
  }

  async function load() {
    try { rows = (await api.get('/api/addons')).addons; } catch { return; }
    if (dead) return;
    const a = document.activeElement;
    const editing = box.contains(a) && ['INPUT', 'SELECT', 'SUMMARY'].includes(a.tagName);   // a refresh would wipe what is being typed
    if (!busy && !editing) render();
    if (rows.some((r) => (r.install && r.install.running) || (r.codec_install && r.codec_install.running))) { clearInterval(timer); timer = setInterval(load, 1500); } else { clearInterval(timer); timer = setInterval(load, 3000); }
  }

  function frequencyEditor(a) {
    const input = h('input', { value: a.frequencies.map(mhz).join(', '), style: { width: '260px' }, 'aria-label': t('Channels (MHz)') });
    const save = h('button', { onclick: () => act('/api/addons/configure', { id: a.id, frequencies: parseMhz(input.value) }) }, t('Save'));
    return h('div.row.wrap', input, save);
  }

  // With several receivers each add-on gets its own: an external program holds that one while the others
  // keep scanning; a built-in decoder only decodes what that one hears.
  function receiverPicker(a) {
    const extras = cfg().device.extra_devices || [];
    if (!extras.length) return null;
    const options = [...(a.kind === 'builtin' ? [['-1', t('Any receiver')]] : []), ...[0, ...extras.map((_, i) => i + 1)].map((i) => [String(i), receiverName(i)])];
    return setting(t('Receiver'), a.kind === 'builtin' ? t('Decode only what this receiver hears.') : t('This program takes this receiver; the others keep scanning.'),
      select(options, String(a.receiver), (v) => act('/api/addons/configure', { id: a.id, receiver: +v }), { style: { width: 'auto' }, disabled: a.running }));
  }

  // -- settings of an external program: friendly controls that the server turns into the program's flags ------
  const BANDS = () => [
    ['433', t('433.92 MHz - most sensors')], ['868', t('868.3 MHz - European 868 MHz sensors')], ['315', t('315 MHz - US sensors and tyre pressure')],
    ['345', t('345 MHz - security sensors')], ['915', t('915 MHz - US meters and sensors')], ['wmbus', t('868.95 MHz - wireless M-Bus meters (water, heat)')],
    ['433+868', t('433.92 and 868.3 MHz (switches between them)')], ['custom', t('Own frequencies…')],
  ];
  const setOption = (a, key, value) => act('/api/addons/configure', { id: a.id, options: { [key]: value } });
  const numberOption = (a, key, attrs) => h('input', { type: 'number', value: a.options[key], style: { width: '110px' }, ...attrs,
    onchange: (e) => setOption(a, key, e.target.value) });

  // The decoders rtl_433 keeps off by default (too many false alarms): listed by the program itself, tick the ones you have.
  // The element is built once and reused by every render of the card (the page refreshes every few seconds and after each
  // change), otherwise it would close, reload and lose its scroll position under the user's hand; ticking saves quietly.
  const pickers = new Map();
  function decoderPicker(a) {
    const cached = pickers.get(a.id);
    if (cached) { requestAnimationFrame(() => { cached.list.scrollTop = cached.top; }); return cached.details; }
    const list = h('div.col', { style: { gap: '4px', maxHeight: '320px', overflow: 'auto' } });
    const chosen = new Set(a.options.extra_decoders);
    const summary = h('summary');
    const details = h('details.addon-advanced', summary, list);
    const entry = { details, list, top: 0 };
    list.addEventListener('scroll', () => { entry.top = list.scrollTop; });
    const label = () => { summary.textContent = t('Extra decoders ({n} switched on)', { n: chosen.size }); };
    let timer = null;
    const save = () => {
      label();
      clearTimeout(timer);
      timer = setTimeout(() => api.post('/api/addons/configure', { id: a.id, options: { extra_decoders: [...chosen] } }).catch(errorToast), 400);
    };
    label();
    let loaded = false;
    details.addEventListener('toggle', async () => {
      if (!details.open || loaded) return;
      loaded = true;
      list.replaceChildren(h('span.faint.small', t('Loading…')));
      let known = [];
      try { known = (await api.get('/api/addons/protocols')).protocols.filter((p) => !p.default); } catch { /* shown as empty below */ }
      const boxes = known.map((p) => h('input', { type: 'checkbox', checked: chosen.has(p.id), onchange: (e) => { if (e.target.checked) chosen.add(p.id); else chosen.delete(p.id); save(); } }));
      const setAll = (on) => { boxes.forEach((x, i) => { x.checked = on; if (on) chosen.add(known[i].id); else chosen.delete(known[i].id); }); save(); };
      list.replaceChildren(
        known.length ? h('div.row.wrap', { style: { gap: '8px' } }, h('button.small', { onclick: () => setAll(true) }, t('Select all')), h('button.small', { onclick: () => setAll(false) }, t('Clear')),
          h('span.faint.small', t('These decoders are off by default because they report false alarms more often.'))) : h('span.faint.small', t('The program did not list its decoders.')),
        ...known.map((p, i) => h('label.switch', boxes[i], h('span.label', p.name))));
    });
    pickers.set(a.id, entry);
    return details;
  }

  function optionRows(a) {
    const o = a.options;
    const rowsOut = [];
    if (a.id === 'rtl_433') {
      rowsOut.push(setting(t('Band'), t('Which frequencies to listen to. Most weather stations and thermometers use 433.92 MHz; many European sensors use 868.3 MHz.'),
        select(BANDS(), o.band, (v) => setOption(a, 'band', v), { style: { width: 'auto' } })));
      const custom = (o.custom_mhz || '').split(/\s+/).filter(Boolean);
      if (o.band === 'custom') {
        const input = h('input', { value: o.custom_mhz, placeholder: '433.92, 868.3', style: { width: '220px' }, 'aria-label': t('Frequencies (MHz)') });
        rowsOut.push(setting(t('Frequencies (MHz)'), t('Separated by spaces or "comma and space".'),
          h('div.row.wrap', input, h('button', { onclick: () => setOption(a, 'custom_mhz', input.value) }, t('Save')))));
      }
      if (o.band === '433+868' || (o.band === 'custom' && custom.length > 1)) {
        rowsOut.push(setting(t('Switch every (seconds)'), t('With several frequencies the receiver listens to each in turn.'), numberOption(a, 'hop_s', { min: 1, max: 3600 })));
      }
    }
    if (a.id === 'rtl_433') {
      rowsOut.push(setting(t('Capture width'), t('Wider is needed by some meters and tyre sensors; automatic is right for most.'),
        select([['', t('Automatic')], ['1000k', '1000 kHz'], ['1200k', '1200 kHz'], ['2048k', '2048 kHz']], o.sample_rate, (v) => setOption(a, 'sample_rate', v), { style: { width: 'auto' } })));
    }
    rowsOut.push(setting(t('Gain (dB)'), t('Leave empty for automatic gain.'), numberOption(a, 'gain', { min: 0, max: 80, step: 'any', placeholder: t('auto') })));
    rowsOut.push(setting(t('Frequency correction (ppm)'), t('For a receiver whose frequency is off. 0 for most.'), numberOption(a, 'ppm', { min: -500, max: 500 })));
    if (a.id === 'rtl_433') {
      rowsOut.push(setting(t('Show remote controls and key fobs'), t('Off by default: they belong to somebody\'s car, garage or door. Their codes are never stored, even when this is on.'),
        toggle('', !!o.remotes, (v) => setOption(a, 'remotes', v))));
      rowsOut.push(decoderPicker(a));
      rowsOut.push(setting(t('Units'), '', select([['native', t('As the sensor reports')], ['si', t('Metric (°C, km/h)')], ['customary', t('Imperial (°F, mph)')]],
        o.units, (v) => setOption(a, 'units', v), { style: { width: 'auto' } })));
    }
    return rowsOut;
  }

  function card(a) {
    const external = a.kind === 'external';
    const status = [
      external ? h('span.chip', t('separate program')) : h('span.chip', t('built in')),
      a.experimental ? h('span.chip.warn', t('experimental')) : null,
      a.running ? h('span.chip.good.live', h('span.dot'), t('running')) : null,
      !a.installed ? h('span.chip.bad', t('not installed')) : null,
    ];
    // The switch sits with the name: it is what turns this add-on on (external programs are started with Start).
    const parts = [
      h('div.row.wrap.addon-head', h('b.addon-name', a.name), ...status, h('span.spacer'),
        external || !a.installed ? null : toggle(a.enabled ? t('On') : t('Off'), a.enabled, (v) => act('/api/addons/configure', { id: a.id, enabled: v }))),
      h('p.muted', { style: { margin: 0 } }, t(a.summary)),
      h('p.faint.small', { style: { margin: 0 } }, t('Licence: {license}', { license: t(a.license) })),
    ];
    if (!external && !a.installed) {
      parts.push(h('p', { style: { margin: '4px 0' } }, t(a.install_hint)));
      if (a.can_install) {
        const st = a.install || {};
        parts.push(h('div.row.wrap',
          st.running ? h('span.chip.accent.live', h('span.dot'), t('Installing…'))
            : h('button.primary', { onclick: () => act('/api/addons/install', { id: a.id }) }, icon('download'), t('Build decoder'))));
        if (st.log && st.log.length) parts.push(h('pre.msg-detail', st.log.join('\n')));
        if (st.error) parts.push(h('span', { style: { color: 'var(--red)' } }, t('Installation failed: {error}', { error: st.error })));
        if (st.hint) parts.push(h('div.col', { style: { gap: '4px' } },
          h('span.small', t('Install what the build needs first (this needs administrator rights, so FalconSDR does not run it):')), h('code', st.hint)));
      }
      const input = h('input', { placeholder: t('Path of the program'), value: a.path, style: { width: '320px' } });
      parts.push(setting(t('Program'), t('Where it is installed, if FalconSDR does not find it by itself.'),
        h('div.row.wrap', input, h('button', { onclick: () => act('/api/addons/configure', { id: a.id, path: input.value }) }, t('Save')))));
    } else if (!external) {
      if (a.id === 'tetra') {
        parts.push(h('p.faint.small', { style: { margin: 0 } }, t('Clear-mode only: encrypted networks and calls are detected and muted, never decrypted. You can also choose TETRA directly as the modulation of any band or Memory List channel.')));
        parts.push(codecInstall(a));
      }
      if (a.enabled || a.id === 'tetra') {
        if (a.id !== 'tetra') {
          parts.push(h('p.faint.small', { style: { margin: 0 } }, t('While scanning, a transmission on one of these channels is decoded instead of being played or recorded.')));
        }
        const missing = a.frequencies.filter((f) => !scanned(f));
        if (missing.length && a.enabled) {
          parts.push(h('div.notice', { role: 'status' }, icon('ban'),
            h('span', { style: { flex: '1 1 260px' } }, t('The scanner does not visit {list} MHz yet, so nothing can be decoded.', { list: missing.map(mhz).join(', ') })),
            h('button', { onclick: async () => { await addToScan(a); await loadSettings(); render(); } }, t('Add to scanned bands'))));
        }
        if (a.enabled) {
          parts.push(setting(t('Channels (MHz)'), a.id === 'tetra' ? t('Optional: automatically decode these carriers as TETRA even inside an NFM band.') : '', frequencyEditor(a)));
          const picker = receiverPicker(a);
          if (picker) parts.push(picker);
        }
        parts.push(h('div.row.wrap', h('span.faint.small', t('Channels heard: {slots} · messages decoded: {events}', { slots: a.stats.slots, events: a.stats.events })),
          h('a', { href: '#/messages' }, t('Open Messages'))));
        if (a.experimental) parts.push(h('p.faint.small', { style: { margin: 0 } }, t('Only tested on synthetic signals so far. If channels are heard but nothing is decoded, please report it.')));
        if (a.id === 'tetra') {
          const codecInput = h('input', { value: a.codec_path || '', placeholder: t('Optional path to external ACELP speech codec helper'), style: { width: '320px' } });
          parts.push(h('details.addon-advanced', h('summary', t('Advanced')),
            h('p.faint.small', { style: { margin: '6px 0' } }, t('Found: {path}', { path: a.program })),
            setting(t('External ACELP voice codec (optional)'), t('FalconSDR does not ship a TETRA speech codec. Without one, control channels, SDS text and LIP GPS still decode.'),
              h('div.row.wrap', codecInput, h('button', { onclick: () => act('/api/addons/configure', { id: a.id, codec_path: codecInput.value }) }, t('Save')))),
            a.managed ? h('div.row', h('button.ghost.danger', { onclick: () => act('/api/addons/uninstall', { id: a.id }) }, icon('trash'), t('Uninstall'))) : null));
        }
      }
    } else if (!a.installed) {
      parts.push(h('p', { style: { margin: '4px 0' } }, t(a.install_hint)));
      if (a.can_install) {
        const st = a.install || {};
        parts.push(h('div.row.wrap',
          st.running ? h('span.chip.accent.live', h('span.dot'), t('Installing…'))
            : h('button.primary', { onclick: () => act('/api/addons/install', { id: a.id }) }, icon('download'), t('Install')),
          h('span.faint.small', t('It is downloaded or built from the author\'s own release; FalconSDR does not ship it.'))));
        if (st.log && st.log.length) parts.push(h('pre.msg-detail', st.log.join('\n')));
        if (st.error) parts.push(h('span', { style: { color: 'var(--red)' } }, t('Installation failed: {error}', { error: st.error })));
        if (st.hint) parts.push(h('div.col', { style: { gap: '4px' } },
          h('span.small', t('Install what the build needs first (this needs administrator rights, so FalconSDR does not run it):')), h('code', st.hint)));
      }
      const input = h('input', { placeholder: t('Path of the program'), value: a.path, style: { width: '320px' } });
      parts.push(setting(t('Program'), t('Where it is installed, if FalconSDR does not find it by itself.'),
        h('div.row.wrap', input, h('button', { onclick: () => act('/api/addons/configure', { id: a.id, path: input.value }) }, t('Save')))));
      if (a.homepage) parts.push(h('a', { href: a.homepage, target: '_blank', rel: 'noopener' }, t('Project page')));
    } else {
      const several = (cfg().device.extra_devices || []).length > 0;
      parts.push(h('div.row.wrap',
        starting === a.id
          ? h('button.primary', { disabled: true }, h('span.spinner'), t('Starting…'))
          : a.running
            ? h('button.primary', { onclick: () => act('/api/addons/stop', { id: a.id }) }, icon('stop'), t('Stop and resume scanning'))
            : h('button.primary', { disabled: !!starting, onclick: () => act('/api/addons/run', { id: a.id }) }, icon('play'),
              several ? t('Start') : t('Start (pauses the scanner)')),
        h('span.faint.small', several ? t('It needs its receiver for itself: that receiver stops scanning while it runs.')
          : t('It needs the receiver for itself: the scanner stops while it runs.'))));
      if (a.error) parts.push(h('span', { style: { color: 'var(--red)' } }, a.error));
      parts.push(h('h4.addon-sub', t('Settings')));
      if (a.running) parts.push(h('p.faint.small', { style: { margin: 0 } }, t('Changes apply the next time it starts.')));
      if (a.frequencies.length) parts.push(setting(t('Channels (MHz)'), '', frequencyEditor(a)));
      parts.push(...optionRows(a));
      const picker = receiverPicker(a);
      if (picker) parts.push(picker);
      const args = h('input', { value: a.extra_args, placeholder: t('Extra options, for example -g 40'), style: { width: '320px' } });
      parts.push(h('details.addon-advanced', h('summary', t('Advanced')),
        h('p.faint.small', { style: { margin: '6px 0' } }, t('Found: {path}', { path: a.program })),
        setting(t('Extra options'), t('Added to the command line.'),
          h('div.row.wrap', args, h('button', { onclick: () => act('/api/addons/configure', { id: a.id, extra_args: args.value }) }, t('Save')))),
        a.managed && !a.running ? h('div.row', h('button.ghost.danger', { onclick: () => act('/api/addons/uninstall', { id: a.id }) }, icon('trash'), t('Uninstall'))) : null));
    }
    return h('section.addon-card', ...parts);
  }

  function render() {
    const listen = cfg().addons.listen_to_data;
    box.replaceChildren(
      h('p.muted', { style: { margin: 0 } }, t('Optional decoders. The built-in ones read the channels your scanner stops on; the separate programs are installed by you and use the receiver on their own - FalconSDR only starts and stops them and shows what they decode in Messages.')),
      ...rows.filter((a) => a.kind === 'builtin').map(card),
      h('section.addon-card',
        h('div.row.wrap.addon-head', h('b.addon-name', t('Hear and record data channels too')), h('span.spacer'),
          toggle(listen ? t('On') : t('Off'), listen, async (v) => { await act('/api/addons/configure', { id: 'acars', listen_to_data: v }); })),
        h('p.faint.small', { style: { margin: 0 } }, t('Normally ACARS and APRS channels are only decoded - their buzz is not played or recorded.'))),
      ...rows.filter((a) => a.kind === 'external').map(card));
  }

  load();
  timer = setInterval(load, 3000);
  return () => { dead = true; clearInterval(timer); };
}
