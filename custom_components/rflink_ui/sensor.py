"""Sensor platform for RFLink UI.

Entities are created on demand: a configured sensor device gets one entity per
measurement it actually transmits (temperature, humidity, wind, rain, ...).
Entities created by previous releases are restored at startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    LIGHT_LUX,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UV_INDEX,
    UnitOfPower,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolumetricFlux,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import async_get as async_get_restore_data

from . import RFLinkConfigEntry
from .const import (
    DOMAIN,
    OPT_SENSORS,
    SENSOR_UNIQUE_ID_PREFIX,
    device_config_name,
    signal_update,
)
from .entity import DeviceHandle, RFLinkEntity, async_setup_dynamic_platform
from .protocol import FORECAST, HUMIDITY_STATUS, SENSOR_FIELDS, decode_sensor_values

_LOGGER = logging.getLogger(__name__)

# Present in recent Home Assistant releases only.
_WIND_DIRECTION_CLASS = getattr(SensorDeviceClass, "WIND_DIRECTION", None)
_ANGLE_STATE_CLASS = getattr(SensorStateClass, "MEASUREMENT_ANGLE", None)

# Sensors created unconditionally by releases <= 1.2.x.
LEGACY_KEYS = ("temperature", "humidity", "battery", "total_rain")


@dataclass(frozen=True)
class SensorSpec:
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    unit: str | None = None
    icon: str | None = None
    options: tuple[str, ...] | None = None


SENSOR_SPECS: dict[str, SensorSpec] = {
    "temperature": SensorSpec(
        SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
    ),
    "humidity": SensorSpec(SensorDeviceClass.HUMIDITY, unit=PERCENTAGE),
    "humidity_status": SensorSpec(
        SensorDeviceClass.ENUM, None, options=tuple(HUMIDITY_STATUS.values())
    ),
    "pressure": SensorSpec(
        SensorDeviceClass.ATMOSPHERIC_PRESSURE, unit=UnitOfPressure.HPA
    ),
    "forecast": SensorSpec(
        SensorDeviceClass.ENUM, None, options=tuple(FORECAST.values())
    ),
    "uv": SensorSpec(unit=UV_INDEX, icon="mdi:weather-sunny-alert"),
    "illuminance": SensorSpec(SensorDeviceClass.ILLUMINANCE, unit=LIGHT_LUX),
    "total_rain": SensorSpec(
        SensorDeviceClass.PRECIPITATION,
        SensorStateClass.TOTAL_INCREASING,
        UnitOfPrecipitationDepth.MILLIMETERS,
    ),
    "rain_rate": SensorSpec(
        SensorDeviceClass.PRECIPITATION_INTENSITY,
        unit=UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
    ),
    "wind_speed": SensorSpec(
        SensorDeviceClass.WIND_SPEED, unit=UnitOfSpeed.KILOMETERS_PER_HOUR
    ),
    "wind_average": SensorSpec(
        SensorDeviceClass.WIND_SPEED, unit=UnitOfSpeed.KILOMETERS_PER_HOUR
    ),
    "wind_gust": SensorSpec(
        SensorDeviceClass.WIND_SPEED, unit=UnitOfSpeed.KILOMETERS_PER_HOUR
    ),
    "wind_direction": SensorSpec(
        _WIND_DIRECTION_CLASS, _ANGLE_STATE_CLASS, DEGREE, icon="mdi:compass-outline"
    ),
    "wind_chill": SensorSpec(
        SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
    ),
    "wind_temperature": SensorSpec(
        SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
    ),
    "power": SensorSpec(SensorDeviceClass.POWER, unit=UnitOfPower.WATT),
    "battery": SensorSpec(state_class=None, icon="mdi:battery"),
}
assert set(SENSOR_SPECS) == {f.key for f in SENSOR_FIELDS}


def sensor_unique_id(key: str, device_id: str) -> str:
    return f"{SENSOR_UNIQUE_ID_PREFIX}{key}_{device_id}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RFLink sensor platform."""
    ent_reg = er.async_get(hass)
    try:
        last_states = async_get_restore_data(hass).last_states
    except Exception:  # noqa: BLE001 - restore data not available
        last_states = {}
    # Legacy placeholder cleanup is based on the states saved at startup, so it
    # is only applied while the platform is being set up.
    setup = {"initial": True}

    def existing_keys(device_id: str) -> list[str]:
        """Keys of entities already registered for this device."""
        keys = []
        for key in SENSOR_SPECS:
            entity_id = ent_reg.async_get_entity_id(
                "sensor", DOMAIN, sensor_unique_id(key, device_id)
            )
            if entity_id is None:
                continue
            stored = last_states.get(entity_id)
            if (
                setup["initial"]
                and key in LEGACY_KEYS
                and (
                    stored is None
                    or stored.state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
                )
            ):
                # Placeholder created by an old release for a value this device
                # never sent: drop it. It is re-created if the value shows up.
                ent_reg.async_remove(entity_id)
                continue
            keys.append(key)
        return keys

    def factory(device_id: str, config: Any) -> DeviceHandle:
        manager = SensorDeviceManager(
            hass,
            entry,
            device_id,
            device_config_name(config, device_id),
            async_add_entities,
        )
        handle = DeviceHandle(manager.entities_list)
        for key in existing_keys(device_id):
            manager.create(key)
        handle.unsubs.append(manager.start())
        return handle

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_SENSORS, factory
    )
    setup["initial"] = False


