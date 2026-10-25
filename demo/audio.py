"""Sounds of the online demo: synthetic voice traffic (espeak-ng) and a music
loop synthesized here, so nothing in the demo has a third-party copyright.
Writes mono PCM16 at the app's audio rate plus clips.json; the service worker
adds the noise and plays them as transmissions."""
from __future__ import annotations

import json
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

RATE = 12500

# Fictional stations and callsigns only. One conversation = lines on one
# frequency, a speaker each; the worker plays them with short gaps.
CONVERSATIONS = {
    "tower": ("en", [
        ("en-gb+m3", "Kestrel five two, Falcon Tower, wind two seven zero degrees, eight knots, runway two seven, cleared to land."),
        ("en-us+m7", "Cleared to land runway two seven, Kestrel five two."),
        ("en-us+m2", "Falcon Tower, Bluejay three zero seven, holding point runway two seven, ready for departure."),
        ("en-gb+m3", "Bluejay three zero seven, line up and wait, runway two seven."),
        ("en-us+m2", "Line up and wait, runway two seven, Bluejay three zero seven."),
    ]),
    "approach": ("en", [
        ("en-029+m4", "Falcon Approach, Osprey one eight, passing flight level one two zero, descending flight level eight zero, information Bravo."),
        ("en-gb-x-rp+f2", "Osprey one eight, Falcon Approach, radar contact. Descend to altitude five thousand feet, QNH one zero one three."),
        ("en-029+m4", "Descend five thousand feet, QNH one zero one three, Osprey one eight."),
    ]),
    "radar": ("en", [
        ("en-gb-scotland+m1", "Kestrel two one, Falcon Radar, climb flight level three four zero, proceed direct DEMOX."),
        ("en-us+m5", "Climb flight level three four zero, direct DEMOX, Kestrel two one."),
    ]),
    "pmr3": ("pl", [
        ("pl+f2", "Marek, tu Kasia, odbiór."),
        ("pl+m3", "Słyszę cię, Kasia. Jesteśmy przy schronisku, za dziesięć minut schodzimy. Odbiór."),
        ("pl+f2", "Dobra, czekamy na parkingu. Bez odbioru."),
    ]),
    "pmr7": ("pl", [
        ("pl+m1", "Ekipa dwa, sprawdzenie łączności. Jak słychać?"),
        ("pl+m6", "Słychać dobrze, na piątkę."),
    ]),
    "ham": ("pl", [
        ("pl+m2", "CQ, CQ, tu SP zero DEMO, SP zero DEMO, wywołuję i czekam na odpowiedź."),
        ("pl+m4", "SP zero DEMO, tu SQ zero TEST. Raport pięć dziewięć, operator Adam. Odbiór."),
    ]),
}


def _speak(voice: str, text: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.wav"
        subprocess.run(["espeak-ng", "-v", voice, "-s", "165", "-w", str(path), text], check=True)
        with wave.open(str(path)) as w:
            rate = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    g = np.gcd(RATE, rate)
    return resample_poly(x, RATE // g, rate // g)


def _radio(x: np.ndarray, am: bool) -> np.ndarray:
    """Voice through a radio: narrow band, a bit of compression, leading/trailing carrier silence."""
    x = sosfilt(butter(4, [300, 2700 if am else 3000], "bandpass", fs=RATE, output="sos"), x)
    x = np.tanh(3 * x / (np.max(np.abs(x)) + 1e-9)) * 0.7
    pad = np.zeros(int(0.25 * RATE), np.float32)
    return np.concatenate([pad, x, pad]).astype(np.float32)


def _music(seconds_per_bar: float = 2.4, bars: int = 16) -> np.ndarray:
    """A seamless loop: pads, bass, a plucked arpeggio and a light beat (Am F C G)."""
    n = int(seconds_per_bar * bars * RATE)
    t = np.arange(n) / RATE
    out = np.zeros(n)
    chords = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]
    hz = lambda m: 440 * 2 ** ((m - 69) / 12)
    beat = seconds_per_bar / 4
    rng = np.random.default_rng(7)
    for bar in range(bars):
        notes = chords[bar % 4]
        s, e = int(bar * seconds_per_bar * RATE), int((bar + 1) * seconds_per_bar * RATE)
        tt = t[s:e] - t[s]
        env = np.minimum(1, tt / 0.3) * np.minimum(1, (seconds_per_bar - tt) / 0.3)
        for m in notes:  # pad: two detuned sines + an octave
            f = hz(m)
            out[s:e] += 0.07 * env * (np.sin(2 * np.pi * f * tt) + np.sin(2 * np.pi * f * 1.004 * tt) + 0.3 * np.sin(4 * np.pi * f * tt))
        for k in range(8):  # arpeggio, eighth notes
            a = s + int(k * beat / 2 * RATE)
            length = int(0.5 * RATE)
            m = notes[[0, 1, 2, 1][k % 4]] + 12 + (12 if k == 6 else 0)
            seg = np.arange(min(length, n - a)) / RATE
            out[a:a + seg.size] += 0.16 * np.exp(-seg * 7) * np.sin(2 * np.pi * hz(m) * seg) * (1 + 0.3 * np.sin(2 * np.pi * hz(m) * 2 * seg))
        for k in range(4):  # bass + kick on the beat, hat off the beat
            a = s + int(k * beat * RATE)
            seg = np.arange(int(beat * RATE)) / RATE
            out[a:a + seg.size] += 0.22 * np.exp(-seg * 3) * np.sin(2 * np.pi * hz(notes[0] - 24) * seg)
            kick = np.arange(int(0.15 * RATE)) / RATE
            out[a:a + kick.size] += 0.35 * np.exp(-kick * 30) * np.sin(2 * np.pi * (50 + 90 * np.exp(-kick * 40)) * kick)
            h = a + int(beat / 2 * RATE)
            hat = int(0.04 * RATE)
            if h + hat <= n:
                out[h:h + hat] += 0.05 * rng.standard_normal(hat) * np.exp(-np.arange(hat) / RATE * 80)
    out = sosfilt(butter(2, 5500, fs=RATE, output="sos"), out)
    return (0.8 * out / np.max(np.abs(out))).astype(np.float32)


def _write(path: Path, x: np.ndarray) -> None:
    path.write_bytes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def build(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    clips = {}
    for conv, (lang, lines) in CONVERSATIONS.items():
        am = lang == "en"  # the English ones are airband
        for i, (voice, text) in enumerate(lines):
            cid = f"{conv}{i}"
            x = _radio(_speak(voice, text), am)
            _write(out / f"{cid}.pcm", x)
            clips[cid] = {"conv": conv, "text": text, "lang": lang, "samples": int(x.size)}
    music = _music()
    _write(out / "music.pcm", music)
    clips["music"] = {"conv": "music", "text": "", "lang": "", "samples": int(music.size)}
    (out / "clips.json").write_text(json.dumps({"rate": RATE, "clips": clips}, indent=1))


if __name__ == "__main__":
    import sys
    build(Path(sys.argv[1]))
