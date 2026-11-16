// "Notifications" add-on card (bottom of the Add-ons page): rules that say when something is worth a message, and the
// channels it goes to (ntfy, Telegram, a webhook, MQTT). Everything is saved through /api/notify (channels hold secrets).
import { api, errorToast, fmtTime, h, select, t, toast, toggle } from '../core.js';
import { cfg } from '../state.js';

const SOURCES = () => [['message', t('A decoded message or sensor reading')], ['recording', t('A recording')], ['transcript', t('A word in a transcript')], ['system', t('Something went wrong')]];
const ADDONS = () => [['vdl2', 'VDL2'], ['acars', 'ACARS'], ['aprs', 'APRS'], ['rtl_433', t('Sensors')]];
const COOLDOWNS = () => [['30', t('30 seconds')], ['60', t('1 minute')], ['300', t('5 minutes')], ['3600', t('1 hour')], ['21600', t('6 hours')], ['86400', t('24 hours')]];
const CHANNEL_TYPES = () => [['ntfy', 'ntfy'], ['telegram', 'Telegram'], ['webhook', t('Webhook')], ['mqtt', 'MQTT']];

// Rules to start from: one click, then adjust.
const TEMPLATES = () => [
  [t('A sensor reading crosses a limit (set it below)'), { source: 'message', addons: ['rtl_433'], metric: 'temperature_C', op: '<', value: 0, cooldown_s: 3600 }],
  [t('A sensor battery is low'), { source: 'message', addons: ['rtl_433'], metric: 'battery_ok', op: '<', value: 1, cooldown_s: 86400 }],
  [t('A particular aircraft, station or device (name it below)'), { source: 'message', addons: ['vdl2', 'acars'], who: '', cooldown_s: 3600 }],
  [t('Words in ACARS / VDL2 messages'), { source: 'message', addons: ['vdl2', 'acars'], text_any: 'MAYDAY, PAN PAN, FUEL', cooldown_s: 300 }],
  [t('An APRS station within a distance (set it below)'), { source: 'message', addons: ['aprs'], within_km: 50, cooldown_s: 3600 }],
  [t('Activity on a chosen frequency (set it below)'), { source: 'recording', freq_mhz: 121.5, cooldown_s: 60 }],
  [t('Chosen words in a transcript (set them below)'), { source: 'transcript', text_any: 'mayday, pan pan', cooldown_s: 300 }],
  [t('A receiver loses its connection'), { source: 'system', event: 'receiver_lost', cooldown_s: 600 }],
  [t('An add-on stops by itself'), { source: 'system', event: 'addon_failed', cooldown_s: 600 }],
];

const FIELDS = {
  ntfy: [['server', 'Server', 'https://ntfy.sh', 'text'], ['topic', 'Topic', 'falconsdr-yourname', 'text'], ['token', 'Access token (if the topic is protected)', '', 'password']],
  telegram: [['token', 'Bot token', '123456:ABC…', 'password'], ['chat_id', 'Chat id', '', 'text']],
  webhook: [['url', 'Address', 'https://…', 'text'], ['header_name', 'Header (optional)', 'Authorization', 'text'], ['header_value', 'Header value', '', 'password']],
  mqtt: [['host', 'Broker', '192.168.1.10', 'text'], ['port', 'Port', '1883', 'number'], ['username', 'User name', '', 'text'], ['password', 'Password', '', 'password'], ['topic', 'Topic', 'falconsdr/notify', 'text']],
};

