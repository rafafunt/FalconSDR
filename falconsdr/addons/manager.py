"""The add-on catalog, what is switched on, which channels the built-in decoders claim, and the one
external program that may hold the dongle. Owned by the Controller (see AGENTS.md gotcha 58)."""
from __future__ import annotations

import re
import shlex
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import CONFIG_DIR, AppConfig
from ..i18n import tr
from ..models import Modulation
from .acars import AcarsDecoder
from .aprs import AprsDecoder
from .events import EventHub
from .stats import StatsStore
from ..sdr_backend import SoapyBackend
from . import install as installer
from .external import BIN_DIR, ExternalRunner, find_program, parse_rtl_433, parse_vdl2

_BUSY_RETRY_S = 3.0               # pause before starting a program again after "receiver busy"
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
              "FalconSDR, GPL-3.0-or-later", modulation=Modulation.AM, frequencies="acars_frequencies_hz", experimental=True),
    AddonSpec("aprs", "APRS", "builtin",
              "Reads amateur-radio position reports, weather and messages (APRS, 144.800 MHz) from the channels the scanner stops on.",
              "FalconSDR, GPL-3.0-or-later", modulation=Modulation.NFM, frequencies="aprs_frequencies_hz"),
    AddonSpec("rtl_433", "rtl_433 sensors", "external",
              "Weather stations, thermometers and other 433/868 MHz sensors, decoded by rtl_433. Needs the receiver for itself while it runs.",
              "rtl_433: GPL-2.0", homepage="https://github.com/merbanan/rtl_433", exe=("rtl_433", "rtl_433.exe")),
    AddonSpec("vdl2", "VDL2", "external",
              "Reads VDL Mode 2 aircraft data link (136.7-137 MHz) with dumpvdl2. Needs the receiver for itself while it runs.",
              "dumpvdl2: GPL-3.0, libacars: MIT", homepage="https://github.com/szpajder/dumpvdl2",
              frequencies="vdl2_frequencies_hz", exe=("dumpvdl2", "dumpvdl2.exe")),
)
SPECS = {s.id: s for s in CATALOG}


# What can be set for an external program. Everything is checked and turned into flags here (never passed through
# as text), so a typo cannot become a command line. rtl_433: -f/-H/-g/-p/-C; dumpvdl2: --gain/--correction.
RTL433_BANDS = {"433": [433.92], "868": [868.3], "315": [315.0], "915": [915.0], "433+868": [433.92, 868.3]}
OPTION_DEFAULTS: dict[str, dict] = {
    "rtl_433": {"band": "433", "custom_mhz": "", "hop_s": 30, "gain": "", "ppm": 0, "units": "native"},
    "vdl2": {"gain": "", "ppm": 0},
}


def _number(value, low: float, high: float, name: str) -> float:
    try:
        number = float(str(value).replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name}: not a number") from exc
    if not low <= number <= high:
        raise ValueError(f"{name}: must be between {low:g} and {high:g}")
    return number


def clean_options(addon_id: str, current: dict, patch: dict) -> dict:
    """``current`` updated with the valid keys of ``patch``; raises ValueError for a bad value."""
    defaults = OPTION_DEFAULTS.get(addon_id, {})
    out = {**defaults, **{k: v for k, v in current.items() if k in defaults}}
    for key, value in patch.items():
        if key not in defaults:
            continue
        if key == "band":
            if value not in (*RTL433_BANDS, "custom"):
                raise ValueError("band: unknown")
        elif key == "custom_mhz":
            # "433.92, 868.3" (a comma and a space) separates; "868,95" (a comma between digits) is a decimal comma.
            tokens = re.split(r"[\s;]+", re.sub(r",\s+", " ", str(value).strip()))
            value = " ".join(f"{_number(v, 1, 6000, 'frequency'):g}" for v in tokens if v)
        elif key == "hop_s":
            value = int(_number(value, 1, 3600, "hop"))
        elif key == "gain":
            value = "" if str(value).strip() == "" else _number(value, 0, 80, "gain")
        elif key == "ppm":
            value = int(_number(value, -500, 500, "ppm"))
        elif key == "units":
            if value not in ("native", "si", "customary"):
                raise ValueError("units: unknown")
        out[key] = value
    return out


