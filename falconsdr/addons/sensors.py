"""What rtl_433 can say, translated for the program: a catalog of its data fields (units, names), a guess at the kind of
device, and the privacy rules for the ones that are not a sensor's business to publish.

Built from the decoders' own field lists (rtl_433 25.12: 254 decoders, 194 distinct fields). Temperature (126 decoders)
and humidity (72) dominate, but there are also wind, rain, pressure, tyre pressure, power and energy, utility meters,
door/window/motion/smoke/leak sensors, remote controls, tank levels, soil, air quality and lightning."""
from __future__ import annotations

import re

# (regex of the field name, label, unit, group). Labels are English; the browser translates them.
_FAMILIES: list[tuple[str, str, str, str]] = [
    (r"temperature_(?:\d_)?C", "Temperature", "°C", "temperature"),
    (r"temperature_(?:\d_)?F", "Temperature", "°F", "temperature"),
    (r"setpoint_C", "Set point", "°C", "temperature"),
    (r"setpoint_F", "Set point", "°F", "temperature"),
    (r"humidity(?:_\d)?", "Humidity", "%", "humidity"),
    (r"moisture", "Soil moisture", "%", "soil"),
    (r"pressure_hPa", "Pressure", "hPa", "pressure"),
    (r"pressure_kPa", "Pressure", "kPa", "pressure"),
    (r"pressure_PSI|pressure_psi", "Pressure", "psi", "pressure"),
    (r"pressure_bar", "Pressure", "bar", "pressure"),
    (r"wind_avg_m_s", "Wind", "m/s", "wind"),
    (r"wind_avg_km_h", "Wind", "km/h", "wind"),
    (r"wind_avg_mi_h", "Wind", "mph", "wind"),
    (r"wind_max_m_s", "Gusts", "m/s", "wind"),
    (r"wind_max_km_h", "Gusts", "km/h", "wind"),
    (r"wind_max_mi_h", "Gusts", "mph", "wind"),
    (r"wind_dir_deg", "Wind direction", "°", "wind"),
    (r"rain_mm", "Rain", "mm", "rain"),
    (r"rain_in", "Rain", "in", "rain"),
    (r"rain_rate_mm_h", "Rain rate", "mm/h", "rain"),
    (r"rain_rate_in_h", "Rain rate", "in/h", "rain"),
    (r"light_lux", "Light", "lx", "light"),
    (r"uvi?", "UV index", "", "light"),
    (r"strike_count", "Lightning strikes", "", "lightning"),
    (r"strike_distance", "Strike distance", "km", "lightning"),
    (r"depth_cm", "Level", "cm", "tank"),
    (r"pm2_5_ug_m3", "PM2.5", "µg/m³", "air"),
    (r"pm10_0?_?ug_m3", "PM10", "µg/m³", "air"),
    (r"co2_ppm", "CO₂", "ppm", "air"),
    (r"power\d*_W", "Power", "W", "energy"),
    (r"energy_kWh", "Energy", "kWh", "energy"),
    (r"voltage_V", "Voltage", "V", "energy"),
    (r"current_A", "Current", "A", "energy"),
    (r"battery_V", "Battery", "V", "battery"),
    (r"battery_mV", "Battery", "mV", "battery"),
    (r"battery_pct", "Battery", "%", "battery"),
    (r"flow_rate_\w+|consumption\w*|volume_m3|gas_m3|water_m3", "Meter reading", "", "meter"),
]
_FAMILIES_RE = [(re.compile(f"^(?:{p})$"), label, unit, group) for p, label, unit, group in _FAMILIES]

# Status fields that are not numbers but worth a chip (door open, alarm, button...).
STATUS_KEYS = {"state", "alarm", "tamper", "motion", "event", "button", "command", "water", "leaking", "heating", "alert", "status", "sensor_type"}
# Technical noise of the decoders: never shown.
TECH = re.compile(r"^(?:flags\d?|data|raw|unknown\d?|counter|seq|sequence|len|payload|msg|msg_type|version|group|repeat|pairing|learn|test|"
                  r"startup|mode|unit|cmd|cmd_id|control|transmit|maybe_battery|display_u|ext_power|radio_clock|PacketCRC|MeterType|"
                  r"impulses|count|rssi|snr|noise|freq\d?|mic|time|model|type|subtype|id|channel|protocol)$")
# Codes of remote controls and key fobs: never stored (somebody's car or garage), whatever the settings say.
SECRET = {"code", "rolling", "fixed", "remote_id", "button_id", "data", "raw", "payload", "key", "serial"}
_REMOTE_MODEL = re.compile(r"remote|keyfob|key fob|car ?key|carremote|security\+|keeloq|hcs[23]00|somfy|nice flor|megacode|regency|"
                           r"doorbell|activlink|elro|byron|garage|rf-tech|dish remote|lightwave|intertechno|fs20|waveman|x10 rf|brennenstuhl|ht680|sc226", re.I)
_METER_MODEL = re.compile(r"ERT-|neptune|m-bus|wmbus|water meter|flow meter|flowis|badger|orion|gas meter|heat meter|scm", re.I)
_SECURITY_MODEL = re.compile(r"contact|door|window|smoke|motion|pir|leak|alarm|security|honeywell|dsc|interlogix|visonic|simplisafe|yale|kerui|chuango|cavius|jasco", re.I)

