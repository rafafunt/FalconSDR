"""Sounds of the online demo: synthetic voice traffic (Piper TTS) and a music
loop synthesized here, so nothing in the demo is anyone else's recording.
Writes mono PCM16 at the app's audio rate plus clips.json; the service worker
adds the noise and plays them as transmissions.

Piper runs from its own venv (demo/.venv: `python3 -m venv demo/.venv &&
demo/.venv/bin/pip install piper-tts`); the voices are downloaded once into
demo/voices. Voice licenses: VCTK and LibriTTS-R are CC BY 4.0 (credited in
the demo's About dialog), the Polish ones CC0."""
from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.request
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

RATE = 12500
DEMO = Path(__file__).resolve().parent
PIPER = DEMO / ".venv" / "bin" / "python"
VOICE_DIR = DEMO / "voices"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
VOICES = {
    "vctk": "en/en_GB/vctk/medium/en_GB-vctk-medium",
    "libritts": "en/en_US/libritts_r/medium/en_US-libritts_r-medium",
    "gosia": "pl/pl_PL/gosia/medium/pl_PL-gosia-medium",
    "darkman": "pl/pl_PL/darkman/medium/pl_PL-darkman-medium",
    "mc": "pl/pl_PL/mc_speech/medium/pl_PL-mc_speech-medium",
}

