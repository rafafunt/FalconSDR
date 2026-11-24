"""Notifications with rules (an add-on of its own, AGENTS.md gotcha 59).

A rule says WHEN something is worth a message - a decoded message that matches (add-ons), a recording on a frequency,
a word in a transcript, a receiver or add-on that failed - and WHERE it goes (ntfy, Telegram, a webhook, MQTT; always
also the in-app list and a toast). Nothing is sent anywhere until the user adds a channel; a message's text then
goes to that service, which the page says. Delivery is on a worker thread with short timeouts and never touches the
scanner. Secrets (tokens, passwords) live in config.json (private) and are masked towards the browser."""
from __future__ import annotations

import json
import math
import queue
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Callable
from urllib.parse import urlparse

from .i18n import tr

MASK = "••••••••"
SECRET_KEYS = {"token", "password", "header_value"}
CHANNEL_TYPES = ("ntfy", "telegram", "webhook", "mqtt")
SOURCES = ("message", "recording", "transcript", "system")
SYSTEM_EVENTS = ("receiver_lost", "addon_failed")
FREQ_TOLERANCE_HZ = 4200
_RATE_PER_MINUTE = 6           # most notifications one rule may send in a minute, whatever its cooldown says
MIN_COOLDOWN_S = 30            # a rule can never be 'no limit': a busy channel or sensor would send one message per reception
_TIMEOUT_S = 10.0


# -- validation ---------------------------------------------------------------------------------------------
def _text(v, limit: int = 300) -> str:
    return str(v if v is not None else "").strip()[:limit]