export function notifyCard(box) {
  let state = { enabled: false, channels: [], rules: [], recent: [] };
  let metrics = [];
  let timer = null;
  let saving = null;
  const recentBox = h('div.col', { style: { gap: '4px' } });

  // A channel that is still being filled in is kept here and not sent (the server refuses an incomplete one).
  const REQUIRED = { ntfy: ['topic'], telegram: ['token', 'chat_id'], webhook: ['url'], mqtt: ['host'] };
  const ready = (c) => c.id || REQUIRED[c.type].every((k) => String(c.settings[k] || '').trim());

  async function save(rerender = false) {
    clearTimeout(saving);
    const drafts = state.channels.filter((c) => !ready(c));
    try {
      const r = await api.post('/api/notify/save', { enabled: state.enabled, channels: state.channels.filter(ready), rules: state.rules });
      state = { ...r, channels: [...r.channels, ...drafts], recent: state.recent };
    } catch (e) { errorToast(e); return; }
    if (rerender) render();
  }
  const saveSoon = () => { clearTimeout(saving); saving = setTimeout(() => save(false), 500); };

  // -- channels ---------------------------------------------------------------------------------------------
  function channelCard(c) {
    const fields = FIELDS[c.type].map(([key, label, ph, type]) => h('label.field', t(label),
      h('input', { type, value: c.settings[key] ?? '', placeholder: ph, autocomplete: 'off',
        onchange: (e) => { c.settings[key] = type === 'number' ? +e.target.value : e.target.value; save(true); } })));
    const extras = c.type === 'mqtt' ? [h('label.switch', h('input', { type: 'checkbox', checked: !!c.settings.tls, onchange: (e) => { c.settings.tls = e.target.checked; save(true); } }), h('span.label', t('Encrypted connection (TLS)')))] : [];
    const testBtn = h('button', { onclick: async () => {
      testBtn.disabled = true;
      try {
        const r = await api.post('/api/notify/test', { id: c.id, channel: c });          // the draft itself: no need to save first
        toast(r.error ? t('The test failed: {error}', { error: r.error }) : t('Test sent - did it arrive?'), r.error ? 'bad' : undefined);
      } catch (e) { errorToast(e); }
      testBtn.disabled = false;
    } }, t('Send a test'));
    return h('div.notify-box',
      h('div.row.wrap', h('input', { value: c.name, style: { flex: '1 1 160px' }, 'aria-label': t('Name'), onchange: (e) => { c.name = e.target.value; save(true); } }),
        h('span.chip', CHANNEL_TYPES().find(([v]) => v === c.type)[1]), h('span.spacer'),
        toggle(t('On'), c.enabled, (v) => { c.enabled = v; save(true); }),
        testBtn, h('button.ghost.danger', { onclick: () => { state.channels = state.channels.filter((x) => x !== c); state.rules.forEach((r) => { r.channels = r.channels.filter((id) => id !== c.id); }); save(true); } }, t('Remove'))),
      h('div.grid.two', ...fields), ...extras);
  }

  function addChannel(type) {
    const settings = Object.fromEntries(FIELDS[type].map(([k]) => [k, '']));
    if (type === 'ntfy') settings.server = 'https://ntfy.sh';
    if (type === 'mqtt') { settings.port = 1883; settings.tls = false; settings.topic = 'falconsdr/notify'; }
    state.channels.push({ id: '', type, name: CHANNEL_TYPES().find(([v]) => v === type)[1], enabled: true, settings });
    render();         // saved when the fields are filled in (the server rejects an empty one)
  }

  // -- rules ----------------------------------------------------------------------------------------------------
  function sentence(r) {
    const where = r.channels.length ? state.channels.filter((c) => r.channels.includes(c.id)).map((c) => c.name).join(', ') : t('only in the app');
    const what = {
      message: () => [r.addons.length ? r.addons.map((a) => (ADDONS().find(([v]) => v === a) || [0, a])[1]).join(', ') : t('any add-on'),
        r.text_any && t('containing {words}', { words: r.text_any }), r.who && t('about {who}', { who: r.who }), r.label && t('label {label}', { label: r.label }),
        r.freq_mhz && `${r.freq_mhz} MHz`, r.metric && `${(metrics.find((m) => m.key === r.metric) || { label: r.metric }).label} ${r.op} ${r.value}`,
        r.within_km && t('within {km} km', { km: r.within_km })].filter(Boolean).join(' · '),
      recording: () => r.freq_mhz ? t('a recording on {f} MHz', { f: r.freq_mhz }) : t('any recording'),
      transcript: () => [r.freq_mhz && `${r.freq_mhz} MHz`, r.text_any && t('containing {words}', { words: r.text_any })].filter(Boolean).join(' · ') || t('any transcript'),
      system: () => (r.event === 'addon_failed' ? t('an add-on stops by itself') : t('a receiver loses its connection')),
    }[r.source]();
    return `${t('When')}: ${what} → ${where}`;
  }

  function ruleCard(r) {
    const set = (key, value, rerender = false) => { r[key] = value; save(rerender); };
    const text = (key, label, ph = '', width = '220px') => h('label.field', label, h('input', { value: r[key] ?? '', placeholder: ph, style: { width }, onchange: (e) => set(key, e.target.value, true) }));
    const num = (key, label) => h('label.field', label, h('input', { type: 'number', min: 0, step: 'any', value: r[key] || '', style: { width: '110px' }, onchange: (e) => set(key, +e.target.value || 0, true) }));
    const rows = [];
    if (r.source === 'message') {
      rows.push(h('div.row.wrap', { style: { gap: '6px' } }, h('b', t('From')), ...ADDONS().map(([id, name]) => h('button.small.day', { class: r.addons.includes(id) ? 'on' : '', 'aria-pressed': String(r.addons.includes(id)),
        onclick: () => set('addons', r.addons.includes(id) ? r.addons.filter((x) => x !== id) : [...r.addons, id], true) }, name)), h('span.faint.small', t('none selected = any'))));
      rows.push(h('div.row.wrap', { style: { gap: '12px', alignItems: 'end' } }, text('text_any', t('Contains any of'), 'MAYDAY, FUEL'), text('who', t('Aircraft, station or device'), 'SP-LWA, LO123, Nexus-T #7'),
        text('label', t('ACARS label'), 'H1', '80px'), num('freq_mhz', t('Frequency (MHz)')), num('within_km', t('Within (km)'))));
      rows.push(h('div.row.wrap', { style: { gap: '8px', alignItems: 'center' } }, h('b', t('Sensor reading')),
        select([['', t('(any)')], ...metrics.map((m) => [m.key, `${t(m.label)}${m.unit ? ' (' + m.unit + ')' : ''}`])], r.metric, (v) => set('metric', v, true), { style: { width: 'auto' } }),
        r.metric ? select([['<', t('below')], ['>', t('above')]], r.op, (v) => set('op', v, true), { style: { width: 'auto' } }) : null,
        r.metric ? h('input', { type: 'number', step: 'any', value: r.value, style: { width: '110px' }, onchange: (e) => set('value', +e.target.value, true) }) : null));
    } else if (r.source === 'recording') {
      rows.push(h('div.row.wrap', { style: { gap: '12px', alignItems: 'end' } }, num('freq_mhz', t('Frequency (MHz)')), h('span.faint.small', t('0 = any frequency'))));
    } else if (r.source === 'transcript') {
      rows.push(h('div.row.wrap', { style: { gap: '12px', alignItems: 'end' } }, text('text_any', t('Contains any of'), 'mayday, pan pan'), num('freq_mhz', t('Frequency (MHz)'))));
    } else {
      rows.push(h('div.row.wrap', h('b', t('Event')), select([['receiver_lost', t('A receiver loses its connection')], ['addon_failed', t('An add-on stops by itself')]], r.event, (v) => set('event', v, true), { style: { width: 'auto' } })));
    }
    rows.push(h('div.row.wrap', { style: { gap: '10px' } }, h('b', t('Send to')),
      ...(state.channels.length ? state.channels.map((c) => h('label.switch', h('input', { type: 'checkbox', checked: r.channels.includes(c.id), disabled: !c.id,
        onchange: (e) => set('channels', e.target.checked ? [...r.channels, c.id] : r.channels.filter((x) => x !== c.id), true) }), h('span.label', c.name))) : [h('span.faint.small', t('no channel yet'))]),
      h('span.faint.small', t('It always appears in the app as well.'))));
    rows.push(h('div.row.wrap', h('b', t('At most once per')), select(COOLDOWNS(), String(r.cooldown_s), (v) => set('cooldown_s', +v, true), { style: { width: 'auto' } }),
      h('span.faint.small', t('for the same aircraft, station, device or frequency; more matches in that time are counted in the next notice'))));
    return h('div.notify-box',
      h('div.row.wrap', h('input', { value: r.name, style: { flex: '1 1 200px' }, 'aria-label': t('Name'), onchange: (e) => set('name', e.target.value, true) }),
        select(SOURCES(), r.source, (v) => set('source', v, true), { style: { width: 'auto' } }), h('span.spacer'),
        toggle(t('On'), r.enabled, (v) => set('enabled', v, true)),
        h('button.ghost.danger', { onclick: () => { state.rules = state.rules.filter((x) => x !== r); save(true); } }, t('Remove'))),
      ...rows, h('p.small.muted', { style: { margin: 0 } }, sentence(r)));
  }

  function addRule(index) {
    const [label, base] = TEMPLATES()[index];
    const name = label.replace(/\s*\([^)]*\)\s*$/, '');        // the hint in brackets is for the list, not for the title of every notification
    state.rules.push({ id: '', name, enabled: true, source: 'message', channels: state.channels.filter((c) => c.enabled && c.id).map((c) => c.id), cooldown_s: 300,
      addons: [], text_any: '', who: '', label: '', freq_mhz: 0, metric: '', op: '<', value: 0, within_km: 0, event: 'receiver_lost', ...base });
    save(true);
  }

  // -- the whole card ---------------------------------------------------------------------------------------
  function render() {
    const pickChannel = select([['', t('Add a channel…')], ...CHANNEL_TYPES()], '', (v) => { if (v) addChannel(v); }, { style: { width: 'auto' } });
    const pickRule = select([['', t('Add a rule…')], ...TEMPLATES().map(([name], i) => [String(i), name])], '', (v) => { if (v !== '') addRule(+v); }, { style: { width: 'auto' } });
    box.replaceChildren(h('section.addon-card',
      h('div.row.wrap.addon-head', h('b.addon-name', t('Notifications')), h('span.chip', t('built in')), h('span.spacer'),
        toggle(state.enabled ? t('On') : t('Off'), state.enabled, (v) => { state.enabled = v; save(true); })),
      h('p.muted', { style: { margin: 0 } }, t('Tells you when something is worth knowing: a message that matches, a sensor reading, a recording on a frequency, a word in a transcript, a receiver that fell off. In the app it appears as a notice; the channels below send it to your phone or your home automation.')),
      h('p.faint.small', { style: { margin: 0 } }, t('Nothing is sent anywhere until you add a channel. The text of a message then goes to that service (ntfy.sh, Telegram...) - use your own server if that matters.')),
      h('h4.addon-sub', t('Where to send')),
      ...state.channels.map(channelCard), h('div.row', pickChannel),
      h('h4.addon-sub', t('Rules')),
      ...(state.rules.length ? state.rules.map(ruleCard) : [h('p.faint.small', { style: { margin: 0 } }, t('No rules yet. Start from one of the examples.'))]), h('div.row', pickRule),
      h('h4.addon-sub', t('Recent notifications')), recentBox));
    renderRecent();
  }

  function renderRecent() {
    const items = [...state.recent].reverse().slice(0, 15);
    recentBox.replaceChildren(...(items.length ? items.map((i) => h('div.row.wrap', { style: { gap: '8px', alignItems: 'baseline' } },
      h('span.mono.small.muted', fmtTime(new Date(i.ts * 1000).toISOString())), h('b', i.title), h('span.small', i.message),
      ...Object.entries(i.results).map(([name, res]) => h('span.chip', { class: res === 'ok' ? 'good' : 'warn', title: res }, `${name}: ${res === 'ok' ? t('sent') : t('failed')}`))))
      : [h('span.faint.small', t('Nothing yet.'))]));
  }

  let first = true;
  async function load() {
    try {
      const r = await api.get('/api/notify');
      if (first) {
        first = false;
        metrics = (await api.get('/api/addons/metrics')).metrics;
        state = r;
        render();
      } else { state.recent = r.recent; renderRecent(); }       // the settings change only here: just the list of recent notices moves
    } catch { /* the next poll tries again */ }
  }

  load();
  timer = setInterval(load, 5000);
  return () => clearInterval(timer);
}