# Fictional stations and callsigns only. A conversation = lines on one
# frequency, one speaker each ("voice:speaker id"), per UI language; airband
# is English everywhere, like the real thing.
CONVERSATIONS = {
    "tower": {"en": [
        ("vctk:95", "Kestrel five two, Falcon Tower, wind two seven zero degrees, eight knots, runway two seven, cleared to land."),
        ("libritts:20", "Cleared to land runway two seven, Kestrel five two."),
        ("libritts:120", "Falcon Tower, Bluejay three zero seven, holding point runway two seven, ready for departure."),
        ("vctk:95", "Bluejay three zero seven, line up and wait, runway two seven."),
        ("libritts:120", "Line up and wait, runway two seven, Bluejay three zero seven."),
    ]},
    "approach": {"en": [
        ("vctk:97", "Falcon Approach, Osprey one eight, passing flight level one two zero, descending flight level eight zero, information Bravo."),
        ("vctk:107", "Osprey one eight, Falcon Approach, radar contact. Descend to altitude five thousand feet, QNH one zero one three."),
        ("vctk:97", "Descend five thousand feet, QNH one zero one three, Osprey one eight."),
    ]},
    "radar": {"en": [
        ("vctk:102", "Kestrel two one, Falcon Radar, climb flight level three four zero, proceed direct DEMOX."),
        ("libritts:300", "Climb flight level three four zero, direct DEMOX, Kestrel two one."),
    ]},
    "pmr3": {
        "pl": [
            ("gosia:0", "Marek, tu Kasia, odbiór."),
            ("darkman:0", "Słyszę cię, Kasia. Jesteśmy przy schronisku, za dziesięć minut schodzimy. Odbiór."),
            ("gosia:0", "Dobra, czekamy na parkingu. Bez odbioru."),
        ],
        "en": [
            ("vctk:90", "Mark, this is Kate, over."),
            ("vctk:82", "Hear you, Kate. We're at the mountain hut, coming down in ten minutes. Over."),
            ("vctk:90", "Okay, we'll wait at the car park. Out."),
        ],
    },
    "pmr7": {
        "pl": [
            ("mc:0", "Ekipa dwa, sprawdzenie łączności. Jak słychać?"),
            ("darkman:0", "Słychać dobrze, na piątkę."),
        ],
        "en": [
            ("vctk:60", "Team two, radio check. How do you read?"),
            ("vctk:81", "Reading you five by five."),
        ],
    },
    "milvhf": {"en": [
        ("vctk:98", "Range Control, Hawk one one, flight of two, entering area Delta, request block four thousand to six thousand."),
        ("libritts:700", "Hawk one one, Range Control, area Delta is active, block four thousand to six thousand approved, report leaving."),
        ("vctk:98", "Block four to six approved, wilco, Hawk one one."),
    ]},
    "miluhf": {"en": [
        ("libritts:40", "Viper flight, check."),
        ("libritts:220", "Two."),
        ("vctk:107", "Viper one one, Falcon Ops, tanker Shell five one on station, track Alpha, flight level two two zero."),
        ("libritts:40", "Falcon Ops, Viper one one, copy, proceeding track Alpha."),
    ]},
    "marine16": {"en": [
        ("vctk:93", "Sea Falcon, Sea Falcon, this is Morning Star, Morning Star, over."),
        ("vctk:81", "Morning Star, this is Sea Falcon, go to channel seven two, over."),
        ("vctk:93", "Seven two, Morning Star."),
    ]},
    "marine72": {"en": [
        ("vctk:93", "Sea Falcon, Morning Star on seven two. We are anchored by the north buoy, can we meet at eighteen hundred? Over."),
        ("vctk:81", "Copy, eighteen hundred at the north buoy. Sea Falcon, out."),
    ]},
    "rpt": {
        "pl": [
            ("darkman:0", "SP zero KOT przez SR zero DEMO, czy ktoś jest na przemienniku? Odbiór."),
            ("gosia:0", "SP zero KOT, tu SQ zero LIS. Słyszę cię dobrze przez przemiennik. Jak u ciebie pogoda? Odbiór."),
            ("darkman:0", "Pada od rana, ale antena wytrzymała. Dzięki za łączność, siedemdziesiąt trzy. SP zero KOT."),
        ],
        "en": [
            ("libritts:500", "Sierra Papa Zero Kilo Oscar Tango via the demo repeater, anyone around? Over."),
            ("vctk:107", "Kilo Oscar Tango, this is Sierra Quebec Zero Lima India Sierra, you're full quieting into the repeater. How's the weather there? Over."),
            ("libritts:500", "Raining all morning, but the antenna held up. Thanks for the contact, seventy three."),
        ],
    },
    "ham": {
        "pl": [
            ("darkman:0", "CQ, CQ, tu SP zero DEMO, SP zero DEMO, wywołuję i czekam na odpowiedź."),
            ("mc:0", "SP zero DEMO, tu SQ zero TEST. Raport pięć dziewięć, operator Adam. Odbiór."),
        ],
        "en": [
            ("vctk:93", "CQ, CQ, this is Sierra Papa Zero Delta Echo Mike Oscar, calling CQ and standing by."),
            ("libritts:500", "Sierra Papa Zero Delta Echo Mike Oscar, this is Sierra Quebec Zero Test. You're five nine, name here is Adam. Over."),
        ],
    },
}

TALK = {
    "en": "You're listening to Falcon FM, one oh three point seven. Time for the weather. A dry and sunny afternoon with a light westerly wind and highs of eighteen degrees. Tonight, clear skies and a few patches of fog by the river. Traffic is moving freely on the ring road. Stay with us, more music after the news.",
    "pl": "Słuchasz Falcon FM, sto trzy i siedem. Czas na pogodę. Po południu sucho i słonecznie, słaby wiatr z zachodu, do osiemnastu stopni. W nocy bezchmurnie, nad rzeką miejscami mgła. Na obwodnicy ruch płynny, bez utrudnień. Zostańcie z nami, po wiadomościach więcej muzyki.",
}


def _model(name: str) -> Path:
    path = VOICE_DIR / (VOICES[name].rsplit("/", 1)[1] + ".onnx")
    for suffix in ("", ".json"):
        target = path.with_name(path.name + suffix)
        if not target.exists():
            VOICE_DIR.mkdir(exist_ok=True)
            urllib.request.urlretrieve(VOICE_URL + VOICES[name] + ".onnx" + suffix, target)
    return path


