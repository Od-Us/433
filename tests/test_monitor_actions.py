"""Signal monitor: line details, full pulses and command buttons."""

import asyncio

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rflink_ui import DOMAIN


async def _setup(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, data={"port": "COM1"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.01)
    return entry


def _seq(hub, raw):
    return str(next(e["seq"] for e in hub.history if e["raw"] == raw))


async def _open_monitor(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "monitor"}
    )


async def test_detail_shows_every_pulse(hass, mock_serial_connection):
    entry = await _setup(hass, {})
    hub = entry.runtime_data.hub
    pulses = ",".join(str(300 + i) for i in range(120))
    raw = f"20;05;DEBUG;Pulses=120;Pulses(uSec)={pulses};"
    hub.handle_line(raw)

    result = await _open_monitor(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"level": "raw", "raw_mode": "off", "line": _seq(hub, raw)}
    )
    assert result["step_id"] == "monitor_detail"
    text = result["description_placeholders"]["pulses"]
    assert "120 pulses" in text
    assert " 419" in text  # last pulse, not truncated
    # The complete line is offered for copy
    copy_key = next(k for k in result["data_schema"].schema if str(k) == "copy")
    assert copy_key.description["suggested_value"] == raw


async def test_create_button_on_new_device(hass, mock_serial_connection):
    entry = await _setup(hass, {})
    hub = entry.runtime_data.hub
    raw = "20;01;Kaku;ID=41;SWITCH=1;CMD=ON;"
    hub.handle_line(raw)

    result = await _open_monitor(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"level": "decoded", "raw_mode": "off", "line": _seq(hub, raw)}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "command"}
    )
    assert result["step_id"] == "monitor_command"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "command": "10;Kaku;41;1;ON;",
            "name": "Garage on",
            "device": "new",
            "device_name": "Garage remote",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    (button_id, cfg), = entry.options["buttons"].items()
    assert cfg["command"] == "10;Kaku;41;1;ON;"
    state = hass.states.get("button.garage_remote_garage_on")
    assert state is not None

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.garage_remote_garage_on"}, blocking=True
    )
    assert b"10;Kaku;41;1;ON;\n" in mock_serial_connection["writer"].written

    # Removing the command removes the entity
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_id": f"[cmd] {button_id}"}
    )
    await hass.async_block_till_done()
    assert hass.states.get("button.garage_remote_garage_on") is None


async def test_invalid_command_rejected(hass, mock_serial_connection):
    entry = await _setup(hass, {"switches": {"Kaku_41_1": "Lamp"}})
    hub = entry.runtime_data.hub
    raw = "20;02;Oregon;ID=0A4C;TEMP=00ba;"
    hub.handle_line(raw)
    result = await _open_monitor(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"level": "decoded", "raw_mode": "off", "line": _seq(hub, raw)}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "command"}
    )
    assert result["description_placeholders"]["note"]  # cannot be converted
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"command": "hello", "name": "X", "device": "dev|Kaku_41_1"},
    )
    assert result["errors"] == {"command": "invalid_command"}
