"""The add-on catalog, what is switched on, which channels the built-in decoders claim, and the one
external program that may hold the dongle. Owned by the Controller (see AGENTS.md gotcha 58)."""
from __future__ import annotations

import shlex
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from ..config import CONFIG_DIR, AppConfig
from ..i18n import tr
from ..models import Modulation
from .acars import AcarsDecoder
from .aprs import AprsDecoder
from .events import EventHub
from .external import BIN_DIR, ExternalRunner, find_program, parse_rtl_433, parse_vdl2

CLAIM_TOLERANCE_HZ = 4200          # half the 8.333 kHz airband grid: one grid point per claimed channel


@dataclass(frozen=True)
class AddonSpec:
    id: str
    name: str
    kind: str                      # "builtin" (our code, on a scanner slot) or "external" (a program the user installs)
    summary: str
    license: str
    homepage: str = ""
    modulation: Modulation | None = None   # builtin: what the slot's demodulator must do
    frequencies: str = ""          # AddonsConfig field with the channels
    exe: tuple[str, ...] = ()
    experimental: bool = False


CATALOG: tuple[AddonSpec, ...] = (
    AddonSpec("acars", "ACARS", "builtin",
              "Reads aircraft text messages (ACARS, 131.5-131.9 MHz) from the channels the scanner stops on, instead of recording their buzz.",
              "FalconSDR (GPL-3.0-or-later)", modulation=Modulation.AM, frequencies="acars_frequencies_hz", experimental=True),
    AddonSpec("aprs", "APRS", "builtin",
              "Reads amateur-radio position reports, weather and messages (APRS, 144.800 MHz) from the channels the scanner stops on.",
              "FalconSDR (GPL-3.0-or-later)", modulation=Modulation.NFM, frequencies="aprs_frequencies_hz"),
    AddonSpec("rtl_433", "rtl_433 sensors", "external",
              "Weather stations, thermometers and other 433/868 MHz sensors, decoded by rtl_433. Needs the receiver for itself while it runs.",
              "rtl_433: GPL-2.0 (separate program)", homepage="https://github.com/merbanan/rtl_433", exe=("rtl_433", "rtl_433.exe")),
    AddonSpec("vdl2", "VDL2", "external",
              "Reads VDL Mode 2 aircraft data link (136.7-137 MHz) with dumpvdl2. Needs the receiver for itself while it runs.",
              "dumpvdl2: GPL-3.0, libacars: MIT (separate programs)", homepage="https://github.com/szpajder/dumpvdl2",
              frequencies="vdl2_frequencies_hz", exe=("dumpvdl2", "dumpvdl2.exe")),
)
SPECS = {s.id: s for s in CATALOG}


def _install_hint(spec: AddonSpec) -> str:
    if spec.id == "rtl_433":
        if sys.platform.startswith("linux"):
            return tr("Install it from your distribution (for example: sudo apt install rtl-433).")
        return tr("Download the Windows or macOS build from the project's releases page and put rtl_433 in {folder}, or set its path below.").format(folder=BIN_DIR / "rtl_433")
    if spec.id == "vdl2":
        return tr("dumpvdl2 has no ready Windows build: build it from its source (needs libacars) or use Linux, then put it in {folder} or set its path below.").format(folder=BIN_DIR / "vdl2")
    return ""


def summarize(event: dict) -> str:
    kind = event.get("kind")
    if kind == "aprs":
        where = f" {event['lat']:.4f}, {event['lon']:.4f}" if "lat" in event and "lon" in event else ""
        text = event.get("comment") or event.get("text") or ""
        return f"{event.get('source', '')} → {event.get('dest', '')}: {event.get('type', '')}{where} {text}".strip()
    if kind == "acars":
        head = " ".join(x for x in (event.get("registration"), event.get("flight"), f"[{event.get('label', '')}]") if x)
        return f"{head} {event.get('text', '')}".replace("\n", " ").strip()
    return event.get("summary", "")


@dataclass
class Claim:
    addon_id: str
    modulation: Modulation
    make: Callable[[], object]


