"""Tests for the RFLink event platform (remotes, buttons, doorbells)."""

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rflink_ui import DOMAIN


async def _setup(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, data={"port": "COM1"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.01)
    return entry


async def test_single_button_event(hass, mock_serial_connection):
    entry = await _setup(hass, {"events": {"Kaku_41_1": {"name": "Hall Button"}}})
    hub = entry.runtime_data.hub

    state = hass.states.get("event.hall_button")
    assert state is not None
    assert state.state == "unknown"
    assert "on" in state.attributes["event_types"]

    hub.handle_line("20;01;Kaku;ID=41;SWITCH=1;CMD=ON;")
    await hass.async_block_till_done()
    state = hass.states.get("event.hall_button")
    assert state.attributes["event_type"] == "on"
    assert state.attributes["button"] == "1"
    first = state.state

    # Same button pressed again: a new event is fired (new timestamp)
    await asyncio.sleep(0.01)
    hub.handle_line("20;02;Kaku;ID=41;SWITCH=1;CMD=ON;")
    await hass.async_block_till_done()
    assert hass.states.get("event.hall_button").state != first

    # Other button of the same remote is ignored
    hub.handle_line("20;03;Kaku;ID=41;SWITCH=2;CMD=OFF;")
    await hass.async_block_till_done()
    assert hass.states.get("event.hall_button").attributes["event_type"] == "on"


async def test_whole_remote_event(hass, mock_serial_connection):
    entry = await _setup(hass, {"events": {"Kaku_41": {"name": "Remote"}}})
    hub = entry.runtime_data.hub

    hub.handle_line("20;01;Kaku;ID=41;SWITCH=3;CMD=OFF;")
    await hass.async_block_till_done()
    state = hass.states.get("event.remote")
    assert state.attributes["event_type"] == "off"
    assert state.attributes["button"] == "3"

    hub.handle_line("20;02;Kaku;ID=41;SWITCH=2;CMD=SET_LEVEL=7;")
    await hass.async_block_till_done()
    state = hass.states.get("event.remote")
    assert state.attributes["event_type"] == "dim"
    assert state.attributes["level"] == 7


async def test_doorbell_event(hass, mock_serial_connection):
    entry = await _setup(hass, {"events": {"Byron_1234": {"name": "Doorbell"}}})
    entry.runtime_data.hub.handle_line("20;05;Byron;ID=1234;CHIME=01;")
    await hass.async_block_till_done()
    state = hass.states.get("event.doorbell")
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["melody"] == "01"


async def test_received_bus_event(hass, mock_serial_connection):
    entry = await _setup(hass, {})
    events = []
    hass.bus.async_listen("rflink_ui_received", events.append)
    entry.runtime_data.hub.handle_line("20;01;Kaku;ID=99;SWITCH=1;CMD=ON;")
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["device_id"] == "Kaku_99_1"
    assert events[0].data["fields"]["CMD"] == "ON"


async def test_switch_command_not_acknowledged(hass, mock_serial_connection):
    """A command the gateway does not acknowledge raises and keeps the state."""
    entry = await _setup(hass, {"switches": {"Kaku_41_1": "Lamp"}})
    entry.runtime_data.hub.ack_timeout = 0.05
    mock_serial_connection["writer"].auto_reply = False

    with pytest.raises(HomeAssistantError, match="acknowledge"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.lamp"}, blocking=True
        )
    assert hass.states.get("switch.lamp").state == "off"


async def test_repeats_setting(hass, mock_serial_connection):
    entry = await _setup(
        hass, {"switches": {"Kaku_41_1": "Lamp"}, "settings": {"repeats": 3}}
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.lamp"}, blocking=True
    )
    assert mock_serial_connection["writer"].written.count(b"10;Kaku;41;1;ON;\n") == 3
    assert entry.runtime_data.hub.stats["acked"] == 3


async def test_options_flow_event_and_settings(hass, mock_serial_connection):
    entry = await _setup(hass, {})

    # Add an event for every button of a remote
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_manual"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device_type": "Event", "device_id": "Kaku_41_1", "name": "Remote"},
    )
    assert result["step_id"] == "event_options"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"whole_remote": True}
    )
    assert result["type"] == "create_entry"
    assert entry.options["events"] == {"Kaku_41": {"name": "Remote"}}
    await hass.async_block_till_done()
    assert hass.states.get("event.remote") is not None

    # Duplicate / invalid ids are refused
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_manual"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device_type": "Switch", "device_id": "Kaku_41", "name": "Dup"},
    )
    assert result["errors"] == {"device_id": "already_configured"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device_type": "Switch", "device_id": "Kaku_99", "name": "Bad"},
    )
    assert result["errors"] == {"device_id": "invalid_device_id"}

    # Gateway settings
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"repeats": 2}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.hub.repeats == 2
    assert entry.options["events"] == {"Kaku_41": {"name": "Remote"}}
