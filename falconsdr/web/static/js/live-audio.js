// Live audio: one long fetch() of raw mono PCM16 (X-Sample-Rate header) from
// /api/audio, each chunk scheduled back to back as an AudioBufferSourceNode.
// Works over plain http:// to an IP (no AudioWorklet, which needs HTTPS).
// Keeps ~0.4 s of latency and resyncs after gaps (no transmission = no data). 0.25 s was
// too little to ride out the server's occasional ~220 ms stalls (tested in a browser, gotcha 51).

const LATENCY_S = 0.4;
const MAX_AHEAD_S = 1.2;
// The lead is held near LATENCY_S by playing up to 1 % slower/faster, never by jumping: a resync
// used to fire while the lead was merely low (< 20 ms, audio still playing) and inserted 250 ms of
// silence - after a server hiccup, again and again (measured: 6 such gaps in 4 s at the headphones).
// 1 %, not more: playbackRate shifts the pitch (3 % = a quarter tone, heard as a bent voice for seconds
// after every hiccup; measured with tests/live_audio: max pitch error 1.4 % -> 0.5 %, still 0 silences).
const MAX_RATE_CHANGE = 0.01;

// Listening aids, in the browser so they work for every device (a phone has no louder setting than
// its maximum). Chain: source -> boost -> compressor -> volume. The compressor is a safety net (a loud
// burst after a quiet stretch never reaches the ears at full force) and what makes boost usable.
export const BOOST_STEPS_DB = [0, 6, 12, 18];
const COMPRESSOR = { threshold: -14, knee: 10, ratio: 12, attack: 0.003, release: 0.2 };
const boostDb = () => {
  const v = parseInt(localStorage.getItem('boostDb') ?? '0', 10);
  return BOOST_STEPS_DB.includes(v) ? v : 0;
};

export function makeLoudnessChain(ctx, boostDbValue) {
  const boost = ctx.createGain();
  boost.gain.value = 10 ** (boostDbValue / 20);
  const comp = ctx.createDynamicsCompressor();
  for (const [k, v] of Object.entries(COMPRESSOR)) comp[k].value = v;
  boost.connect(comp);
  return { input: boost, boost, output: comp };
}

export const liveAudio = {
  ctx: null,
  gain: null,
  chain: null,
  boostDb: boostDb(),
  ctrl: null,
  playing: false,
  volume: parseFloat(localStorage.getItem('volume') ?? '0.9'),
  onchange: () => {},
  t: 0,

  async start() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      this.chain = makeLoudnessChain(this.ctx, this.boostDb);
      this.chain.output.connect(this.gain);
      // Through an <audio> element, not straight to the speakers: Android then treats
      // it as media playback - keeps it going with the screen off, shows controls.
      if (this.ctx.createMediaStreamDestination) {
        const dest = this.ctx.createMediaStreamDestination();
        this.gain.connect(dest);
        this.el = new Audio();
        this.el.srcObject = dest.stream;
      } else this.gain.connect(this.ctx.destination);
    }
    if (this.el) this.el.play().catch(() => {});
    await this.ctx.resume();
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: 'FalconSDR', artist: 'Live' });
      navigator.mediaSession.setActionHandler('play', () => this.start());
      navigator.mediaSession.setActionHandler('pause', () => this.stop());
    }
    this.playing = true;
    this.onchange();
    if (!this.ctrl) this._stream();
  },

  stop() {
    this.playing = false;
    if (this.ctrl) { this.ctrl.abort(); this.ctrl = null; }
    if (this.el) this.el.pause();
    this.onchange();
  },

  async toggle() { return this.playing ? this.stop() : this.start(); },

  setVolume(v) {
    this.volume = v;
    localStorage.setItem('volume', String(v));
    if (this.gain) this.gain.gain.value = v;
  },

  setBoost(db) {
    this.boostDb = db;
    localStorage.setItem('boostDb', String(db));
    if (this.chain) this.chain.boost.gain.value = 10 ** (db / 20);
  },

  async _stream() {
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.t = 0;
    let rest = null;
    try {
      const r = await fetch('/api/audio', { signal: ctrl.signal, cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      const rate = +(r.headers.get('X-Sample-Rate') || 12500);
      const reader = r.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        let u = value;
        if (rest) { const m = new Uint8Array(rest.length + u.length); m.set(rest); m.set(u, rest.length); u = m; rest = null; }
        if (u.length % 2) { rest = u.slice(-1); u = u.slice(0, -1); }
        if (!u.length) continue;
        if (u.byteOffset % 2) u = u.slice();
        const i16 = new Int16Array(u.buffer, u.byteOffset, u.length / 2);
        const f = new Float32Array(i16.length);
        for (let i = 0; i < f.length; i++) f[i] = i16[i] / 32768;
        const buf = this.ctx.createBuffer(1, f.length, rate);
        buf.copyToChannel(f, 0);
        const src = this.ctx.createBufferSource();
        src.buffer = buf;
        src.connect(this.chain.input);
        const now = this.ctx.currentTime;
        // Really ran dry (or a new transmission after silence) / far too far ahead: start over.
        if (this.t <= now || this.t > now + MAX_AHEAD_S) this.t = now + LATENCY_S;
        const lead = this.t - now;
        const speed = 1 + Math.max(-MAX_RATE_CHANGE, Math.min(MAX_RATE_CHANGE, (lead - LATENCY_S) * 0.5));
        src.playbackRate.value = speed;
        src.start(this.t);
        this.t += buf.duration / speed;
      }
    } catch (e) {
      if (!ctrl.signal.aborted) console.warn('live audio stream:', e);  // dropped - reconnect below
    }
    // Close this response for good before a reconnect: an error that left it open kept the
    // connection, and every retry took another of the browser's 6 per server - the page froze.
    const dropped = !ctrl.signal.aborted;
    ctrl.abort();
    if (this.ctrl === ctrl) this.ctrl = null;
    if (this.playing && dropped) setTimeout(() => { if (this.playing && !this.ctrl) this._stream(); }, 1000);
  },
};
