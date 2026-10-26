// Adding, describing and editing bands - shared by Listen ("What to scan") and Schedule.
import { carrierHz, fmtChannel, fmtMHz, formDialog, h, icon, mount, select, slider, t, toggle } from '../core.js';
import { S, cfg } from '../state.js';

export const MODULATIONS = ['AM', 'NFM', 'WFM', 'SSB', 'DMR', 'YSF'];
const DIGITAL = ['DMR', 'YSF'];
export const modLabel = (m) => (m === 'YSF' ? 'C4FM (YSF)' : m);

// Digital voice is an optional add-on (Settings → Digital voice): until it is
// installed DMR/C4FM show greyed out, and picking one leads there instead.
export function modOptions() {
  const ready = S.settings.capabilities.digital;
  return MODULATIONS.map((m) => (DIGITAL.includes(m) && !ready
    ? [m, `${modLabel(m)} · ${t('optional download')}`, offerDigital]
    : [m, modLabel(m)]));
}

function offerDigital() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));  // closes the form dialog it was picked in
  location.hash = '#/settings/digital';
}
export const OVERRIDES = ['trigger_level_dbfs', 'tuning_time_ms', 'linger_time_ms', 'gain_mode', 'gain_db', 'noise_reduction_enabled', 'noise_reduction_strength', 'squelch_enabled', 'squelch_offset_db'];
export const hasOverrides = (b) => OVERRIDES.some((k) => b[k] !== null && b[k] !== undefined);
export const clearOverrides = (b) => OVERRIDES.forEach((k) => { b[k] = null; });

// The id is made here, not by the server: a schedule entry refers to the band right away.
export function newBand(fields) {
  return { id: Math.random().toString(36).slice(2, 10), enabled: true, modulation: 'AM', weight: 0, label: '',
    trigger_level_dbfs: null, tuning_time_ms: null, linger_time_ms: null,
    gain_mode: null, gain_db: null, noise_reduction_enabled: null, noise_reduction_strength: null, ...fields };
}

export function bandRange(b) {
  if (b.start_hz === b.stop_hz) return `${fmtMHz(b.start_hz, 4)} MHz · ${modLabel(b.modulation)}`;
  return `${fmtMHz(b.start_hz, 3)} – ${fmtMHz(b.stop_hz, 3)} MHz · ${(b.step_hz / 1000).toFixed(b.step_hz % 1000 ? 3 : 0)} kHz · ${modLabel(b.modulation)}${b.weight ? ` · W ${b.weight}` : ''}`;
}

export function presetPicker(onAdd) {
  const presets = S.settings.presets;
  return select([['', t('Add a band preset…')], ...presets.map((p) => [p.id, t(p.label)])], '', (id) => {
    const p = presets.find((x) => x.id === id);
    if (p) onAdd(newBand({ start_hz: p.start_hz, stop_hz: p.stop_hz, step_hz: p.step_hz, label: t(p.label), modulation: p.modulation }));
  });
}

export async function askCustomRange() {
  const v = await formDialog(t('Custom range'), [
    { key: 'label', label: t('Name'), value: '' },
    { key: 'start', label: t('Start (MHz)'), type: 'number', value: 118 },
    { key: 'stop', label: t('Stop (MHz)'), type: 'number', value: 137 },
    { key: 'step', label: t('Step (kHz)'), type: 'number', value: 25 },
    { key: 'mod', label: t('Modulation'), type: 'select', value: 'AM', options: modOptions() },
  ], t('Add'));
  if (!v || !(v.stop > v.start) || !(v.step > 0)) return null;
  return newBand({ label: v.label || t('Custom range'), start_hz: Math.round(v.start * 1e6), stop_hz: Math.round(v.stop * 1e6), step_hz: Math.round(v.step * 1e3), modulation: v.mod });
}

export async function askSingleFrequency() {
  const v = await formDialog(t('Single frequency'), [
    { key: 'freq', label: t('Frequency (MHz)'), type: 'number', value: 121.5, hint: t('Watched alongside the bands, e.g. 121.500 MHz guard') },
    { key: 'label', label: t('Name'), value: '' },
    { key: 'mod', label: t('Modulation'), type: 'select', value: 'AM', options: modOptions() },
  ], t('Add'));
  if (!v || !(v.freq > 0)) return null;
  const hz = carrierHz(v.freq);
  return newBand({ label: v.label || `${fmtChannel(hz)} MHz`, start_hz: hz, stop_hz: hz, step_hz: 25000, modulation: v.mod });
}

