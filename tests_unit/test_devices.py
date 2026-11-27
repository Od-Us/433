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