class AddonManager:
    def __init__(self, config: AppConfig, log: Callable[[str], None] = lambda _m: None):
        self._config = config
        self._log = log
        self.hub = EventHub(CONFIG_DIR / "addons", config.addons.keep_days)
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, int]] = defaultdict(lambda: {"slots": 0, "events": 0})
        self._runner: ExternalRunner | None = None
        self.running_external = ""
        self.last_error = ""

    # -- state ----------------------------------------------------------------------------------
    def enabled(self, addon_id: str) -> bool:
        return addon_id in self._config.addons.enabled

    @property
    def listen_to_data(self) -> bool:
        return bool(self._config.addons.listen_to_data)

    def program(self, addon_id: str) -> str | None:
        spec = SPECS[addon_id]
        return find_program(spec.exe, str(self._config.addons.paths.get(addon_id, "")), addon_id) if spec.exe else None

    def status(self) -> list[dict]:
        cfg = self._config.addons
        out = []
        for spec in CATALOG:
            program = self.program(spec.id) if spec.exe else None
            out.append({
                "id": spec.id, "name": spec.name, "kind": spec.kind, "summary": tr(spec.summary), "license": spec.license,
                "homepage": spec.homepage, "enabled": self.enabled(spec.id), "experimental": spec.experimental,
                "installed": spec.kind == "builtin" or program is not None, "program": program or "",
                "running": self.running_external == spec.id,
                "frequencies": list(getattr(cfg, spec.frequencies)) if spec.frequencies else [],
                "path": str(cfg.paths.get(spec.id, "")), "extra_args": str(cfg.extra_args.get(spec.id, "")),
                "install_hint": _install_hint(spec) if spec.kind == "external" and program is None else "",
                "stats": dict(self._stats[spec.id]),
                "error": self.last_error if self.last_error.startswith(spec.id + ":") else "",
            })
        return out

    def configure(self, addon_id: str, patch: dict) -> None:
        spec = SPECS.get(addon_id)
        if spec is None:
            raise ValueError("No such add-on")
        cfg = self._config.addons
        if "enabled" in patch:
            enabled = [x for x in cfg.enabled if x != addon_id]
            if patch["enabled"]:
                enabled.append(addon_id)
            cfg.enabled = enabled
        if "frequencies" in patch and spec.frequencies:
            freqs = sorted({int(f) for f in patch["frequencies"] if 1_000_000 <= int(f) <= 6_000_000_000})
            setattr(cfg, spec.frequencies, freqs)
        if "path" in patch:
            cfg.paths = {**cfg.paths, addon_id: str(patch["path"]).strip()}
        if "extra_args" in patch:
            cfg.extra_args = {**cfg.extra_args, addon_id: str(patch["extra_args"]).strip()}
        if "listen_to_data" in patch:
            cfg.listen_to_data = bool(patch["listen_to_data"])
        self._config.save()

    # -- built-in decoders on scanner slots --------------------------------------------------------
    def claim(self, freq_hz: int) -> Claim | None:
        cfg = self._config.addons
        for spec in CATALOG:
            if spec.kind != "builtin" or spec.id not in cfg.enabled:
                continue
            if any(abs(freq_hz - int(f)) <= CLAIM_TOLERANCE_HZ for f in getattr(cfg, spec.frequencies)):
                make = AcarsDecoder if spec.id == "acars" else AprsDecoder
                self._stats[spec.id]["slots"] += 1
                return Claim(spec.id, spec.modulation, make)
        return None

    def publish_decoded(self, addon_id: str, freq_hz: int, events: list[dict]) -> None:
        for event in events:
            event = {**event, "freq_hz": freq_hz}
            self.hub.publish(addon_id, event, summarize(event))
            self._stats[addon_id]["events"] += 1

    # -- an external program holding the dongle -----------------------------------------------------
    def _device_args(self, addon_id: str, device: dict) -> list[str]:
        driver = str(device.get("driver", "auto"))
        if driver not in ("auto", "rtlsdr", "rtlsdr_native"):
            raise RuntimeError(tr("{name} works with RTL-SDR receivers only.").format(name=SPECS[addon_id].name))
        args = dict(kv.split("=", 1) for kv in str(device.get("device_args", "")).split(",") if "=" in kv)
        ident = args.get("serial") or args.get("index") or ""
        if addon_id == "vdl2":
            return ["--rtlsdr", ident or "0"]
        return ["-d", (":" + ident) if args.get("serial") else ident] if ident else []

    def start_external(self, addon_id: str, device: dict) -> None:
        spec = SPECS.get(addon_id)
        if spec is None or spec.kind != "external":
            raise ValueError("No such add-on")
        program = self.program(addon_id)
        if program is None:
            raise RuntimeError(tr("{name} is not installed.").format(name=spec.name))
        cfg = self._config.addons
        extra = shlex.split(str(cfg.extra_args.get(addon_id, "")))
        if addon_id == "vdl2":
            argv = [program, "--output", "decoded:json:file:path=-", *self._device_args(addon_id, device), *extra,
                    *[str(int(f)) for f in cfg.vdl2_frequencies_hz]]
            parse = parse_vdl2
        else:
            argv = [program, "-F", "json", *self._device_args(addon_id, device), *extra]
            parse = parse_rtl_433
        with self._lock:
            if self._runner is not None and self._runner.alive:
                raise RuntimeError(tr("Another add-on is running - stop it first."))
            self.last_error = ""

            def on_line(line: str) -> None:
                parsed = parse(line)
                if parsed is not None:
                    event, summary = parsed
                    self.hub.publish(addon_id, event, summary)
                    self._stats[addon_id]["events"] += 1

            def on_exit(code: int) -> None:
                with self._lock:
                    runner = self._runner
                    self._runner, self.running_external = None, ""
                    if code:
                        tail = " ".join(list(runner.stderr_tail)[-3:]) if runner else ""
                        self.last_error = f"{addon_id}: exit {code} {tail}".strip()
                        self._log(tr("{name} stopped by itself (exit {code}) {tail}").format(name=spec.name, code=code, tail=tail))

            runner = ExternalRunner(argv, on_line, on_exit)
            runner.start()
            self._runner, self.running_external = runner, addon_id
        self._log(tr("{name} started - the scanner is paused while it runs.").format(name=spec.name))

    def stop_external(self) -> None:
        with self._lock:
            runner = self._runner
        if runner is not None:
            runner.stop()