def _num(v, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return default


def clean_channel(raw: dict, old: dict | None = None) -> dict:
    """One channel from the browser. A secret that arrives masked is kept as it was."""
    kind = _text(raw.get("type"), 20)
    if kind not in CHANNEL_TYPES:
        raise ValueError("unknown channel type")
    s = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    old_s = (old or {}).get("settings", {})
    out: dict = {}
    for key in ({"ntfy": ("server", "topic", "token", "priority"), "telegram": ("token", "chat_id", "api_base"),
                 "webhook": ("url", "header_name", "header_value"),
                 "mqtt": ("host", "port", "tls", "username", "password", "topic")}[kind]):
        value = s.get(key, "")
        if key in SECRET_KEYS:
            out[key] = old_s.get(key, "") if value == MASK else _text(value, 500)
        elif key == "tls":
            out[key] = bool(value)
        elif key in ("port", "priority"):
            out[key] = int(_num(value, 0)) or (1883 if key == "port" else 3)
        else:
            out[key] = _text(value, 300)
    if kind == "ntfy":
        out["server"] = out["server"] or "https://ntfy.sh"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", out["topic"]):
            raise ValueError("ntfy: the topic is letters, digits, - and _ (up to 64)")
        out["priority"] = min(5, max(1, out["priority"]))
    if kind == "telegram":
        out["api_base"] = out["api_base"] or "https://api.telegram.org"
    if kind in ("ntfy", "webhook", "telegram"):
        url = {"ntfy": out.get("server"), "webhook": out.get("url"), "telegram": out.get("api_base")}[kind]
        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            raise ValueError(f"{kind}: the address must start with http:// or https://")
    if kind == "webhook" and out.get("header_name"):
        if not re.fullmatch(r"[A-Za-z0-9-]+", out["header_name"]):
            raise ValueError("webhook: invalid header name")
        if any(c in out.get("header_value", "") for c in ("\r", "\n")):
            raise ValueError("webhook: invalid header value")
    if kind == "telegram":
        if not out["token"] or not out["chat_id"]:
            raise ValueError("telegram: a bot token and a chat id are needed")
    if kind == "mqtt":
        out["topic"] = out["topic"] or "falconsdr/notify"
        if not out["host"]:
            raise ValueError("mqtt: a broker address is needed")
        out["port"] = out["port"] if 0 < out["port"] < 65536 else (8883 if out["tls"] else 1883)
    return {"id": _text(raw.get("id"), 20) or secrets.token_hex(4), "type": kind, "name": _text(raw.get("name"), 60) or kind,
            "enabled": bool(raw.get("enabled", True)), "settings": out}


def clean_rule(raw: dict, channel_ids: set[str]) -> dict:
    source = _text(raw.get("source"), 20)
    if source not in SOURCES:
        raise ValueError("unknown rule source")
    words = [w.strip() for w in re.split(r"[,;\n]+", _text(raw.get("text_any"), 500)) if w.strip()][:20]
    op = raw.get("op") if raw.get("op") in ("<", ">") else "<"
    out = {
        "id": _text(raw.get("id"), 20) or secrets.token_hex(4), "name": _text(raw.get("name"), 80) or "Rule", "enabled": bool(raw.get("enabled", True)),
        "source": source, "channels": [c for c in raw.get("channels", []) if c in channel_ids],
        "cooldown_s": int(min(86400, max(MIN_COOLDOWN_S, _num(raw.get("cooldown_s"), 300)))),
        "addons": [a for a in raw.get("addons", []) if a in ("acars", "aprs", "vdl2", "rtl_433")],
        "text_any": ", ".join(words), "who": _text(raw.get("who"), 80), "label": _text(raw.get("label"), 10),
        "freq_mhz": max(0.0, _num(raw.get("freq_mhz"))), "metric": _text(raw.get("metric"), 40), "op": op,
        "value": _num(raw.get("value")), "within_km": max(0.0, _num(raw.get("within_km"))),
        "event": raw.get("event") if raw.get("event") in SYSTEM_EVENTS else "receiver_lost",
    }
    return out


def mask(channels: list[dict]) -> list[dict]:
    out = []
    for c in channels:
        s = {k: (MASK if k in SECRET_KEYS and v else v) for k, v in c.get("settings", {}).items()}
        out.append({**c, "settings": s})
    return out


# -- matching -----------------------------------------------------------------------------------------------
def _haystack(e: dict) -> str:
    parts = [e.get("summary"), e.get("text"), e.get("comment"), e.get("registration"), e.get("flight"), e.get("source"),
             e.get("model"), e.get("label"), e.get("src"), e.get("dst"), e.get("type")]
    return " ".join(str(p) for p in parts if p).lower()


def _who(e: dict) -> str:
    if e.get("kind") == "sensor":
        return f"{e.get('model', '')} #{e.get('device_id')}"
    return str(e.get("registration") or e.get("source") or e.get("src") or e.get("name") or e.get("freq_hz") or "")


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def matches(rule: dict, source: str, e: dict, home: tuple[float, float] = (0.0, 0.0)) -> bool:
    if rule["source"] != source:
        return False
    if source == "system":
        return e.get("event") == rule["event"]
    if source == "message":
        if rule["addons"] and e.get("addon") not in rule["addons"]:
            return False
    if rule["freq_mhz"] and abs(float(e.get("freq_hz") or 0) - rule["freq_mhz"] * 1e6) > FREQ_TOLERANCE_HZ:
        return False
    if source in ("message", "transcript"):
        words = [w.strip().lower() for w in rule["text_any"].split(",") if w.strip()]
        text = _haystack(e) if source == "message" else str(e.get("text", "")).lower()
        if words and not any(w in text for w in words):
            return False
    if source == "message":
        if rule["who"] and rule["who"].lower() not in " ".join(str(e.get(k, "")) for k in ("registration", "flight", "source", "src", "dst", "model", "device_id")).lower() + " " + _who(e).lower():
            return False
        if rule["label"] and str(e.get("label", "")) != rule["label"]:
            return False
        if rule["metric"]:
            value = (e.get("fields") or {}).get(rule["metric"]) if e.get("kind") == "sensor" else None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            if not (value < rule["value"] if rule["op"] == "<" else value > rule["value"]):
                return False
        if rule["within_km"]:
            lat, lon = e.get("lat"), e.get("lon")
            if lat is None or lon is None or not (home[0] or home[1]):
                return False
            if _km(home[0], home[1], float(lat), float(lon)) > rule["within_km"]:
                return False
    return True


# -- delivery -----------------------------------------------------------------------------------------------
def _contexts():
    try:
        from .digital_voice import _ssl_contexts      # the certificate fallbacks of a packaged build
        yield from _ssl_contexts()
    except Exception:  # noqa: BLE001
        yield None


def _post(url: str, data: bytes, headers: dict) -> None:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "FalconSDR", **headers}, method="POST")
    error = None
    for context in _contexts():
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S, context=context if url.startswith("https") else None) as resp:
                resp.read(2048)
            return
        except urllib.error.HTTPError as exc:
            try:
                why = exc.read(300).decode("utf-8", "replace").strip().replace("\n", " ")     # e.g. Discord: "Cannot send an empty message"
            except OSError:
                why = ""
            raise RuntimeError(f"HTTP {exc.code}" + (f": {why[:150]}" if why else "")) from exc
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise RuntimeError(str(exc.reason)) from exc
            error = error or exc
    raise RuntimeError(str(error))


