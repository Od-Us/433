import asyncio

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rflink_ui import DOMAIN
from custom_components.rflink_ui.const import signal_update


async def test_sensor_setup_and_updates(hass, mock_serial_connection):
    """Test setting up a sensor and receiving temperature and humidity updates."""
    # Define a sensor in options
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"port": "COM1"},
        options={
            "switches": {},
            "sensors": {"Oregon_0A4C": "Garden Sensor"},
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.01)

    # 1. Entities are created on demand: nothing exists before the first packet
    assert hass.states.get("sensor.garden_sensor_temperature") is None
    assert hass.states.get("sensor.garden_sensor_total_rain") is None

    # 2. Simulate dispatcher update (Positive temperature + humidity + OK battery)
    # TEMP = "00ba" hex = 186 dec -> 18.6°C
    # HUM = "40" -> 40%
    # BAT = "OK"
    # RSSI = "6" (custom/unknown key)
    from homeassistant.helpers.dispatcher import dispatcher_send
    dispatcher_send(
        hass,
        signal_update(entry.entry_id, "Oregon_0A4C"),
        {
            "TEMP": "00ba",
            "HUM": "40",
            "BAT": "OK",
            "RSSI": "6"
        }
    )
    await hass.async_block_till_done()

    # Verify the entities were created for the received measurements only
    temp_state = hass.states.get("sensor.garden_sensor_temperature")
    assert temp_state.state == "18.6"
    assert temp_state.attributes.get("unit_of_measurement") == "°C"
    assert hass.states.get("sensor.garden_sensor_humidity").attributes.get(
        "unit_of_measurement"
    ) == "%"
    assert hass.states.get("sensor.garden_sensor_battery").attributes.get("icon") == (
        "mdi:battery"
    )
    assert hass.states.get("sensor.garden_sensor_total_rain") is None
    assert temp_state.attributes.get("battery") == "OK"
    assert temp_state.attributes.get("rssi") == "6"

    hum_state = hass.states.get("sensor.garden_sensor_humidity")
    assert hum_state.state == "40"
    assert hum_state.attributes.get("battery") == "OK"

    battery_state = hass.states.get("sensor.garden_sensor_battery")
    assert battery_state.state == "OK"

    # 3. Simulate dispatcher update (Negative temperature + LOW battery)
    # -5.4°C = 54 dec. High bit 0x8000 set -> 0x8036 hex = "8036"
    dispatcher_send(
        hass,
        signal_update(entry.entry_id, "Oregon_0A4C"),
        {
            "TEMP": "8036",
            "BAT": "LOW",
        }
    )
    await hass.async_block_till_done()

    temp_state = hass.states.get("sensor.garden_sensor_temperature")
    assert temp_state.state == "-5.4"
    assert temp_state.attributes.get("battery") == "LOW"

    battery_state = hass.states.get("sensor.garden_sensor_battery")
    assert battery_state.state == "LOW"


async def test_f007_th_channel_alias(hass, mock_serial_connection):
    """Test F007_TH sensor setup using stable channel ID (F007_TH_CH7)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"port": "COM1"},
        options={
            "switches": {},
            "sensors": {"F007_TH_CH7": "Patio Sensor"},
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Simulate receiving RFLink packet with raw ID 45246 (lowest hex digit 6 -> CH7)
    entry.runtime_data.hub.handle_line("20;62;F007_TH;ID=45246;TEMP=00d2;HUM=55;BAT=OK;")
    await hass.async_block_till_done()

    temp_state = hass.states.get("sensor.patio_sensor_temperature")
    assert temp_state is not None
    assert temp_state.state == "21.0"

    hum_state = hass.states.get("sensor.patio_sensor_humidity")
    assert hum_state is not None
    assert hum_state.state == "55"

    battery_state = hass.states.get("sensor.patio_sensor_battery")
    assert battery_state is not None
    assert battery_state.state == "OK"


async def test_rain_sensor_updates(hass, mock_serial_connection):
    """Test rain sensor entity creation and RAIN packet parsing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"port": "COM1"},
        options={
            "switches": {},
            "sensors": {"Auriol Rain_0057": "Rain Gauge"},
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.rain_gauge_total_rain") is None

    # Simulate RFLink packet: RAIN=0064 -> hex 64 = 100 -> 10.0 mm
    entry.runtime_data.hub.handle_line("20;3B;Auriol Rain;ID=0057;RAIN=0064;BAT=OK;")
    await hass.async_block_till_done()

    rain_state = hass.states.get("sensor.rain_gauge_total_rain")
    assert rain_state is not None
    assert rain_state.state == "10.0"
    assert rain_state.attributes.get("unit_of_measurement") == "mm"
    assert rain_state.attributes.get("battery") == "OK"


async def test_sensor_force_update_same_value(hass, mock_serial_connection):
    """Test that last_updated changes even if sensor value is unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"port": "COM1"},
        options={
            "switches": {},
            "sensors": {"Oregon_0A4C": "Garden Sensor"},
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant.helpers.dispatcher import dispatcher_send

    dispatcher_send(
        hass,
        signal_update(entry.entry_id, "Oregon_0A4C"),
        {"TEMP": "00ba", "BAT": "OK"},
    )
    await hass.async_block_till_done()

    state1 = hass.states.get("sensor.garden_sensor_temperature")
    assert state1.state == "18.6"
    last_updated1 = state1.last_updated

    await asyncio.sleep(0.01)

    # Dispatch same value again
    dispatcher_send(
        hass,
        signal_update(entry.entry_id, "Oregon_0A4C"),
        {"TEMP": "00ba", "BAT": "OK"},
    )
    await hass.async_block_till_done()

    state2 = hass.states.get("sensor.garden_sensor_temperature")
    assert state2.state == "18.6"
    assert state2.last_updated > last_updated1





async def test_weather_station_fields(hass, mock_serial_connection):
    """Wind, pressure and UV fields create their own entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"port": "COM1"},
        options={"sensors": {"Oregon Wind_1A2B": "Station"}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entry.runtime_data.hub.handle_line(
        "20;40;Oregon Wind;ID=1A2B;WINSP=0064;WINGS=00c8;WINDIR=0004;BARO=03f6;UV=0005;"
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.station_wind_speed").state == "10.0"
    assert hass.states.get("sensor.station_wind_gust").state == "20.0"
    assert hass.states.get("sensor.station_wind_direction").state == "90.0"
    assert hass.states.get("sensor.station_pressure").state == "1014"
    assert hass.states.get("sensor.station_uv_index").state == "5"


async def test_sensors_follow_options_without_reload(hass, mock_serial_connection):
    """Adding and removing a sensor in the options does not reopen the port."""
    entry = MockConfigEntry(domain=DOMAIN, data={"port": "COM1"}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.01)
    opened = mock_serial_connection["serialx_open_connection"].call_count

    hass.config_entries.async_update_entry(
        entry, options={"sensors": {"Oregon_0A4C": "Garden Sensor"}}
    )
    await hass.async_block_till_done()
    entry.runtime_data.hub.handle_line("20;01;Oregon;ID=0A4C;TEMP=00ba;")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_sensor_temperature").state == "18.6"

    hass.config_entries.async_update_entry(entry, options={"sensors": {}})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_sensor_temperature") is None
    assert mock_serial_connection["serialx_open_connection"].call_count == opened
