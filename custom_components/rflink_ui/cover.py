"""Cover platform for RFLink UI."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RFLinkConfigEntry
from .const import OPT_COVERS, UNIQUE_ID_PREFIXES, device_config_name
from .entity import DeviceHandle, RFLinkEntity, async_setup_dynamic_platform
from .protocol import split_device_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RFLink covers."""

    def factory(device_id: str, config: Any) -> DeviceHandle:
        inverted = (
            bool(config.get("inverted", False)) if isinstance(config, dict) else False
        )
        return DeviceHandle(
            [
                RFLinkCover(
                    entry, device_id, device_config_name(config, device_id), inverted
                )
            ]
        )

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_COVERS, factory
    )


class RFLinkCover(RFLinkEntity, CoverEntity, RestoreEntity):
    """Representation of an RFLink cover (no position feedback)."""

    _attr_name = None
    _attr_force_update = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    # RFLink / Somfy RTS do not report an absolute position.
    _attr_current_cover_position = None
    _attr_is_closed = None

    def __init__(
        self, entry: RFLinkConfigEntry, device_id: str, name: str, inverted: bool
    ) -> None:
        self._protocol, self._rflink_id, self._rflink_switch = split_device_id(
            device_id
        )
        super().__init__(entry, device_id, name, self._protocol)
        self._inverted = inverted
        self._attr_unique_id = f"{UNIQUE_ID_PREFIXES[OPT_COVERS]}{device_id}"
        self._attr_is_opening = False
        self._attr_is_closing = False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        movement = (
            "opening"
            if self._attr_is_opening
            else "closing" if self._attr_is_closing else "stopped"
        )
        return {"movement_state": movement, "position_known": False}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            self._attr_is_opening = state.state == "opening"
            self._attr_is_closing = state.state == "closing"

    def _set_movement(self, opening: bool | None) -> None:
        self._attr_is_opening = opening is True
        self._attr_is_closing = opening is False

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        cmd = data.get("CMD", "").upper()
        if cmd == "UP":
            self._set_movement(not self._inverted)
        elif cmd == "DOWN":
            self._set_movement(self._inverted)
        elif cmd == "STOP":
            self._set_movement(None)
        else:
            return
        self.async_write_ha_state()

    async def _send(self, command: str) -> None:
        await self._async_send(
            self._protocol, self._rflink_id, self._rflink_switch, command
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._send("DOWN" if self._inverted else "UP")
        self._set_movement(True)
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._send("UP" if self._inverted else "DOWN")
        self._set_movement(False)
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._send("STOP")
        self._set_movement(None)
        self.async_write_ha_state()