def _varlen(n: int) -> bytes:
    out = bytearray()
    while True:
        digit, n = n % 128, n // 128
        out.append(digit | (128 if n else 0))
        if not n:
            return bytes(out)


def _mqtt_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _tls_socket(host: str, port: int):
    """A TLS connection, trying the certificate fallbacks of a packaged build when the default store does not know the broker's CA."""
    error: Exception | None = None
    for context in _contexts():
        sock = socket.create_connection((host, port), timeout=_TIMEOUT_S)
        try:
            return (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=host)
        except ssl.SSLCertVerificationError as exc:
            sock.close()
            error = error or exc
        except OSError:
            sock.close()
            raise
    raise error or RuntimeError("TLS failed")


def mqtt_publish(host: str, port: int, tls: bool, username: str, password: str, topic: str, payload: bytes) -> None:
    """One QoS 0 message over MQTT 3.1.1 (CONNECT, PUBLISH, DISCONNECT) - all this needs, no dependency."""
    sock = _tls_socket(host, port) if tls else socket.create_connection((host, port), timeout=_TIMEOUT_S)
    try:
        flags = 0x02 | (0x80 if username else 0) | (0x40 if username and password else 0)
        body = _mqtt_string("MQTT") + bytes([4, flags]) + (30).to_bytes(2, "big") + _mqtt_string("falconsdr-" + secrets.token_hex(3))
        if username:
            body += _mqtt_string(username) + (_mqtt_string(password) if password else b"")
        sock.sendall(bytes([0x10]) + _varlen(len(body)) + body)
        ack = b""
        while len(ack) < 4:
            chunk = sock.recv(4 - len(ack))
            if not chunk:
                raise RuntimeError("the broker closed the connection")
            ack += chunk
        if ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError(f"the broker refused the connection (code {ack[3] if len(ack) > 3 else '?'})")
        packet = _mqtt_string(topic) + payload
        sock.sendall(bytes([0x30]) + _varlen(len(packet)) + packet + bytes([0xE0, 0x00]))
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        sock.close()


def webhook_body(url: str, title: str, message: str, payload: dict) -> dict:
    """Discord and Slack accept only their own JSON (Discord answers 400 "Cannot send an empty message" to anything without
    `content`); every other address gets ours. A decoded message is untrusted text, so Discord is told not to ping anybody."""
    parts = urlparse(url)
    host, text = (parts.hostname or "").lower(), f"**{title}**\n{message}"
    if (host == "discord.com" or host.endswith(".discord.com") or host in ("discordapp.com", "www.discordapp.com")) and parts.path.startswith("/api/webhooks/"):
        return {"content": text[:2000], "allowed_mentions": {"parse": []}}
    if host == "hooks.slack.com":
        return {"text": f"*{title}*\n{message}"}
    return {"title": title, "message": message, **payload}


def send(channel: dict, title: str, message: str, payload: dict) -> None:
    """Delivers one notification; raises RuntimeError with a short reason."""
    s = channel["settings"]
    kind = channel["type"]
    if kind == "ntfy":
        body = {"topic": s["topic"], "title": title, "message": message, "priority": s["priority"], "tags": ["satellite_antenna"]}
        headers = {"Content-Type": "application/json"}
        if s.get("token"):
            headers["Authorization"] = f"Bearer {s['token']}"
        _post(s["server"].rstrip("/") + "/", json.dumps(body).encode(), headers)
    elif kind == "telegram":
        _post(f"{s['api_base'].rstrip('/')}/bot{s['token']}/sendMessage",
              json.dumps({"chat_id": s["chat_id"], "text": f"{title}\n{message}"}).encode(), {"Content-Type": "application/json"})
    elif kind == "webhook":
        headers = {"Content-Type": "application/json"}
        if s.get("header_name"):
            headers[s["header_name"]] = s.get("header_value", "")
        _post(s["url"], json.dumps(webhook_body(s["url"], title, message, payload), ensure_ascii=False).encode("utf-8"), headers)
    elif kind == "mqtt":
        mqtt_publish(s["host"], s["port"], s["tls"], s.get("username", ""), s.get("password", ""), s["topic"],
                     json.dumps({"title": title, "message": message, **payload}, ensure_ascii=False).encode("utf-8"))