class SensorDeviceManager:
    """Routes packets of one sensor device and creates entities on demand."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RFLinkConfigEntry,
        device_id: str,
        name: str,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.device_id = device_id
        self.name = name
        self.protocol = device_id.rsplit("_", 1)[0]
        self.async_add_entities = async_add_entities
        self.entities: dict[str, RFLinkSensor] = {}
        # Shared with the DeviceHandle so that entities created later are
        # removed together with the device.
        self.entities_list: list[RFLinkSensor] = []
        self._started = False

    def create(
        self, key: str, value: Any = None, attrs: dict | None = None
    ) -> RFLinkSensor:
        entity = RFLinkSensor(self.entry, self.device_id, self.name, self.protocol, key)
        if value is not None:
            entity.set_value(value, attrs or {})
        self.entities[key] = entity
        self.entities_list.append(entity)
        return entity

    def start(self):
        self._started = True
        return async_dispatcher_connect(
            self.hass, signal_update(self.entry.entry_id, self.device_id), self._handle
        )

    @callback
    def _handle(self, data: dict[str, str]) -> None:
        values, extra = decode_sensor_values(data)
        _LOGGER.debug("Sensor %s received %s", self.device_id, values)
        attrs = dict(extra)
        if "battery" in values:
            attrs["battery"] = values["battery"]
        new: list[RFLinkSensor] = []
        for key, value in values.items():
            if key not in SENSOR_SPECS:
                continue
            if (entity := self.entities.get(key)) is None:
                new.append(self.create(key, value, attrs))
            else:
                entity.async_update_value(value, attrs)
        if new:
            _LOGGER.info(
                "RFLink sensor %s: adding %s",
                self.device_id,
                ", ".join(e.key for e in new),
            )
            self.async_add_entities(new)


class RFLinkSensor(RFLinkEntity, RestoreSensor):
    """One measurement of an RFLink sensor device."""

    _attr_force_update = True
    _subscribe_updates = False

    def __init__(
        self,
        entry: RFLinkConfigEntry,
        device_id: str,
        name: str,
        protocol: str,
        key: str,
    ) -> None:
        super().__init__(entry, device_id, name, protocol)
        self.key = key
        spec = SENSOR_SPECS[key]
        self._attr_translation_key = key
        self._attr_unique_id = sensor_unique_id(key, device_id)
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_native_unit_of_measurement = spec.unit
        if spec.icon:
            self._attr_icon = spec.icon
        if spec.options:
            self._attr_options = list(spec.options)
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self._has_value = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._has_value:
            return
        if (data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = data.native_value
        if (state := await self.async_get_last_state()) is not None:
            self._attr_extra_state_attributes = {
                k: v
                for k, v in state.attributes.items()
                if k
                not in (
                    "unit_of_measurement",
                    "device_class",
                    "state_class",
                    "friendly_name",
                    "icon",
                    "options",
                )
            }

    def set_value(self, value: Any, attrs: dict[str, Any]) -> None:
        self._attr_native_value = value
        self._attr_extra_state_attributes = {
            **self._attr_extra_state_attributes,
            **attrs,
        }
        self._has_value = True

    @callback
    def async_update_value(self, value: Any, attrs: dict[str, Any]) -> None:
        self.set_value(value, attrs)
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def icon(self) -> str | None:
        if self.key == "battery":
            return (
                "mdi:battery-alert"
                if self._attr_native_value == "LOW"
                else "mdi:battery"
            )
        return super().icon
