"""Switch platform for RFLink UI."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RFLinkConfigEntry
from .const import OPT_SWITCHES, UNIQUE_ID_PREFIXES, device_config_name
from .entity import DeviceHandle, RFLinkEntity, async_setup_dynamic_platform
from .protocol import split_device_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RFLink switch platform."""

    def factory(device_id: str, config: Any) -> DeviceHandle:
        return DeviceHandle(
            [RFLinkSwitch(entry, device_id, device_config_name(config, device_id))]
        )

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_SWITCHES, factory
    )


class RFLinkSwitch(RFLinkEntity, SwitchEntity, RestoreEntity):
    """Representation of an RFLink switch."""

    _attr_name = None
    _attr_force_update = True

    def __init__(self, entry: RFLinkConfigEntry, device_id: str, name: str) -> None:
        self._protocol, self._rflink_id, self._rflink_switch = split_device_id(
            device_id
        )
        super().__init__(entry, device_id, name, self._protocol)
        self._attr_unique_id = f"{UNIQUE_ID_PREFIXES[OPT_SWITCHES]}{device_id}"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            self._attr_is_on = state.state == STATE_ON

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        cmd = data.get("CMD", "").upper()
        _LOGGER.debug("Switch %s received %s", self._device_id, cmd)
        if cmd in ("ON", "ALLON"):
            self._attr_is_on = True
        elif cmd in ("OFF", "ALLOFF"):
            self._attr_is_on = False
        else:
            return
        self.async_write_ha_state()

    async def _async_set(self, on: bool) -> None:
        # Raises HomeAssistantError (state unchanged) if the gateway is not
        # connected or did not acknowledge the command.
        await self._async_send(
            self._protocol, self._rflink_id, self._rflink_switch, "ON" if on else "OFF"
        )
        self._attr_is_on = on
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
