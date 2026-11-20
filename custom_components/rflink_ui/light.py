"""Light platform for RFLink UI."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RFLinkConfigEntry
from .const import OPT_LIGHTS, UNIQUE_ID_PREFIXES, device_config_name
from .entity import DeviceHandle, RFLinkEntity, async_setup_dynamic_platform
from .protocol import split_device_id

_LOGGER = logging.getLogger(__name__)

LIGHT_TYPES = ("dimmable", "hybrid", "switchable", "toggle")
DIMMABLE_TYPES = ("dimmable", "hybrid")
MAX_LEVEL = 15


def brightness_to_level(brightness: int) -> int:
    """Map HA brightness 1..255 to RFLink level 1..15 (never 0 = off)."""
    return max(1, min(MAX_LEVEL, round(brightness * MAX_LEVEL / 255)))


def level_to_brightness(level: int) -> int:
    """Map RFLink level 0..15 to HA brightness."""
    return max(0, min(255, round(level * 255 / MAX_LEVEL)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RFLink light platform."""

    def factory(device_id: str, config: Any) -> DeviceHandle:
        light_type = (
            config.get("type", "dimmable") if isinstance(config, dict) else "dimmable"
        )
        return DeviceHandle(
            [
                RFLinkLight(
                    entry, device_id, device_config_name(config, device_id), light_type
                )
            ]
        )

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_LIGHTS, factory
    )


class RFLinkLight(RFLinkEntity, LightEntity, RestoreEntity):
    """Representation of an RFLink light."""

    _attr_name = None

    def __init__(
        self, entry: RFLinkConfigEntry, device_id: str, name: str, light_type: str
    ) -> None:
        self._protocol, self._rflink_id, self._rflink_switch = split_device_id(
            device_id
        )
        super().__init__(entry, device_id, name, self._protocol)
        self._light_type = light_type if light_type in LIGHT_TYPES else "dimmable"
        self._attr_unique_id = f"{UNIQUE_ID_PREFIXES[OPT_LIGHTS]}{device_id}"
        self._attr_is_on = False
        if self._light_type in DIMMABLE_TYPES:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_brightness = 255
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_brightness = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            self._attr_is_on = state.state == STATE_ON
            if self._light_type in DIMMABLE_TYPES:
                try:
                    self._attr_brightness = int(state.attributes[ATTR_BRIGHTNESS])
                except (KeyError, TypeError, ValueError):
                    pass

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        cmd = data.get("CMD", "").upper()
        _LOGGER.debug("Light %s received %s", self._device_id, cmd)

        if self._light_type == "toggle":
            if cmd != "ON":
                return
            self._attr_is_on = not self._attr_is_on
            self.async_write_ha_state()
            return

        level: int | None = None
        if cmd.startswith("SET_LEVEL="):
            try:
                level = int(cmd.split("=", 1)[1])
            except ValueError:
                return
        elif cmd.isdigit():
            level = int(cmd)

        if level is not None:
            if self._light_type in DIMMABLE_TYPES and level > 0:
                self._attr_brightness = level_to_brightness(level)
            self._attr_is_on = level > 0
        elif cmd in ("ON", "ALLON"):
            self._attr_is_on = True
        elif cmd in ("OFF", "ALLOFF"):
            self._attr_is_on = False
        else:
            return
        self.async_write_ha_state()

    async def _send(self, value: str | int) -> None:
        await self._async_send(
            self._protocol, self._rflink_id, self._rflink_switch, value
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._light_type == "toggle":
            await self._send("ON")
            self._attr_is_on = not self._attr_is_on
            self.async_write_ha_state()
            return

        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None and self._light_type in DIMMABLE_TYPES:
            level = brightness_to_level(brightness)
            await self._send(level)
            self._attr_brightness = level_to_brightness(level)

        if brightness is None or self._light_type in ("hybrid", "switchable"):
            await self._send("ON")

        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._light_type == "toggle":
            await self._send("ON")
            self._attr_is_on = not self._attr_is_on
        else:
            await self._send("OFF")
            self._attr_is_on = False
        self.async_write_ha_state()
