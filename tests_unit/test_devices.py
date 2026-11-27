"""Tests for device alias helpers and value previews."""

from conftest import devices as d

NOW = 1_000_000.0

RECENT = {
    "Oregon TempHygro_0B12": {
        "type": "sensor",
        "protocol": "Oregon TempHygro",
        "data": {"ID": "0B12", "TEMP": "00d6", "HUM": "48", "BAT": "OK"},
        "last_seen": NOW - 12,
    },
    "Oregon TempHygro_0C99": {
        "type": "sensor",
        "protocol": "Oregon TempHygro",
        "data": {"ID": "0C99", "TEMP": "8014", "HUM": "90", "BAT": "LOW"},
        "last_seen": NOW - 300,
    },
    "Cresta_1234": {
        "type": "sensor",
        "protocol": "Cresta",
        "data": {"ID": "1234", "TEMP": "0010"},
        "last_seen": NOW - 1,
    },
    "Kaku_42_1": {
        "type": "switch",
        "protocol": "Kaku",
        "data": {"ID": "42", "SWITCH": "1", "CMD": "ON"},
        "last_seen": NOW - 5,
    },
    "Kaku_42_2": {
        "type": "switch",
        "protocol": "Kaku",
        "data": {"ID": "42", "SWITCH": "2", "CMD": "OFF"},
        "last_seen": NOW - 2,
    },
    "F007_TH_CH3": {
        "type": "sensor",
        "protocol": "F007_TH",
        "data": {"ID": "45292", "TEMP": "00e1"},
        "last_seen": NOW - 7,
    },
}


def test_aliases_roundtrip():
    assert d.device_aliases("Garden") == []
    cfg = d.with_aliases("Garden", ["A_1", "A_1", ""])
    assert cfg == {"name": "Garden", "aliases": ["A_1"]}
    assert d.device_aliases(cfg) == ["A_1"]
    assert d.with_aliases(cfg, []) == {"name": "Garden"}


def test_id_index_includes_aliases():
    options = {
        "sensors": {"Oregon TempHygro_0A4C": {"name": "G", "aliases": ["X_1"]}},
        "switches": {"Kaku_41_1": "Lamp"},
    }
    index = d.id_index(options)
    assert index["X_1"] == ("sensors", "Oregon TempHygro_0A4C")
    assert index["Kaku_41_1"] == ("switches", "Kaku_41_1")


def test_alias_for_rules():
    assert (
        d.alias_for(
            "sensors",
            "Oregon TempHygro_0A4C",
            "Oregon TempHygro_0B12",
            "sensor",
            "Oregon TempHygro",
        )
        == "Oregon TempHygro_0B12"
    )
    # other protocol
    assert (
        d.alias_for(
            "sensors", "Oregon TempHygro_0A4C", "Cresta_1234", "sensor", "Cresta"
        )
        is None
    )
    # command packet never aliases a sensor
    assert d.alias_for("sensors", "Kaku_41", "Kaku_42_1", "switch", "Kaku") is None
    # button devices
    assert (
        d.alias_for("switches", "Kaku_41_1", "Kaku_42_1", "switch", "Kaku")
        == "Kaku_42_1"
    )
    assert d.alias_for("switches", "Kaku_41_1", "Kaku_41_1", "switch", "Kaku") is None
    # whole remote event -> remote id
    assert d.alias_for("events", "Kaku_41", "Kaku_42_2", "switch", "Kaku") == "Kaku_42"
    # 2-part binary sensor only takes sensor-style packets
    assert (
        d.alias_for("binary_sensors", "Kaku_41", "Kaku_42_1", "switch", "Kaku") is None
    )
    assert (
        d.alias_for(
            "binary_sensors", "X10Secure_11", "X10Secure_22", "sensor", "X10Secure"
        )
        == "X10Secure_22"
    )
    # underscored protocol
    assert (
        d.alias_for("sensors", "F007_TH_CH2", "F007_TH_CH3", "sensor", "F007_TH")
        == "F007_TH_CH3"
    )


def test_alias_candidates_sorted_and_filtered():
    options = {
        "sensors": {
            "Oregon TempHygro_0A4C": {"name": "G", "aliases": ["Oregon TempHygro_0C99"]}
        }
    }
    cands = d.alias_candidates(RECENT, options, "sensors", "Oregon TempHygro_0A4C")
    assert [c[0] for c in cands] == ["Oregon TempHygro_0B12"]

    cands = d.alias_candidates(
        RECENT, {"events": {"Kaku_41": {"name": "R"}}}, "events", "Kaku_41"
    )
    assert [c[0] for c in cands] == ["Kaku_42"]
    assert cands[0][1]["data"]["SWITCH"] == "2"  # most recent button kept


def test_devices_for_learned():
    options = {
        "sensors": {"Oregon TempHygro_0A4C": "Garden"},
        "switches": {"Kaku_41_1": "Lamp"},
        "events": {"Kaku_41": {"name": "Remote"}},
    }
    assert d.devices_for_learned(
        options, "Oregon TempHygro_0B12", "sensor", "Oregon TempHygro"
    ) == [("sensors", "Oregon TempHygro_0A4C", "Oregon TempHygro_0B12")]
    assert d.devices_for_learned(options, "Kaku_42_1", "switch", "Kaku") == [
        ("switches", "Kaku_41_1", "Kaku_42_1"),
        ("events", "Kaku_41", "Kaku_42"),
    ]


