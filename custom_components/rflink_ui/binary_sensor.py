"""Binary sensor platform for RFLink UI."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from . import RFLinkConfigEntry
from .const import (
    DOMAIN,
    OPT_BINARY_SENSORS,
    UNIQUE_ID_PREFIXES,
    device_config_name,
    signal_connection,
)
from .entity import DeviceHandle, RFLinkEntity, async_setup_dynamic_platform

_LOGGER = logging.getLogger(__name__)

ON_VALUES = {"ON", "ALLON", "OPEN", "MOTION", "ALARM"}
OFF_VALUES = {"OFF", "ALLOFF", "CLOSE", "CLOSED", "OK"}
# Sensor-style fields some protocols use instead of CMD.
STATE_FIELDS = ("CMD", "PIR", "SMOKEALERT")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RFLink binary sensor platform."""
    async_add_entities([RFLinkConnectionSensor(entry)])

    def factory(device_id: str, config: Any) -> DeviceHandle:
        return DeviceHandle([RFLinkBinarySensor(entry, device_id, config)])

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_BINARY_SENSORS, factory
    )


class RFLinkConnectionSensor(BinarySensorEntity):
    """Gateway connection status."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connection status"

    def __init__(self, entry: RFLinkConfigEntry) -> None:
        self._entry = entry
        port = entry.data.get("port", "RFLink")
        self._attr_unique_id = f"rflink_connection_{port}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hub = self._entry.runtime_data.hub
        return {"port": hub.port, "firmware": hub.firmware, **hub.stats}

    async def async_added_to_hass(self) -> None:
        self._attr_is_on = self._entry.runtime_data.hub.connected
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_connection(self._entry.entry_id),
                self._handle_connection,
            )
        )

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self._attr_is_on = connected
        self.async_write_ha_state()


class RFLinkBinarySensor(RFLinkEntity, BinarySensorEntity, RestoreEntity):
    """An RF binary sensor (PIR, door contact, smoke detector, ...)."""

    _attr_name = None
    _attr_force_update = True

    def __init__(
        self, entry: RFLinkConfigEntry, device_id: str, config: dict | str
    ) -> None:
        name = device_config_name(config, device_id)
        protocol = (
            device_id.rsplit("_", 2)[0] if device_id.count("_") >= 2 else "Unknown"
        )
        super().__init__(entry, device_id, name, protocol)
        self._attr_unique_id = f"{UNIQUE_ID_PREFIXES[OPT_BINARY_SENSORS]}{device_id}"
        self._attr_is_on = False
        self._delay_listener: CALLBACK_TYPE | None = None

        if isinstance(config, dict):
            self._off_delay = config.get("off_delay")
            device_class = config.get("device_class")
            self._attr_device_class = (
                BinarySensorDeviceClass(device_class)
                if device_class and device_class != "none"
                else None
            )
        else:
            self._off_delay = None
            self._attr_device_class = _detect_device_class(device_id, name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            self._attr_is_on = state.state == STATE_ON

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_delay()
        await super().async_will_remove_from_hass()

    def _cancel_delay(self) -> None:
        if self._delay_listener:
            self._delay_listener()
            self._delay_listener = None

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        value = next((data[f].upper() for f in STATE_FIELDS if f in data), "")
        _LOGGER.debug("Binary sensor %s received %s", self._device_id, value)
        if value in ON_VALUES:
            self._attr_is_on = True
        elif value in OFF_VALUES:
            self._attr_is_on = False
        else:
            return

        self._cancel_delay()
        if self._off_delay and self._attr_is_on:

            @callback
            def _off(_now) -> None:
                self._delay_listener = None
                self._attr_is_on = False
                self.async_write_ha_state()

            self._delay_listener = async_call_later(self.hass, self._off_delay, _off)

        self.async_write_ha_state()


def _detect_device_class(device_id: str, name: str) -> BinarySensorDeviceClass | None:
    """Guess a device class for legacy (string) configurations."""
    text = f"{name} {device_id}".lower()
    words = set(text.replace("_", " ").split())
    if any(k in text for k in ("motion", "pir", "detect")):
        return BinarySensorDeviceClass.MOTION
    if any(k in text for k in ("door", "window", "opening")):
        return BinarySensorDeviceClass.OPENING
    if "smoke" in text or "gas" in text or "co" in words:
        return BinarySensorDeviceClass.SMOKE
    if any(k in text for k in ("moisture", "water", "flood", "leak")):
        return BinarySensorDeviceClass.MOISTURE
    if "connectivity" in text or "ping" in text:
        return BinarySensorDeviceClass.CONNECTIVITY
    return None