class Notifier:
    def __init__(self, config, home: Callable[[], tuple[float, float]] = lambda: (0.0, 0.0), sender=send):
        self._config = config
        self._home = home
        self._send = sender
        self._lock = threading.Lock()
        self._cooldown: dict[tuple, float] = {}
        self._held: dict[tuple, int] = {}      # matches swallowed per (rule, who) since its last notice
        self._rate: dict[str, deque] = {}
        self.recent: deque[dict] = deque(maxlen=100)
        self._seq = int(time.time() * 1000)          # same reason as EventHub ids: one number line across restarts
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name="notifier").start()

    @property
    def seq(self) -> int:
        return self._seq

    # -- inputs ------------------------------------------------------------------------------------------------
    def on_message(self, event: dict) -> None:
        self._dispatch("message", event)

    def on_recording(self, freq_hz: int, name: str, path: str) -> None:
        self._dispatch("recording", {"freq_hz": freq_hz, "name": name, "path": path,
                                     "summary": f"{freq_hz / 1e6:.4f} MHz" + (f" {name}" if name else "")})

    def on_transcript(self, freq_hz: int, text: str, path: str) -> None:
        self._dispatch("transcript", {"freq_hz": freq_hz, "text": text, "path": path,
                                      "summary": f"{freq_hz / 1e6:.4f} MHz: {text[:200]}"})

    def on_system(self, event: str, detail: str) -> None:
        self._dispatch("system", {"event": event, "summary": detail})

    # -- rules ----------------------------------------------------------------------------------------------------
    def _dispatch(self, source: str, event: dict) -> None:
        cfg = self._config.notify
        if not cfg.enabled:
            return
        try:
            rules = [r for r in cfg.rules if r.get("enabled") and r.get("source") == source]
            for rule in rules:
                if not matches(rule, source, event, self._home()):
                    continue
                held = self._allowed(rule, event)
                if held is not None:
                    self._fire(rule, source, event, held)
        except Exception:  # noqa: BLE001 - a bad rule must never reach the decoder that called us
            import traceback
            traceback.print_exc()

    def _allowed(self, rule: dict, event: dict) -> int | None:
        """None = swallowed (counted, mentioned in the next notice); else how many matches were swallowed since the last one."""
        now = time.monotonic()
        with self._lock:
            key = (rule["id"], _who(event) or event.get("event", ""))
            recent = self._rate.setdefault(rule["id"], deque())
            while recent and now - recent[0] > 60:
                recent.popleft()
            if now - self._cooldown.get(key, -1e9) < rule["cooldown_s"] or len(recent) >= _RATE_PER_MINUTE:
                self._held[key] = self._held.get(key, 0) + 1
                return None
            recent.append(now)
            self._cooldown[key] = now
            if len(self._cooldown) > 5000:
                self._cooldown = {k: v for k, v in self._cooldown.items() if now - v < 86400}
                self._held = {k: v for k, v in self._held.items() if k in self._cooldown}
            return self._held.pop(key, 0)

    def _fire(self, rule: dict, source: str, event: dict, held: int = 0) -> None:
        title = rule["name"]
        message = str(event.get("summary") or event.get("text") or "")[:500]
        if source == "message" and event.get("freq_hz") and f"{event['freq_hz'] / 1e6:.3f}" not in message:
            message = f"{message}  ({event['freq_hz'] / 1e6:.4f} MHz)"
        if held:
            message = f"{message}  " + tr("(+{n} more since the last notice)").format(n=held)
        with self._lock:
            self._seq += 1
            item = {"id": self._seq, "ts": round(time.time(), 1), "rule": rule["name"], "title": title, "message": message,
                    "source": source, "results": {}}
            self.recent.append(item)
        channels = {c["id"]: c for c in self._config.notify.channels if c.get("enabled")}
        payload = {"rule": rule["name"], "source": source, "event": {k: v for k, v in event.items() if k != "path"}}
        for cid in rule["channels"]:
            if cid in channels:
                self._queue.put((item, channels[cid], payload))

    def _worker(self) -> None:
        while True:
            item, channel, payload = self._queue.get()
            try:
                self._send(channel, item["title"], item["message"], payload)
                status = "ok"
            except Exception as exc:  # noqa: BLE001
                status = f"error: {exc}"
            with self._lock:
                item["results"][channel["name"]] = status

    def test(self, channel: dict) -> str:
        """Sends a test message right now; returns "" or the reason it failed."""
        try:
            self._send(channel, "FalconSDR", "Test notification - it works.", {"rule": "test", "source": "test"})
            return ""
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    def since(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [{**i, "results": dict(i["results"])} for i in self.recent if i["id"] > after]      # the worker still fills results
