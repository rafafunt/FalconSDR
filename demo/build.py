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

sys.path.insert(0, str(DEMO))
import audio  # noqa: E402


def fixtures() -> dict:
    return {
        "version": __version__,
        "config": asdict(AppConfig()),
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
    audio.build(DIST / "demo" / "audio")
    (DIST / "demo" / "fixtures.json").write_text(json.dumps(fixtures(), ensure_ascii=False))
    print(f"demo {__version__} -> {DIST}")


if __name__ == "__main__":
    main()
