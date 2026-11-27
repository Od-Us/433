"""Tests for the RFLink frame parser and value decoding."""

import pytest
from conftest import protocol as p


def test_command_packet():
    pkt = p.parse_line("20;01;Kaku;ID=41;SWITCH=1;CMD=ON;")
    assert pkt.kind == p.KIND_DEVICE
    assert pkt.device_id == "Kaku_41_1"
    assert pkt.remote_id == "Kaku_41"
    assert pkt.device_type == p.TYPE_SWITCH
    assert pkt.fields["CMD"] == "ON"


def test_cover_packet():
    pkt = p.parse_line("20;3B;RTS;ID=0A4B;SWITCH=0;CMD=UP;")
    assert pkt.device_type == p.TYPE_COVER
    assert pkt.device_id == "RTS_0A4B_0"


def test_command_without_switch_defaults_to_zero():
    pkt = p.parse_line("20;05;EV1527;ID=0a1b2c;CMD=ON;")
    assert pkt.device_id == "EV1527_0a1b2c_0"


def test_sensor_packet():
    pkt = p.parse_line("20;3A;Oregon TempHygro;ID=0A4C;TEMP=00ba;HUM=40;BAT=OK;")
    assert pkt.device_type == p.TYPE_SENSOR
    assert pkt.device_id == "Oregon TempHygro_0A4C"
    assert pkt.remote_id is None


def test_f007_channel_alias():
    pkt = p.parse_line("20;10;F007_TH;ID=45291;TEMP=00e1;HUM=50;BAT=OK;")
    assert pkt.device_id == "F007_TH_CH2"
    assert pkt.alias_id == "F007_TH_45291"


@pytest.mark.parametrize(
    ("line", "response"),
    [
        ("20;02;OK;", p.RESPONSE_OK),
        ("20;03;CMD UNKNOWN;", p.RESPONSE_UNKNOWN),
        ("20;04;PONG;", p.RESPONSE_PONG),
        ("20;05;VER=1.1;REV=48;BUILD=0c;", p.RESPONSE_VERSION),
    ],
)
def test_responses(line, response):
    pkt = p.parse_line(line)
    assert pkt.kind == p.KIND_RESPONSE
    assert pkt.response == response


def test_version_fields():
    pkt = p.parse_line("20;05;VER=1.1;REV=48;BUILD=0c;")
    assert pkt.fields == {"VER": "1.1", "REV": "48", "BUILD": "0c"}


def test_banner_and_garbage():
    assert (
        p.parse_line("20;00;Nodo RadioFrequencyLink - RFLink Gateway V1.1 - R48;").kind
        == p.KIND_BANNER
    )
    assert p.parse_line("hello") is None
    assert p.parse_line("10;PING;") is None
    assert p.parse_line("20;06;DEBUG;Pulses=90;").kind == p.KIND_OTHER


def test_split_ids_preserve_underscored_protocol():
    assert p.split_device_id("Unitec_1a4a_4") == ("Unitec", "1a4a", "4")
    assert p.split_device_id("My_Proto_1a4a_4") == ("My_Proto", "1a4a", "4")
    assert p.split_remote_id("F007_TH_45291") == ("F007_TH", "45291")
    with pytest.raises(ValueError):
        p.split_device_id("Kaku_41")


def test_build_command():
    assert p.build_command("Kaku", "41", "1", "ON") == "10;Kaku;41;1;ON;\n"


def test_decode_sensor_values():
    values, extra = p.decode_sensor_values(
        {
            "ID": "0A4C",
            "TEMP": "80a5",  # negative: -16.5
            "HUM": "40",
            "BARO": "03f6",
            "RAIN": "0032",
            "WINSP": "0064",
            "WINDIR": "0012",  # decimal sector 12
            "BAT": "LOW",
            "HSTATUS": "2",
            "CHIME": "01",
        }
    )
    assert values["temperature"] == -16.5
    assert values["humidity"] == 40
    assert values["pressure"] == 1014
    assert values["total_rain"] == 5.0
    assert values["wind_speed"] == 10.0
    assert values["wind_direction"] == 12 * 22.5
    assert values["battery"] == "LOW"
    assert values["humidity_status"] == "dry"
    assert extra == {"chime": "01"}


def test_wind_direction_hex_fallback():
    assert p.decode_sensor_values({"WINDIR": "000f"})[0]["wind_direction"] == 15 * 22.5


def test_invalid_value_goes_to_attributes():
    values, extra = p.decode_sensor_values({"TEMP": "zz"})
    assert values == {}
    assert extra == {"temperature_raw": "zz"}


def test_debug_echo_is_ack():
    pkt = p.parse_line("20;10;RFUDEBUG=ON;")
    assert pkt.kind == p.KIND_RESPONSE
    assert pkt.response == p.RESPONSE_OK