# One physical sensor, two names: Nexus (temperature only) and Rubicson send the same 36-bit frame and differ only in what the last
# byte means (Nexus: humidity, 0 on a thermometer; Rubicson: a CRC). When that byte happens to be a valid Rubicson CRC - about 1
# temperature in 256, e.g. 6.6 and 37.1 degrees for one id - rtl_433's Nexus decoder deliberately steps aside and the reading
# arrives as "Rubicson-Temperature". Without this the sensor would turn into a second device each time it passes such a value.
ALIASES = {"Rubicson-Temperature": "Nexus-T"}

CATEGORIES = ("weather", "thermometer", "tpms", "energy", "meter", "security", "remote", "tank", "soil", "air", "lightning", "other")


def describe_key(key: str) -> tuple[str, str, str] | None:
    """(label, unit, group) of a numeric data field, or None if it is not one we know."""
    for pattern, label, unit, group in _FAMILIES_RE:
        if pattern.match(key):
            n = re.search(r"_(\d)_", key) or re.search(r"power(\d+)_W", key)
            return (f"{label} {n.group(1)}" if n and label in ("Temperature", "Power") else label), unit, group
    return None


# The fields a rule can compare (for the browser): one per distinct label and unit.
_RULE_KEYS = ("temperature_C", "temperature_F", "humidity", "moisture", "pressure_hPa", "pressure_kPa", "pressure_PSI", "wind_avg_km_h", "wind_avg_m_s",
              "wind_max_km_h", "wind_max_m_s", "wind_dir_deg", "rain_mm", "rain_rate_mm_h", "light_lux", "uvi", "strike_count", "depth_cm", "pm2_5_ug_m3",
              "co2_ppm", "power_W", "energy_kWh", "voltage_V", "current_A", "battery_V", "battery_mV", "battery_pct", "setpoint_C")


def metrics() -> list[dict]:
    out, seen = [], set()
    for key in _RULE_KEYS:
        label, unit, group = describe_key(key)
        if (label, unit) not in seen:
            seen.add((label, unit))
            out.append({"key": key, "label": label, "unit": unit, "group": group})
    out.append({"key": "battery_ok", "label": "Battery OK", "unit": "", "group": "battery"})
    return out


def category(model: str, fields: dict) -> str:
    keys = set(fields)
    if fields.get("type") == "TPMS" or "TPMS" in model.upper():
        return "tpms"
    # A weather sensor also has a "button" (its test-transmit button): a remote is what has no temperature or humidity.
    measures = any(k.startswith(("temperature", "humidity")) for k in keys)
    if (keys & {"button", "command", "rolling", "fixed"} and not measures) or _REMOTE_MODEL.search(model):
        return "remote"
    if keys & {"strike_count"}:
        return "lightning"
    if keys & {"depth_cm"}:
        return "tank"
    if _METER_MODEL.search(model) and not keys & {"temperature_C", "temperature_F"}:
        return "meter"
    if any(k.startswith(("power", "energy", "voltage", "current")) for k in keys):
        return "energy"
    if keys & {"pm2_5_ug_m3", "co2_ppm"}:
        return "air"
    if any(k.startswith(("wind_", "rain")) for k in keys) or keys & {"uvi", "uv", "light_lux"}:
        return "weather"
    if "moisture" in keys:
        return "soil"
    if keys & {"alarm", "tamper", "motion", "water", "leaking", "state", "event"} or _SECURITY_MODEL.search(model):
        return "security"
    if any(k.startswith(("temperature", "humidity")) for k in keys):
        return "thermometer"
    return "other"


_PRIMARY = {"weather": ("temperature_C", "temperature_F", "wind_avg_km_h", "wind_avg_m_s", "rain_mm"), "tpms": ("pressure_kPa", "pressure_PSI", "pressure_hPa"),
            "energy": ("power_W", "energy_kWh", "voltage_V"), "tank": ("depth_cm",), "soil": ("moisture",), "air": ("pm2_5_ug_m3", "co2_ppm"),
            "lightning": ("strike_count",)}


def interpret(model: str, fields: dict) -> dict:
    """category, readings (numeric, with names and units), status chips and the field that stands for the device."""
    cat = category(model, fields)
    readings = []
    for key, value in fields.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        info = describe_key(key)
        if info is not None:
            readings.append({"key": key, "label": info[0], "unit": info[1], "value": round(value, 2) if isinstance(value, float) else value, "group": info[2]})
    status = [{"key": k, "value": v} for k, v in fields.items() if k in STATUS_KEYS and k not in SECRET and not isinstance(v, (dict, list))]
    wanted = _PRIMARY.get(cat, ("temperature_C", "temperature_F", "humidity", "moisture"))
    primary = next((k for k in wanted if any(r["key"] == k for r in readings)), readings[0]["key"] if readings else "")
    return {"category": cat, "readings": readings, "status": status, "primary": primary}


def redact(fields: dict) -> dict:
    """What may be stored: never a remote's code, and no decoder noise."""
    return {k: v for k, v in fields.items() if k not in SECRET and not (TECH.match(k) and k not in ("id", "channel"))}
