"""Builds the online demo (falconsdr.com) into demo/dist: the app's own web UI,
unchanged, plus a service worker (site/sw.js) that answers /api/* with a
simulated receiver. Needs espeak-ng and the repo's .venv (numpy, scipy).

    .venv/bin/python demo/build.py && demo/deploy.sh
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
DIST = DEMO / "dist"

# The user's own presets and config must not leak into the published fixtures.
os.environ["HOME"] = os.environ["USERPROFILE"] = tempfile.mkdtemp()
sys.path.insert(0, str(ROOT))

from falconsdr import __version__, i18n, military_presets  # noqa: E402
from falconsdr.config import AppConfig  # noqa: E402
from falconsdr.controller import channel_dict  # noqa: E402
from falconsdr.addons import aprs as aprs_mod, sensors  # noqa: E402
from falconsdr.addons.external import parse_rtl_433  # noqa: E402
from falconsdr.addons.manager import AddonManager, summarize  # noqa: E402

sys.path.insert(0, str(DEMO))
import audio  # noqa: E402


# Fictional stations for the simulated add-ons. Everything is passed through the program's own parsers, so an event has
# exactly the fields a real one has; sw.js only varies the values over time.
_SENSORS = [   # (rtl_433 JSON line, {field: [base, daily swing, noise, decimals]}, seconds between transmissions)
    ({"model": "Acurite-Tower", "id": 11202, "channel": "A", "battery_ok": 1, "temperature_C": 6.8, "humidity": 71}, {"temperature_C": [6.5, 4.5, 0.2, 1], "humidity": [72, -14, 1.5, 0]}, 18),
    ({"model": "LaCrosse-TX141THBv2", "id": 53, "channel": 1, "battery_ok": 1, "temperature_C": 21.4, "humidity": 46}, {"temperature_C": [21.3, 0.8, 0.1, 1], "humidity": [46, 3, 1, 0]}, 31),
    ({"model": "Fineoffset-WH1080", "id": 23, "battery_ok": 1, "temperature_C": 6.2, "humidity": 78, "wind_avg_km_h": 11.3, "wind_max_km_h": 19.8, "wind_dir_deg": 230, "rain_mm": 148.2},
     {"temperature_C": [6.0, 4.5, 0.2, 1], "humidity": [78, -12, 1.5, 0], "wind_avg_km_h": [10, 6, 3, 1], "wind_max_km_h": [18, 8, 4, 1], "wind_dir_deg": [230, 60, 25, 0]}, 48),
    ({"model": "Toyota", "type": "TPMS", "id": "b3a1f0c2", "status": 128, "pressure_kPa": 231.5, "temperature_C": 12, "mic": "CRC"}, {"pressure_kPa": [231, 3, 0.6, 1], "temperature_C": [12, 5, 0.5, 0]}, 97),
    ({"model": "Honeywell-Security", "id": 88213, "battery_ok": 1, "state": "closed", "alarm": 0, "tamper": 0, "event": "heartbeat"}, {"state": ["closed", "closed", "closed", "closed", "open"]}, 64),
    ({"model": "CurrentCost-TX", "id": 3052, "power0_W": 412, "power1_W": 38}, {"power0_W": [380, 160, 70, 0], "power1_W": [38, 8, 4, 0]}, 15),
    ({"model": "Fineoffset-WH51", "id": "0ab3c1", "battery_mV": 1520, "moisture": 38}, {"moisture": [38, 0, 1.5, 0]}, 70),
    ({"model": "Oil-SonicStd", "id": 4410, "depth_cm": 112}, {"depth_cm": [112, 0, 0.6, 0]}, 120),
    ({"model": "Nexus-TH", "id": 152, "channel": 2, "battery_ok": 0, "temperature_C": 18.2, "humidity": 52}, {"temperature_C": [18, 1.2, 0.15, 1], "humidity": [52, 6, 1.2, 0]}, 59),
]

_APRS = [   # (source, destination, path, information field)
    ("SP9XQA-9", "APRS", ["WIDE1-1", "WIDE2-1*"], b"!4953.12N/02158.40E>118/045 Mobile"),
    ("SP9XQB", "APRX28", ["SR9WXY*"], b"=4947.50N/02201.20E-Home station, 144.800 MHz"),
    ("SR9WXY", "APRS", ["WIDE2-1*"], b"!4950.05N/02152.30E#Digipeater Zagorz"),
    ("SP9XQC-13", "APRS", ["SR9WXY*"], b"@251423z4955.20N/02130.10E_090/003g005t048r000p000P000h85b10132"),
    ("SQ9XQD-7", "APRS", ["WIDE1-1"], b">Mobile, listening on 144.800"),
    ("SP9XQE", "APRS", ["SR9WXY*"], b":SP9XQB   :Test from the field{12"),
    ("SP9XQF-5", "APRS", ["WIDE1-1", "WIDE2-1*"], b"!5001.45N/02118.22E>200/032 Bieszczady trip"),
    ("SP9XQG", "APRS", ["SR9WXY*"], b";LEADER   *092345z4953.12N/02102.55E>Hike group"),
]


def addon_fixtures() -> dict:
    sensor_devices = []
    for raw, gen, every in _SENSORS:
        event, _ = parse_rtl_433(json.dumps(raw))
        sensor_devices.append({"event": event, "gen": gen, "every": every})
    aprs_stations = []
    for source, dest, path, info in _APRS:
        event = {"kind": "aprs", "source": source, "dest": dest, "path": path, "pid": 240, "raw": info.decode("latin-1")}
        event.update(aprs_mod.parse_aprs(dest, info))
        aprs_stations.append({"event": event, "summary": summarize(event)})
    return {"status": AddonManager(AppConfig()).status(), "sensors": sensor_devices, "aprs": aprs_stations, "metrics": sensors.metrics()}


def fixtures() -> dict:
    config = asdict(AppConfig())
    # The default points into this build's scratch HOME; show the demo's fake filesystem instead.
    config["recording"]["output_dir"] = "/home/demo/FalconSDR/recordings"
    return {
        "version": __version__,
        "addons": addon_fixtures(),
        "config": config,
        "presets": [{**p.to_dict(), "user": False} for p in military_presets.get_all_presets()],
        "curated": {
            "military": [channel_dict(c) for c in military_presets.MILITARY_CHANNELS],
            "fire": [channel_dict(c) for c in military_presets.FIRE_CHANNELS],
        },
        "languages": [{"code": c, "name": i18n.LANGUAGE_NAMES.get(c, c)} for c in i18n.available_languages()],
        "i18n": {c: i18n.catalog_for(c) for c in i18n.available_languages()},
    }


def main() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.copytree(ROOT / "falconsdr" / "web" / "static", DIST)
    shutil.copytree(DEMO / "site", DIST, dirs_exist_ok=True)
    shutil.copy(ROOT / "LICENSE", DIST / "LICENSE.txt")  # GPL: the licence travels with the code we serve
    audio.build(DIST / "demo" / "audio")
    (DIST / "demo" / "fixtures.json").write_text(json.dumps(fixtures(), ensure_ascii=False))
    print(f"demo {__version__} -> {DIST}")


if __name__ == "__main__":
    main()
