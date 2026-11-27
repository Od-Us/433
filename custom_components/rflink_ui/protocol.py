"""RFLink serial protocol helpers.

This module is intentionally free of any Home Assistant import so that it can
be unit tested on its own.

RFLink frames look like::

    20;01;Kaku;ID=41;SWITCH=1;CMD=ON;                       (command packet)
    20;3A;Oregon TempHygro;ID=0A4C;TEMP=00ba;HUM=40;BAT=OK;  (sensor packet)
    20;02;OK;                                               (ack of a 10; command)
    20;03;CMD UNKNOWN;                                      (rejected command)
    20;04;PONG;                                             (answer to 10;PING;)
    20;05;VER=1.1;REV=48;BUILD=0c;                          (answer to 10;VERSION;)
    20;00;Nodo RadioFrequencyLink - RFLink Gateway V1.1 - R48;  (boot banner)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

KIND_DEVICE = "device"
KIND_RESPONSE = "response"
KIND_BANNER = "banner"
KIND_OTHER = "other"

RESPONSE_OK = "OK"
RESPONSE_UNKNOWN = "CMD UNKNOWN"
RESPONSE_PONG = "PONG"
RESPONSE_VERSION = "VER"

TYPE_SWITCH = "switch"
TYPE_COVER = "cover"
TYPE_SENSOR = "sensor"

COVER_COMMANDS = {"UP", "DOWN", "STOP"}

# Gateway debug modes printing raw pulse sequences ("20;xx;DEBUG;Pulses=..").
DEBUG_COMMANDS = ("RFDEBUG", "RFUDEBUG", "QRFDEBUG")


@dataclass(frozen=True)
class Packet:
    """A decoded RFLink line."""

    raw: str
    kind: str
    protocol: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    # Primary identifier used to route the packet to configured devices.
    device_id: str | None = None
    # Secondary identifier (legacy F007_TH raw id) also routed.
    alias_id: str | None = None
    # Identifier of the whole remote (protocol_id, without the switch/button).
    remote_id: str | None = None
    device_type: str | None = None
    # For KIND_RESPONSE: OK / CMD UNKNOWN / PONG / VER
    response: str | None = None


def parse_line(line: str) -> Packet | None:
    """Parse a raw RFLink line. Return None for lines that are not RFLink frames."""
    line = line.strip()
    parts = line.split(";")
    if len(parts) < 3 or parts[0] != "20":
        return None

    name = parts[2].strip()

    if name in (RESPONSE_OK, RESPONSE_UNKNOWN, RESPONSE_PONG):
        return Packet(raw=line, kind=KIND_RESPONSE, response=name)

    # Echo of a debug mode command (10;RFDEBUG=ON; -> 20;xx;RFDEBUG=ON;) is
    # the acknowledgement of that command.
    if name.split("=", 1)[0] in DEBUG_COMMANDS:
        return Packet(raw=line, kind=KIND_RESPONSE, response=RESPONSE_OK)

    fields: dict[str, str] = {}
    for part in parts[2:] if name.startswith("VER=") else parts[3:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()

    if name.startswith("VER="):
        return Packet(
            raw=line, kind=KIND_RESPONSE, response=RESPONSE_VERSION, fields=fields
        )

    if "RFLink Gateway" in name or "RadioFrequencyLink" in name:
        return Packet(raw=line, kind=KIND_BANNER, protocol=name)

    device_id = fields.get("ID")
    if not name or not device_id or name == "DEBUG":
        return Packet(raw=line, kind=KIND_OTHER, protocol=name or None, fields=fields)

    alias_id = None
    remote_id = None
    if "CMD" in fields:
        switch = fields.get("SWITCH", "0")
        full_id = f"{name}_{device_id}_{switch}"
        remote_id = f"{name}_{device_id}"
        cmd = fields["CMD"].upper()
        device_type = TYPE_COVER if cmd in COVER_COMMANDS else TYPE_SWITCH
    else:
        device_type = TYPE_SENSOR
        full_id = f"{name}_{device_id}"
        if name == "F007_TH":
            # Ambient Weather F007TH: the ID changes on battery swap, the channel
            # does not. Route on the channel and keep the raw id as an alias.
            try:
                channel = (int(device_id[-1], 16) & 0x07) + 1
            except ValueError:
                pass
            else:
                full_id = f"{name}_CH{channel}"
                alias_id = f"{name}_{device_id}"

    return Packet(
        raw=line,
        kind=KIND_DEVICE,
        protocol=name,
        fields=fields,
        device_id=full_id,
        alias_id=alias_id,
        remote_id=remote_id,
        device_type=device_type,
    )


def split_device_id(device_id: str) -> tuple[str, str, str]:
    """Split a ``protocol_id_switch`` identifier.

    Splits from the right so that protocol names containing ``_`` (e.g.
    ``F007_TH``) are preserved.
    """
    parts = device_id.rsplit("_", 2)
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    raise ValueError(
        f"Invalid RFLink device id '{device_id}', expected protocol_id_switch"
    )


def split_remote_id(device_id: str) -> tuple[str, str]:
    """Split a ``protocol_id`` identifier."""
    parts = device_id.rsplit("_", 1)
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise ValueError(f"Invalid RFLink remote id '{device_id}', expected protocol_id")


def build_command(protocol: str, *args: str | int) -> str:
    """Build an outgoing ``10;...;`` frame."""
    payload = ";".join(str(arg) for arg in (protocol, *args))
    return f"10;{payload};\n"


# ---------------------------------------------------------------------------
# Sensor value decoding
# ---------------------------------------------------------------------------


def _signed_tenths(value: str) -> float:
    """Decode RFLink signed hex (bit 15 = sign) expressed in tenths."""
    raw = int(value, 16)
    if raw & 0x8000:
        return -(raw & 0x7FFF) / 10.0
    return raw / 10.0


def _hex_int(value: str) -> int:
    return int(value, 16)


def _hex_tenths(value: str) -> float:
    try:
        return int(value, 16) / 10.0
    except ValueError:
        return float(value) / 10.0


def _decimal_int(value: str) -> int:
    return int(value, 10)


def _wind_direction(value: str) -> float:
    """WINDIR is a 0-15 compass sector. Firmwares send it in decimal or hex."""
    try:
        sector = int(value, 10)
    except ValueError:
        sector = int(value, 16)
    if not 0 <= sector <= 15:
        sector = int(value, 16)
    if not 0 <= sector <= 15:
        raise ValueError(value)
    return sector * 22.5


def _mapping(values: dict[int, str]) -> Callable[[str], str]:
    def convert(value: str) -> str:
        return values[int(value, 16)]

    return convert


HUMIDITY_STATUS = {0: "normal", 1: "comfortable", 2: "dry", 3: "wet"}
FORECAST = {0: "no_info", 1: "sunny", 2: "partly_cloudy", 3: "cloudy", 4: "rain"}


@dataclass(frozen=True)
class SensorField:
    """Description of an RFLink sensor field."""

    field: str
    key: str
    convert: Callable[[str], Any]


# Order matters: it is the order entities are created in for a new device.
SENSOR_FIELDS: tuple[SensorField, ...] = (
    SensorField("TEMP", "temperature", _signed_tenths),
    SensorField("HUM", "humidity", _decimal_int),
    SensorField("HSTATUS", "humidity_status", _mapping(HUMIDITY_STATUS)),
    SensorField("BARO", "pressure", _hex_int),
    SensorField("BFORECAST", "forecast", _mapping(FORECAST)),
    SensorField("UV", "uv", _hex_int),
    SensorField("LUX", "illuminance", _hex_int),
    SensorField("RAIN", "total_rain", _hex_tenths),
    SensorField("RAINRATE", "rain_rate", _hex_tenths),
    SensorField("WINSP", "wind_speed", _hex_tenths),
    SensorField("AWINSP", "wind_average", _hex_tenths),
    SensorField("WINGS", "wind_gust", _hex_tenths),
    SensorField("WINDIR", "wind_direction", _wind_direction),
    SensorField("WINCHL", "wind_chill", _signed_tenths),
    SensorField("WINTMP", "wind_temperature", _signed_tenths),
    SensorField("WATT", "power", _hex_int),
    SensorField("BAT", "battery", str),
)

SENSOR_FIELDS_BY_FIELD = {f.field: f for f in SENSOR_FIELDS}
SENSOR_FIELDS_BY_KEY = {f.key: f for f in SENSOR_FIELDS}

# Fields that are part of the frame structure rather than measurements.
NON_MEASUREMENT_FIELDS = {"ID", "SWITCH", "CMD"}


def decode_sensor_values(
    fields: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (decoded values by sensor key, extra raw fields as attributes)."""
    values: dict[str, Any] = {}
    extra: dict[str, str] = {}
    for name, raw in fields.items():
        if name in NON_MEASUREMENT_FIELDS:
            continue
        desc = SENSOR_FIELDS_BY_FIELD.get(name)
        if desc is None:
            extra[name.lower()] = raw
            continue
        try:
            values[desc.key] = desc.convert(raw)
        except (ValueError, KeyError):
            extra[f"{desc.key}_raw"] = raw
    return values, extra
