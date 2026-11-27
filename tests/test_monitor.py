"""Tests for the signal monitor of the options flow."""

import asyncio

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rflink_ui import DOMAIN


def _labels(result) -> str:
    """Labels of the selectable lines of the monitor form."""
    key = next(k for k in result["data_schema"].schema if str(k) == "line")
    selector = result["data_schema"].schema[key]
    return "\n".join(o["label"] for o in selector.config["options"])


async def test_monitor_decoded_and_raw(hass, mock_serial_connection):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"port": "COM1"},
        options={"sensors": {"Oregon TempHygro_0A4C": "Garden"}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.01)
    hub = entry.runtime_data.hub
    hub.handle_line("20;01;Oregon TempHygro;ID=0A4C;TEMP=00ba;HUM=40;")
    hub.handle_line("20;02;DEBUG;Pulses=90;Pulses(uSec)=1200,450;")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "monitor"}
    )
    assert result["step_id"] == "monitor"
    lines = _labels(result)
    assert "[Garden]" in lines and "18.6°C" in lines
    assert "Pulses" not in lines

    # Raw level with pulse sequences of unrecognised signals
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"level": "raw", "raw_mode": "unknown"}
    )
    assert result["step_id"] == "monitor"
    assert "Pulses(uSec)=1200,450" in _labels(result)
    assert hub.debug_mode == "unknown"
    assert b"10;RFUDEBUG=ON;\n" in mock_serial_connection["writer"].written

    # Closing the window switches the debug mode off
    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert hub.debug_mode is None
    assert b"10;RFUDEBUG=OFF;\n" in mock_serial_connection["writer"].written