def _install_hint(spec: AddonSpec) -> str:
    if spec.id == "rtl_433":
        if installer.can_install("rtl_433"):
            return tr("FalconSDR can download the ready build the author publishes for this system.")
        return tr("Install it from your distribution (for example: sudo apt install rtl-433), or put the program in {folder} or set its path below.").format(folder=BIN_DIR / "rtl_433")
    if spec.id == "vdl2":
        if installer.can_install("vdl2"):
            return tr("FalconSDR can build dumpvdl2 for you from the author's source code (a few minutes).")
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
    def __init__(self, config: AppConfig, log: Callable[[str], None] = lambda _m: None,
                 on_stopped: Callable[[str], None] = lambda _id: None):
        self._config = config
        self._log = log
        self._on_stopped = on_stopped     # an external program ended (by itself or stopped): the Controller gives its receiver back
        home = lambda: (float(config.location.home_lat or 0), float(config.location.home_lon or 0))   # noqa: E731
        self.stats = StatsStore(CONFIG_DIR / "addons" / "stats", home)
        if self.stats.empty:
            self.stats.backfill(CONFIG_DIR / "addons")       # the days of messages already on disk
        self.hub = EventHub(CONFIG_DIR / "addons", config.addons.keep_days, stats=self.stats)
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, int]] = defaultdict(lambda: {"slots": 0, "events": 0})
        # Several external programs may run at once, each on its own receiver: id -> {"runner", "since", "cancelled"}.
        self._runs: dict[str, dict] = {}
        self.last_error = ""
        self.schedule_failed: set[str] = set()      # add-ons the schedule tried and could not start (Controller keeps it)

    @property
    def running_external(self) -> str:
        """The first running external program ('' = none): what the single-add-on parts of the UI show."""
        with self._lock:
            return next(iter(self._runs), "")

    @property
    def running_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    @property
    def started_at(self) -> float:
        with self._lock:
            return next((r["since"] for r in self._runs.values()), 0.0)

    def can_run(self, addon_id: str) -> bool:
        """Could the schedule start this add-on? (An installed external program that did not just fail.)"""
        spec = SPECS.get(addon_id)
        return bool(spec and spec.kind == "external" and self.program(addon_id) and addon_id not in self.schedule_failed)

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
                "id": spec.id, "name": spec.name, "kind": spec.kind, "summary": tr(spec.summary), "license": tr(spec.license),
                "homepage": spec.homepage, "enabled": self.enabled(spec.id), "experimental": spec.experimental,
                "installed": spec.kind == "builtin" or program is not None, "program": program or "",
                "running": spec.id in self.running_ids,
                "frequencies": list(getattr(cfg, spec.frequencies)) if spec.frequencies else [],
                "receiver": self.receiver(spec.id, 0 if spec.kind == "external" else -1),
                "options": clean_options(spec.id, cfg.options.get(spec.id, {}), {}) if spec.id in OPTION_DEFAULTS else {},
                "path": str(cfg.paths.get(spec.id, "")), "extra_args": str(cfg.extra_args.get(spec.id, "")),
                "install_hint": _install_hint(spec) if spec.kind == "external" and program is None else "",
                "managed": bool(program) and Path(program).is_relative_to(BIN_DIR),
                "can_install": installer.can_install(spec.id), "install": installer.state(spec.id) if installer.can_install(spec.id) else None,
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
        if "options" in patch and spec.id in OPTION_DEFAULTS:
            cfg.options = {**cfg.options, addon_id: clean_options(addon_id, cfg.options.get(addon_id, {}), patch["options"])}
        if "receiver" in patch:
            cfg.receivers = {**cfg.receivers, addon_id: int(patch["receiver"])}
        if "path" in patch:
            cfg.paths = {**cfg.paths, addon_id: str(patch["path"]).strip()}
        if "extra_args" in patch:
            cfg.extra_args = {**cfg.extra_args, addon_id: str(patch["extra_args"]).strip()}
        if "listen_to_data" in patch:
            cfg.listen_to_data = bool(patch["listen_to_data"])
        self._config.save()

    # -- built-in decoders on scanner slots --------------------------------------------------------
    def receiver(self, addon_id: str, default: int = -1) -> int:
        """The receiver chosen for an add-on (index; -1 = any)."""
        value = self._config.addons.receivers.get(addon_id, default)
        return value if isinstance(value, int) else default

    def claim(self, freq_hz: int, receiver: int = 0) -> Claim | None:
        cfg = self._config.addons
        for spec in CATALOG:
            if spec.kind != "builtin" or spec.id not in cfg.enabled:
                continue
            if self.receiver(spec.id) not in (-1, receiver):
                continue          # this add-on listens on another receiver
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

    def _argv(self, addon_id: str, program: str, device: dict):
        cfg = self._config.addons
        opts = clean_options(addon_id, cfg.options.get(addon_id, {}), {})
        extra = shlex.split(str(cfg.extra_args.get(addon_id, "")))
        gain = [] if opts["gain"] == "" else [("--gain" if addon_id == "vdl2" else "-g"), f"{float(opts['gain']):g}"]
        ppm = [] if not opts["ppm"] else [("--correction" if addon_id == "vdl2" else "-p"), str(int(opts["ppm"]))]
        if addon_id == "vdl2":
            return [program, "--output", "decoded:json:file:path=-", *self._device_args(addon_id, device), *gain, *ppm, *extra,
                    *[str(int(f)) for f in cfg.vdl2_frequencies_hz]], parse_vdl2
        if opts["band"] == "custom":
            freqs = [float(v) for v in opts["custom_mhz"].split()] or RTL433_BANDS["433"]
        else:
            freqs = RTL433_BANDS[opts["band"]]
        band = [x for f in freqs for x in ("-f", f"{f:g}M")] + (["-H", str(int(opts["hop_s"]))] if len(freqs) > 1 else [])
        units = [] if opts["units"] == "native" else ["-C", opts["units"]]
        # "-F log" as well: without it rtl_433 says nothing about why it failed
        return [program, "-F", "json", "-F", "log", *self._device_args(addon_id, device), *band, *gain, *ppm, *units, *extra], parse_rtl_433

    def start_external(self, addon_id: str, device: dict) -> None:
        spec = SPECS.get(addon_id)
        if spec is None or spec.kind != "external":
            raise ValueError("No such add-on")
        program = self.program(addon_id)
        if program is None:
            raise RuntimeError(tr("{name} is not installed.").format(name=spec.name))
        argv, parse = self._argv(addon_id, program, device)
        with self._lock:
            if addon_id in self._runs:
                raise RuntimeError(tr("{name} is already running.").format(name=spec.name))
            self.last_error = ""
            self._runs[addon_id] = {"runner": None, "since": time.time(), "cancelled": False}
        # The dongle that the scanner just closed is not free for a moment (usb_claim_interface -6): the same
        # cooldown the backends keep between a close and the next open.
        SoapyBackend._wait_for_reopen_cooldown()
        try:
            self._launch(addon_id, argv, parse, tries=3)
        except OSError as exc:      # the program could not even be started
            with self._lock:
                self._runs.pop(addon_id, None)
            raise RuntimeError(f"{spec.name}: {exc}") from exc
        self._log(tr("{name} started - the scanner is paused while it runs.").format(name=spec.name))

    def _launch(self, addon_id: str, argv: list[str], parse, tries: int) -> None:
        spec = SPECS[addon_id]
        started = time.monotonic()

        def on_line(line: str) -> None:
            parsed = parse(line)
            if parsed is not None:
                event, summary = parsed
                self.hub.publish(addon_id, event, summary)
                self._stats[addon_id]["events"] += 1

        def on_exit(code: int) -> None:
            SoapyBackend._record_close_time()       # the next open (the scanner's) keeps the cooldown too
            with self._lock:
                run = self._runs.get(addon_id)
                runner = run["runner"] if run else None
                cancelled = run is None or run["cancelled"]
            tail = list(runner.stderr_tail) if runner else []
            busy = (bool(code) and tries > 0 and not cancelled and time.monotonic() - started < 10
                    and any(re.search(r"claim|busy|-6\b", line, re.I) for line in tail))
            if busy:    # the receiver was not yet free: wait and try again, quietly
                self._log(tr("The receiver was still busy - trying again."))
                threading.Timer(_BUSY_RETRY_S, lambda: self._retry(addon_id, argv, parse, tries - 1)).start()
                return
            with self._lock:
                self._runs.pop(addon_id, None)
                if code:
                    shown = " ".join(tail[-3:])
                    self.last_error = f"{addon_id}: exit {code} {shown}".strip()
                    self._log(tr("{name} stopped by itself (exit {code}) {tail}").format(name=spec.name, code=code, tail=shown))
            self._on_stopped(addon_id)

        runner = ExternalRunner(argv, on_line, on_exit)
        with self._lock:
            if addon_id in self._runs:
                self._runs[addon_id]["runner"] = runner
        runner.start()

    def _retry(self, addon_id: str, argv: list[str], parse, tries: int) -> None:
        with self._lock:
            run = self._runs.get(addon_id)
            if run is None or run["cancelled"]:
                return
        self._launch(addon_id, argv, parse, tries)

    def stop_external(self, addon_id: str = "") -> None:
        """Stops one external program, or all of them."""
        with self._lock:
            ids = [addon_id] if addon_id else list(self._runs)
            runners = []
            for i in ids:
                run = self._runs.get(i)
                if run:
                    run["cancelled"] = True
                    runners.append((i, run["runner"]))
        for i, runner in runners:
            if runner is not None:
                runner.stop()
            with self._lock:
                run = self._runs.get(i)
                if run and not (run["runner"] is not None and run["runner"].alive):
                    self._runs.pop(i, None)
        SoapyBackend._record_close_time()
