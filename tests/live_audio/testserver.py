"""Local test server for live-audio.js: serves a page that starts liveAudio at once, the
chosen live-audio.js, and /api/audio = a continuous 440 Hz tone in 30 ms PCM16 chunks with
injected stalls. Never touches the receiver.

usage: testserver.py PORT LIVE_AUDIO_JS SCENARIO LOGFILE
scenarios: clean | stall_catchup (220 ms stall every 6 s, then the missed chunks at once,
like the engine's queue) | stall_loss (same stall, missed audio never sent) | drift (1.5 %
less audio than real time, like the lost IQ) | mix (stall_catchup + drift + jitter)
"""
import json
import math
import random
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, JS, SCENARIO, LOG = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
RATE, CHUNK = 12500, 375
PAGE = b"""<!doctype html><meta charset=utf-8><title>live audio test</title>
<script type=module>
import { liveAudio } from '/js/live-audio.js';
liveAudio.start().then(() => console.log('LIVEAUDIO started'), (e) => console.log('LIVEAUDIO failed ' + e));
</script>"""
streams = {"opened": 0, "open": 0}
lock = threading.Lock()


def log(**kv):
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": time.time(), **kv}) + "\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/audio"):
            return self.audio()
        if self.path.startswith("/js/live-audio.js"):
            body, ctype = open(JS, "rb").read(), "text/javascript"
        else:
            body, ctype = PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def audio(self):
        with lock:
            streams["opened"] += 1
            streams["open"] += 1
            log(event="open", **streams)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Sample-Rate", str(RATE))
        self.end_headers()
        n, phase, t_next, start = 0, 0.0, time.monotonic(), time.monotonic()
        try:
            while True:
                pcm = []
                for _ in range(CHUNK):
                    pcm.append(int(16000 * math.sin(phase)))
                    phase += 2 * math.pi * 440 / RATE
                data = struct.pack("<%dh" % CHUNK, *pcm)
                every = 1333 if SCENARIO == "real" else 200
                stall = SCENARIO in ("stall_catchup", "stall_loss", "mix", "real") and n > 0 and n % every == 0
                if stall:
                    time.sleep(0.22)
                    log(event="stall", chunk=n)
                    if SCENARIO in ("stall_loss", "real"):  # the stalled time's audio is gone: skip it
                        t_next += 0.22
                        phase += 2 * math.pi * 440 * 0.22  # content jumps, like lost IQ
                self.wfile.write(data)
                self.wfile.flush()
                n += 1
                step = CHUNK / RATE
                if SCENARIO in ("drift", "mix"):
                    step *= 1.015
                if SCENARIO == "real":
                    step *= 1.009  # 0.9 % less audio than wall time, as measured after the read fix
                t_next += step
                jitter = random.uniform(0, 0.02) if SCENARIO in ("mix", "real") else 0.0
                delay = t_next - time.monotonic() + jitter
                if delay > 0:
                    time.sleep(delay)  # behind schedule (after a stall): no sleep = catch-up burst
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with lock:
                streams["open"] -= 1
                log(event="close", **streams)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
