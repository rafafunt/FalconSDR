"""Silence gaps and waveform jumps in the recorded test output (a 440 Hz tone), plus stream
counts and console lines. usage: analyze.py OUTDIR TAG..."""
import json
import sys
import wave

import numpy as np

out = sys.argv[1]
for tag in sys.argv[2:]:
    w = wave.open(f"{out}/{tag}.wav")
    sr = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64)
    nz = np.nonzero(np.abs(x) > 200)[0]
    if not len(nz):
        print(f"{tag}: NO AUDIO recorded ({len(x) / sr:.1f} s of silence)")
        continue
    x = x[nz[0]:nz[-1]]
    dur = len(x) / sr
    # silence: |x| small for >= 3 ms (the tone itself crosses zero, but never stays there)
    q = (np.abs(x) < 200).astype(np.int8)
    d = np.diff(np.r_[0, q, 0])
    runs = [(e - s) / sr * 1000 for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]) if e - s >= sr * 0.003]
    # jumps: zero-crossing spacing far from a 440 Hz half period (54.5 samples); +-3 % rate changes stay inside
    zc = np.nonzero(np.diff(np.signbit(x)))[0]
    sp = np.diff(zc)
    jumps = int(np.sum((sp < 54.5 * 0.75) | (sp > 54.5 * 1.25)))
    ev = [json.loads(l) for l in open(f"{out}/{tag}.events")] if open(f"{out}/{tag}.events").read() else []
    opened = max((e.get("opened", 0) for e in ev), default=0)
    stalls = sum(1 for e in ev if e["event"] == "stall")
    con = open(f"{out}/{tag}.console").read().strip()
    print(f"{tag}: {dur:.0f} s audio | silences >=3 ms: {len(runs)} (total {sum(runs):.0f} ms, longest {max(runs, default=0):.0f} ms)"
          f" | waveform jumps: {jumps} | server stalls: {stalls} | /api/audio streams opened: {opened}")
    if con:
        print("     console:", con.replace("\n", " || ")[:400])