def test_describe_and_preview():
    assert (
        d.describe_fields(RECENT["Oregon TempHygro_0B12"]["data"])
        == "21.4°C · 48% · bat. OK"
    )
    assert (
        d.describe_fields(RECENT["Oregon TempHygro_0C99"]["data"])
        == "-2°C · 90% · bat. LOW"
    )
    assert d.describe_fields({"ID": "42", "SWITCH": "1", "CMD": "ON"}) == "1: ON"
    assert (
        d.describe_fields({"ID": "1", "WINSP": "0064", "WINDIR": "0004"})
        == "10 km/h · 90°"
    )
    assert d.preview(RECENT["Kaku_42_1"], NOW) == "1: ON (5 s)"
    assert d.preview(None) is None
    assert d.format_age(NOW - 7200, NOW) == "2 h"
    assert d.format_age(NOW - 300, NOW) == "5 min"


def test_recent_info_for_remote():
    assert d.recent_info_for(RECENT, "Kaku_42")["data"]["SWITCH"] == "2"
    assert d.recent_info_for(RECENT, "Nope_1") is None


def _hhmmss(ts):
    import time as _t

    return _t.strftime("%H:%M:%S", _t.gmtime(ts))


def test_describe_seen():
    info = {"first_seen": 3600.0, "last_seen": 3600.0 + 125, "count": 4}
    assert d.describe_seen(info, _hhmmss, "1re", "dern.") == (
        "1re 01:00:00 · dern. 01:02:05 · 4×"
    )
    once = {"last_seen": 60.0}
    assert d.describe_seen(once, _hhmmss) == "last 00:01:00 · 1×"
    assert d.describe_seen(None, _hhmmss) == ""


def test_monitor_lines_levels():
    from conftest import protocol as p

    history = []
    for ts, direction, raw in [
        (1.0, "out", "10;PING;"),
        (2.0, "in", "20;01;PONG;"),
        (3.0, "in", "20;02;Oregon TempHygro;ID=0A4C;TEMP=00ba;HUM=40;"),
        (4.0, "out", "10;Kaku;41;1;ON;"),
        (5.0, "in", "20;03;OK;"),
        (6.0, "in", "20;04;DEBUG;Pulses=90;Pulses(uSec)=1200,450,1200;"),
        (7.0, "in", "garbage line"),
    ]:
        pkt = p.parse_line(raw) if direction == "in" else None
        history.append(
            {
                "ts": ts,
                "dir": direction,
                "raw": raw,
                "kind": pkt.kind if pkt else None,
                "protocol": pkt.protocol if pkt else None,
                "device_id": pkt.device_id if pkt else None,
                "fields": pkt.fields if pkt else {},
            }
        )

    decoded = d.monitor_lines(
        history,
        d.LEVEL_DECODED,
        _hhmmss,
        names={"Oregon TempHygro_0A4C": "Garden"},
    )
    assert decoded == [
        "00:00:03 ← Oregon TempHygro 0A4C [Garden] · 18.6°C · 40%",
        "00:00:04 → 10;Kaku;41;1;ON;",
    ]

    raw = d.monitor_lines(history, d.LEVEL_RAW, _hhmmss)
    assert len(raw) == 7
    assert raw[5] == "00:00:06 ← 20;04;DEBUG;Pulses=90;Pulses(uSec)=1200,450,1200;"
    assert raw[6] == "00:00:07 ← garbage line"

    assert d.monitor_lines(history, d.LEVEL_RAW, _hhmmss, text_filter="pulses") == [
        raw[5]
    ]
    assert len(d.monitor_lines(history, d.LEVEL_RAW, _hhmmss, limit=2)) == 2


def test_pulses_and_commands():
    fields = {
        "Pulses": "10",
        "Pulses(uSec)": "1200,450,1200,450,330,9800,240,240,240,240",
    }
    values = d.pulse_values(fields)
    assert len(values) == 10
    assert d.format_pulses(values, per_row=8).splitlines() == [
        "   0: 1200  450 1200  450  330 9800  240  240",
        "   8:  240  240",
    ]
    assert (
        d.pulse_summary(values)
        == "10 pulses · min 240 µs · max 9800 µs · total 14.4 ms"
    )
    assert d.pulse_values({}) == []

    assert d.command_from_packet("Kaku", {"ID": "41", "SWITCH": "1", "CMD": "ON"}) == (
        "10;Kaku;41;1;ON;"
    )
    assert d.command_from_packet("RTS", {"ID": "0A4B", "SWITCH": "0", "CMD": "UP"}) == (
        "10;RTS;0A4B;0;UP;"
    )
    assert d.command_from_packet("Oregon", {"ID": "1", "TEMP": "00ba"}) is None

    assert d.valid_command("10;Kaku;41;1;ON;")
    assert not d.valid_command("10;Kaku;;1;ON;")
    assert not d.valid_command("20;01;OK;")
    assert not d.valid_command("10;Kaku;41;1;ON")
