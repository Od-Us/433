"""Helpers about configured devices and their additional ids (aliases).

No Home Assistant import: unit tested in ``tests_unit``.

A device is stored in the options as either a plain name (legacy) or a dict::

    {"name": "Garden", "aliases": ["Oregon TempHygro_0B12"], ...}

Packets received for the primary id or any alias update the same entities.
Commands are always sent with the primary id.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    DEVICE_OPTION_KEYS,
    OPT_BINARY_SENSORS,
    OPT_EVENTS,
    OPT_SENSORS,
)
from .protocol import TYPE_SENSOR, decode_sensor_values

CONF_ALIASES = "aliases"


def device_aliases(config: Any) -> list[str]:
    """Return the additional ids of a device config."""
    if isinstance(config, Mapping):
        return [str(a) for a in config.get(CONF_ALIASES) or [] if a]
    return []


def with_aliases(config: Any, aliases: Iterable[str]) -> Any:
    """Return a copy of ``config`` with ``aliases`` (converting legacy strings)."""
    aliases = list(dict.fromkeys(a for a in aliases if a))
    if isinstance(config, Mapping):
        new = dict(config)
    else:
        new = {"name": str(config)}
    if aliases:
        new[CONF_ALIASES] = aliases
    else:
        new.pop(CONF_ALIASES, None)
    return new


def id_index(options: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Map every configured id (primary and alias) to (options key, primary id)."""
    index: dict[str, tuple[str, str]] = {}
    for key in DEVICE_OPTION_KEYS:
        for device_id, config in options.get(key, {}).items():
            index[device_id] = (key, device_id)
            for alias in device_aliases(config):
                index.setdefault(alias, (key, device_id))
    return index


def _remainder_parts(device_id: str, protocol: str) -> int:
    """Number of parts after ``protocol_`` (0 if the id is of another protocol)."""
    prefix = f"{protocol}_"
    if not protocol or not device_id.startswith(prefix):
        return 0
    rest = device_id[len(prefix) :]
    return len(rest.split("_")) if rest and all(rest.split("_")) else 0


def alias_for(
    option_key: str,
    device_id: str,
    seen_id: str,
    seen_type: str | None,
    seen_protocol: str | None = None,
) -> str | None:
    """Return the id to store as an alias of ``device_id`` for a received id.

    Returns None when the received device cannot be an alias (other protocol,
    other kind of packets).
    """
    protocol = seen_protocol or (
        seen_id.rsplit("_", 1)[0]
        if seen_type == TYPE_SENSOR
        else seen_id.rsplit("_", 2)[0]
    )
    parts = _remainder_parts(device_id, protocol)
    if parts not in (1, 2):
        return None

    if seen_type == TYPE_SENSOR:
        # Sensor-style packets are routed on protocol_id.
        if option_key != OPT_SENSORS and not (
            option_key in (OPT_BINARY_SENSORS, OPT_EVENTS) and parts == 1
        ):
            return None
        candidate = seen_id
    else:
        if option_key == OPT_SENSORS:
            return None
        if parts == 2:
            candidate = seen_id
        elif option_key == OPT_EVENTS:
            # Whole remote: the alias is the other remote, any button.
            candidate = seen_id.rsplit("_", 1)[0]
        else:
            return None

    return None if candidate == device_id else candidate


# ---------------------------------------------------------------------------
# Human readable preview of the last received values
# ---------------------------------------------------------------------------

_UNITS = {
    "temperature": "°C",
    "wind_chill": "°C",
    "wind_temperature": "°C",
    "humidity": "%",
    "pressure": " hPa",
    "total_rain": " mm",
    "rain_rate": " mm/h",
    "wind_speed": " km/h",
    "wind_average": " km/h",
    "wind_gust": " km/h",
    "wind_direction": "°",
    "illuminance": " lx",
    "power": " W",
}
_PREFIX = {"uv": "UV ", "battery": "bat. ", "wind_gust": "raf. "}
MAX_PREVIEW = 70


def describe_fields(fields: Mapping[str, str]) -> str:
    """Short text of the values of a packet, e.g. ``21.4°C · 48% · bat. OK``."""
    if "CMD" in fields:
        text = fields["CMD"]
        if "SWITCH" in fields:
            text = f"{fields['SWITCH']}: {text}"
        return text

    values, extra = decode_sensor_values(dict(fields))
    parts = []
    for key, value in values.items():
        if isinstance(value, float):
            value = f"{value:g}"
        parts.append(f"{_PREFIX.get(key, '')}{value}{_UNITS.get(key, '')}")
    parts.extend(f"{k.upper()}={v}" for k, v in extra.items())
    text = " · ".join(parts) or "-"
    if len(text) > MAX_PREVIEW:
        text = text[: MAX_PREVIEW - 1] + "…"
    return text


def format_age(last_seen: float | None, now: float | None = None) -> str:
    """Compact age of a timestamp: ``12 s``, ``5 min``, ``3 h``."""
    if last_seen is None:
        return "?"
    age = max(0, int((now if now is not None else time.time()) - last_seen))
    if age < 60:
        return f"{age} s"
    if age < 3600:
        return f"{age // 60} min"
    return f"{age // 3600} h"


def preview(info: Mapping[str, Any] | None, now: float | None = None) -> str | None:
    """``values (age)`` for an entry of ``hub.recent_devices`` (None if unknown)."""
    if not info:
        return None
    return f"{describe_fields(info.get('data', {}))} ({format_age(info.get('last_seen'), now)})"


def recent_info_for(
    recent: Mapping[str, Mapping[str, Any]], alias: str
) -> Mapping[str, Any] | None:
    """Recent entry for an id, also matching a remote id against its buttons."""
    if alias in recent:
        return recent[alias]
    matches = [
        info for dev_id, info in recent.items() if dev_id.rsplit("_", 1)[0] == alias
    ]
    if not matches:
        return None
    return max(matches, key=lambda info: info.get("last_seen") or 0)


def alias_candidates(
    recent: Mapping[str, Mapping[str, Any]],
    options: Mapping[str, Any],
    option_key: str,
    device_id: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Recently received ids that could be added as aliases of a device.

    Ids already configured (as a device or an alias) are skipped. Most recent
    first.
    """
    taken = id_index(options)
    found: dict[str, Mapping[str, Any]] = {}
    for seen_id, info in recent.items():
        alias = alias_for(
            option_key, device_id, seen_id, info.get("type"), info.get("protocol")
        )
        if alias is None or alias in taken:
            continue
        current = found.get(alias)
        if current is None or (info.get("last_seen") or 0) > (
            current.get("last_seen") or 0
        ):
            found[alias] = info
    return sorted(
        found.items(), key=lambda item: item[1].get("last_seen") or 0, reverse=True
    )


def devices_for_learned(
    options: Mapping[str, Any],
    seen_id: str,
    seen_type: str | None,
    seen_protocol: str | None = None,
) -> list[tuple[str, str, str]]:
    """Configured devices a received id could be an alias of.

    Returns (options key, primary id, alias id) tuples.
    """
    taken = id_index(options)
    result = []
    for key in DEVICE_OPTION_KEYS:
        for device_id in options.get(key, {}):
            alias = alias_for(key, device_id, seen_id, seen_type, seen_protocol)
            if alias is not None and alias not in taken:
                result.append((key, device_id, alias))
    return result
