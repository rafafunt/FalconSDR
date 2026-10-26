// App-wide state: server settings (config, Memory List, blacklist, presets...),
// the live scanner snapshot (polled), and tiny pub/sub so views can react.
import { api, debounce, errorToast, lang, modal, setCh833, t } from './core.js';

export const S = {
  settings: null,   // GET /api/settings
  live: null,       // GET /api/live
  log: [],
  logId: 0,
};

const listeners = {};
export function on(event, fn) { (listeners[event] ||= new Set()).add(fn); return () => listeners[event].delete(fn); }
export function emit(event, data) { (listeners[event] || []).forEach((fn) => fn(data)); }

export const cfg = () => S.settings.config;

export async function loadSettings() {
  S.settings = await api.get('/api/settings');
  setCh833(S.settings.channels);
  emit('settings');
  return S.settings;
}

// Merge a partial config change locally right away (UI stays snappy), then
// persist; sliders call the debounced variant while dragging.
function mergeLocal(target, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (v && typeof v === 'object' && !Array.isArray(v) && target[k] && typeof target[k] === 'object') mergeLocal(target[k], v);
    else target[k] = v;
  }
}

let pending = {};
const flush = debounce(async () => {
  const patch = pending;
  pending = {};
  try { await api.post('/api/config', patch); } catch (e) { errorToast(e); }
}, 350);

export function patchConfig(patch) {
  mergeLocal(S.settings.config, patch);
  mergeLocal(pending, JSON.parse(JSON.stringify(patch)));
  if (patch.bands) pending.bands = S.settings.config.bands;  // bands always go as a whole list
  emit('config');
  flush();
}

export async function saveChannels(channels) {
  S.settings.channels = channels;
  setCh833(channels);
  emit('channels');
  try { await api.put('/api/channels', channels); } catch (e) { errorToast(e); }
}

export async function saveBlacklist(blacklist) {
  S.settings.blacklist = blacklist;
  emit('blacklist');
  try { await api.put('/api/blacklist', blacklist); } catch (e) { errorToast(e); }
}

export function channelName(freqKhz) {
  const c = S.settings.channels.find((ch) => Math.round(ch.freq_hz / 1000) === freqKhz);
  return c ? c.name : '';
}

// ---------------------------------------------------------------- live polling
let fast = false;
let timer = null;
export function setFastPolling(v) { fast = v; schedule(0); }

async function poll() {
  try {
    const live = await api.get('/api/live?since_log=' + S.logId);
    S.live = live;
    // Two polls can overlap (setFastPolling) - append each entry once.
    const fresh = live.log.filter((e) => e.id > S.logId);
    if (fresh.length) {
      S.log = [...S.log, ...fresh].slice(-100);
      S.logId = fresh[fresh.length - 1].id;
    }
    emit('live', live);
  } catch { /* server restarting - keep trying */ }
  schedule(fast ? 250 : 1500);
}

function schedule(ms) { clearTimeout(timer); timer = setTimeout(poll, ms); }
export function startPolling() { poll(); }

// ---------------------------------------------------------------- legal notices
const LAUNCH_TEXT = () => t('FalconSDR can tune to a wide range of radio frequencies, some of which may be restricted or illegal to listen to or record where you live.\n\nUsing this software does not authorize you to listen to or record any transmission. Radio monitoring and recording laws vary significantly between countries - some places allow listening to most frequencies but restrict recording or sharing what you hear; others restrict listening to non-public or certain public-safety/aviation communications entirely.\n\nYou are solely responsible for knowing and following the laws that apply to you. If you\'re unsure, check your local telecommunications regulations before using the scanning or recording features.');

export async function maybeShowLaunchNotice() {
  const seen = cfg().legal_disclaimer.launch_seen_languages;
  if (seen.includes(lang)) return;
  await modal(t('Before you start'), LAUNCH_TEXT(), [[t('I understand'), true, 'primary']]);
  seen.push(lang);
  api.post('/api/disclaimer', { kind: 'launch', lang }).catch(() => {});
}

// Gate shared by every recording-starting action; false = user declined.
export async function confirmRecording() {
  const seen = cfg().legal_disclaimer.recording_seen_languages;
  if (seen.includes(lang)) return true;
  const ok = await modal(t('Before you record'), t("You're about to record for the first time.\n\nRecording radio transmissions can be more restricted than simply listening to them, depending on your country's laws - even where monitoring itself is legal. Make sure you've checked the rules that apply where you live before continuing."),
    [[t('Cancel'), false, 'ghost'], [t('I understand, continue'), true, 'primary']]);
  if (!ok) return false;
  seen.push(lang);
  await api.post('/api/disclaimer', { kind: 'recording', lang });
  return true;
}