// Name/range/modulation/weight plus the per-band overrides of the global settings.
export function bandEditor(b, save, rerender) {
  const single = b.start_hz === b.stop_hz;
  const num = (label, value, onchange, attrs = {}) => h('label.field', label, h('input', { type: 'number', value, step: 'any', ...attrs, onchange: (e) => { onchange(+e.target.value); save(); } }));
  const fields = h('div.band-edit',
    h('label.field', t('Name'), h('input', { value: b.label, onchange: (e) => { b.label = e.target.value; save(); rerender(); } })),
    num(single ? t('Frequency (MHz)') : t('Start (MHz)'), (b.start_hz / 1e6).toFixed(4), (v) => { b.start_hz = Math.round(v * 1e6); if (single) b.stop_hz = b.start_hz; }),
    single ? null : num(t('Stop (MHz)'), (b.stop_hz / 1e6).toFixed(4), (v) => { b.stop_hz = Math.round(v * 1e6); }),
    single ? null : num(t('Step (kHz)'), b.step_hz / 1000, (v) => { b.step_hz = Math.round(v * 1000); }),
    h('label.field', t('Modulation'), select(modOptions(), b.modulation, (v) => { b.modulation = v; save(); })),
    num(t('Priority weight'), b.weight, (v) => { b.weight = v; }, { min: 0, max: 20, step: 0.5, title: t('0 = normal. Higher = visited more often than other weighted bands (1 vs 2 = twice as often).') }));
  // Per-band overrides: unticked = follow the global setting. Values that
  // only make sense together (on/off + strength, mode + gain) share one tile.
  const c = cfg();
  const defaults = {
    trigger_level_dbfs: c.trigger_level_dbfs, tuning_time_ms: c.timing.tuning_time_ms, linger_time_ms: c.timing.linger_time_ms,
    gain_mode: c.device.gain_mode, gain_db: c.device.gain_db,
    noise_reduction_enabled: c.noise_reduction.enabled, noise_reduction_strength: c.noise_reduction.strength,
    squelch_enabled: c.squelch_enabled, squelch_offset_db: c.squelch_offset_db,
  };
  const ov = (keys, label, makeInput, globalValue) => {
    const own = () => keys.some((k) => b[k] !== null && b[k] !== undefined);
    const holder = h('div.col', { style: { gap: '8px' } });
    const draw = () => mount(holder, own() ? makeInput(draw) : h('span.faint.small', t('Global: {v}', { v: globalValue })));
    draw();
    return h('div.override',
      h('label.switch', h('input', {
        type: 'checkbox', checked: own(), title: t('Own value for this band'),
        onchange: (e) => { keys.forEach((k) => { b[k] = e.target.checked ? defaults[k] : null; }); save(); draw(); },
      }), h('span.label', label)),
      holder);
  };
  const rangeInput = (key, min, max, step, fmt, scale = 1) => slider({ name: '', min, max, step, value: (b[key] ?? defaults[key]) * scale, format: fmt, oninput: (v) => { b[key] = v / scale; save(); } });
  const gainModes = [['manual', t('Manual')], ['auto', t('Auto (hardware)')], ['sw_auto', t('Auto (software)')]];
  const overrides = h('div.overrides',
    ov(['trigger_level_dbfs'], t('Trigger level'), () => rangeInput('trigger_level_dbfs', -90, 0, 1, (v) => `${v} dBFS`), `${c.trigger_level_dbfs} dBFS`),
    ov(['squelch_enabled', 'squelch_offset_db'], t('Squelch'), (redraw) => {
      const on = b.squelch_enabled ?? defaults.squelch_enabled;
      const offset = rangeInput('squelch_offset_db', -40, 0, 1, (v) => `${v} dB`);
      offset.input.disabled = !on;
      return [toggle(t('On'), on, (v) => { b.squelch_enabled = v; save(); redraw(); }), offset];
    }, c.squelch_enabled ? `${t('On')}, ${c.squelch_offset_db} dB` : t('Off')),
    ov(['tuning_time_ms'], t('Tuning time'), () => rangeInput('tuning_time_ms', 0, 1000, 10, (v) => `${v} ms`), `${c.timing.tuning_time_ms} ms`),
    ov(['linger_time_ms'], t('Hold after a transmission'), () => rangeInput('linger_time_ms', 0, 30000, 100, (v) => `${(v / 1000).toFixed(1)} s`), `${(c.timing.linger_time_ms / 1000).toFixed(1)} s`),
    ov(['gain_mode', 'gain_db'], t('Gain'), (redraw) => [
      select(gainModes, b.gain_mode ?? defaults.gain_mode, (v) => { b.gain_mode = v; save(); redraw(); }),
      (b.gain_mode ?? defaults.gain_mode) === 'auto' ? null : rangeInput('gain_db', 0, 50, 0.1, (v) => `${(+v).toFixed(1)} dB`),
    ], `${gainModes.find(([k]) => k === c.device.gain_mode)?.[1] ?? c.device.gain_mode}, ${c.device.gain_db} dB`),
    ov(['noise_reduction_enabled', 'noise_reduction_strength'], t('Noise reduction'), (redraw) => {
      const on = b.noise_reduction_enabled ?? defaults.noise_reduction_enabled;
      const strength = rangeInput('noise_reduction_strength', 0, 100, 1, (v) => `${v}%`, 100);
      strength.input.disabled = !on;
      return [toggle(t('On'), on, (v) => { b.noise_reduction_enabled = v; save(); redraw(); }), strength];
    }, c.noise_reduction.enabled ? `${t('On')}, ${Math.round(c.noise_reduction.strength * 100)}%` : t('Off')));
  return h('div.col', fields,
    h('div.row.wrap', h('h3', t('Own settings for this band')), h('span.spacer'),
      hasOverrides(b) ? h('button.ghost.small', { onclick: () => { clearOverrides(b); save(); rerender(); } }, icon('refresh'), t('Restore general settings')) : null),
    overrides);
}
