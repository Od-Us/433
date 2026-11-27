"""Tests for additional ids (aliases) and in-place device edits."""

import asyncio

from homeassistant.core import Event
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rflink_ui import DOMAIN


async def _setup(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, data={"port": "COM1"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.01)
    return entry


def _track(hass, entity_id):
    """Record the states an entity goes through."""
    seen = []

    def _listener(event: Event) -> None:
        if event.data["entity_id"] == entity_id and event.data["new_state"]:
            seen.append(event.data["new_state"].state)

    hass.bus.async_listen("state_changed", _listener)
    return seen


async def test_sensor_alias_routes_to_same_entities(hass, mock_serial_connection):
    entry = await _setup(
        hass,
        {
            "sensors": {
                "Oregon TempHygro_0A4C": {
                    "name": "Garden",
                    "aliases": ["Oregon TempHygro_0B12"],
                }
            }
        },
    )
    hub = entry.runtime_data.hub
    hub.handle_line("20;01;Oregon TempHygro;ID=0A4C;TEMP=00ba;")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_temperature").state == "18.6"

    hub.handle_line("20;02;Oregon TempHygro;ID=0B12;TEMP=00d6;")
    await hass.async_block_till_done()
    state = hass.states.get("sensor.garden_temperature")
    assert state.state == "21.4"
    assert state.attributes["rflink_id"] == "0B12"


async def test_switch_alias_receives_but_sends_primary(hass, mock_serial_connection):
    entry = await _setup(
        hass, {"switches": {"Kaku_41_1": {"name": "Lamp", "aliases": ["Kaku_99_3"]}}}
    )
    entry.runtime_data.hub.handle_line("20;01;Kaku;ID=99;SWITCH=3;CMD=ON;")
    await hass.async_block_till_done()
    assert hass.states.get("switch.lamp").state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.lamp"}, blocking=True
    )
    assert b"10;Kaku;41;1;OFF;\n" in mock_serial_connection["writer"].written


async def test_edit_in_place_does_not_change_state(hass, mock_serial_connection):
    entry = await _setup(hass, {"binary_sensors": {"EV1527_0a1b2c_0": "Door"}})
    entry.runtime_data.hub.handle_line("20;01;EV1527;ID=0a1b2c;SWITCH=0;CMD=ON;")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.door").state == "on"
    seen = _track(hass, "binary_sensor.door")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_id": "[Binary Sensor] EV1527_0a1b2c_0"}
    )
    assert result["step_id"] == "edit_device"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Front door",
            "aliases": ["EV1527_0d0e0f_0"],
            "device_class": "door",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.options["binary_sensors"]["EV1527_0a1b2c_0"]["aliases"] == [
        "EV1527_0d0e0f_0"
    ]
    # Same entity, never unavailable, still on
    assert "unavailable" not in seen
    state = hass.states.get("binary_sensor.door")
    assert state.state == "on"
    assert state.attributes["device_class"] == "door"

    # The new id now drives the entity
    entry.runtime_data.hub.handle_line("20;02;EV1527;ID=0d0e0f;SWITCH=0;CMD=OFF;")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.door").state == "off"


async def test_change_primary_id_in_place(hass, mock_serial_connection):
    entry = await _setup(hass, {"switches": {"Kaku_41_1": "Lamp"}})
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.lamp"}, blocking=True
    )
    seen = _track(hass, "switch.lamp")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "modify"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device_id": "[Switch] Kaku_41_1", "new_device_id": "Kaku_77_2"},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert "unavailable" not in seen
    assert hass.states.get("switch.lamp").state == "on"
    ent = er.async_get(hass).async_get("switch.lamp")
    assert ent.unique_id == "rflink_switch_Kaku_77_2"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.lamp"}, blocking=True
    )
    assert b"10;Kaku;77;2;OFF;\n" in mock_serial_connection["writer"].written


async def test_learning_offers_additional_id(hass, mock_serial_connection):
    entry = await _setup(hass, {"sensors": {"Oregon TempHygro_0A4C": "Garden"}})
    hub = entry.runtime_data.hub
    hub.handle_line("20;01;Oregon TempHygro;ID=0A4C;TEMP=00ba;HUM=40;")
    hub.handle_line("20;02;Oregon TempHygro;ID=0B12;TEMP=00d6;HUM=48;")
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_learned"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_id": "Oregon TempHygro_0B12"}
    )
    assert result["step_id"] == "learned_target"
    assert "21.4°C" in result["description_placeholders"]["values"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"target": "sensors|Oregon TempHygro_0A4C"}
    )
    assert result["type"] == "create_entry"
    assert entry.options["sensors"]["Oregon TempHygro_0A4C"] == {
        "name": "Garden",
        "aliases": ["Oregon TempHygro_0B12"],
    }


async def test_edit_rejects_alias_of_other_device(hass, mock_serial_connection):
    entry = await _setup(hass, {"switches": {"Kaku_41_1": "Lamp", "Kaku_42_1": "Fan"}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_id": "[Switch] Kaku_41_1"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Lamp", "aliases": ["Kaku_42_1"]}
    )
    assert result["errors"] == {"aliases": "alias_conflict"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Lamp", "aliases": ["Kaku_41"]}
    )
    assert result["errors"] == {"aliases": "invalid_alias"}