def _speak(voice: str, text: str) -> np.ndarray:
    name, speaker = voice.split(":")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.wav"
        subprocess.run([str(PIPER), "-m", "piper", "-m", str(_model(name)), "-s", speaker, "-f", str(path)],
                       input=text.encode(), check=True, capture_output=True)
        with wave.open(str(path)) as w:
            rate = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    g = np.gcd(RATE, rate)
    return resample_poly(x, RATE // g, rate // g)


def _radio(x: np.ndarray, am: bool) -> np.ndarray:
    """Voice through a radio: band-limited, gently compressed, a little carrier before and after."""
    x = sosfilt(butter(4, [250 if am else 300, 3000 if am else 3400], "bandpass", fs=RATE, output="sos"), x)
    x = np.tanh(1.5 * x / (np.max(np.abs(x)) + 1e-9)) / np.tanh(1.5) * 0.7
    pad = np.zeros(int(0.25 * RATE), np.float32)
    return np.concatenate([pad, x, pad]).astype(np.float32)


def _music(seconds_per_bar: float = 2.4, bars: int = 16, chords=((57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62))) -> np.ndarray:
    """A seamless loop: pads, bass, a plucked arpeggio and a light beat (Am F C G by default)."""
    n = int(seconds_per_bar * bars * RATE)
    t = np.arange(n) / RATE
    out = np.zeros(n)
    hz = lambda m: 440 * 2 ** ((m - 69) / 12)
    beat = seconds_per_bar / 4
    rng = np.random.default_rng(7)
    for bar in range(bars):
        notes = chords[bar % len(chords)]
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
    for conv, variants in CONVERSATIONS.items():
        am = conv in ("tower", "approach", "radar", "milvhf", "miluhf")
        for lang, lines in variants.items():
            for i, (voice, text) in enumerate(lines):
                cid = f"{conv}_{lang}{i}"
                x = _radio(_speak(voice, text), am)
                if conv == "rpt":  # the repeater's courtesy beep after each over
                    tt = np.arange(int(0.18 * RATE)) / RATE
                    x = np.concatenate([x[:-int(0.1 * RATE)], 0.35 * np.sin(2 * np.pi * 1000 * tt), np.zeros(int(0.15 * RATE))]).astype(np.float32)
                _write(out / f"{cid}.pcm", x)
                clips[cid] = {"conv": conv, "text": text, "lang": lang, "samples": int(x.size)}
    for lang, text in TALK.items():  # second FM station: jingle, then the weather over a music bed
        voice = _speak("vctk:107" if lang == "en" else "gosia:0", text)
        voice = 0.75 * voice / np.max(np.abs(voice))
        n = int(1.6 * RATE) + voice.size + int(20 * RATE)  # then the music comes up
        bed = _music(2.0, int(np.ceil(n / RATE / 2.0)), ((60, 64, 67), (57, 60, 64), (53, 57, 60), (55, 59, 62)))[:n] * 0.22
        tt = np.arange(int(0.35 * RATE)) / RATE
        for k, m in enumerate((72, 76, 79)):
            a = int(k * 0.3 * RATE)
            bed[a:a + tt.size] += 0.35 * np.exp(-tt * 6) * np.sin(2 * np.pi * 440 * 2 ** ((m - 69) / 12) * tt)
        end = int(1.6 * RATE) + voice.size
        bed[end:] *= np.minimum(3.6, 1 + np.arange(n - end) / (0.8 * RATE))  # bed 0.22 -> 0.8 in 2 s
        bed[int(1.6 * RATE):end] += voice
        x = (0.85 * bed / np.max(np.abs(bed))).astype(np.float32)
        _write(out / f"talk_{lang}.pcm", x)
        clips[f"talk_{lang}"] = {"conv": "talk", "text": text, "lang": lang, "samples": int(x.size)}
    music = _music()
    _write(out / "music.pcm", music)
    clips["music"] = {"conv": "music", "text": "", "lang": "", "samples": int(music.size)}
    (out / "clips.json").write_text(json.dumps({"rate": RATE, "clips": clips}, indent=1))


if __name__ == "__main__":
    import sys
    build(Path(sys.argv[1]))
