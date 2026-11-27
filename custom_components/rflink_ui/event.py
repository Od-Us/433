"""Event platform for RFLink UI: RF remotes, wall buttons and doorbells.

An event entity fires every time the button is pressed, which is what
automations want for remotes (a switch entity only keeps the last state and
misses repeated presses of the same button).

Two flavours:
* one button: device id ``protocol_id_switch`` (e.g. ``Kaku_41_1``);
* whole remote: device id ``protocol_id`` (e.g. ``Kaku_41``), the pressed
  button is available in the ``button`` attribute of the event.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RFLinkConfigEntry
from .const import OPT_EVENTS, UNIQUE_ID_PREFIXES, device_config_name, signal_remote
from .entity import DeviceHandle, RFLinkEntity, async_setup_dynamic_platform
from .protocol import split_device_id, split_remote_id

_LOGGER = logging.getLogger(__name__)

COMMAND_EVENTS = {
    "ON": "on",
    "OFF": "off",
    "ALLON": "all_on",
    "ALLOFF": "all_off",
    "UP": "up",
    "DOWN": "down",
    "STOP": "stop",
    "PAIR": "pair",
}
EVENT_TYPES = [*dict.fromkeys(COMMAND_EVENTS.values()), "dim", "ring", "other"]


def event_from_fields(data: dict[str, str]) -> tuple[str, dict[str, Any]] | None:
    """Translate an RFLink packet into (event type, event attributes)."""
    attrs: dict[str, Any] = {}
    if "SWITCH" in data:
        attrs["button"] = data["SWITCH"]

    if "CMD" in data:
        cmd = data["CMD"].upper()
        attrs["command"] = cmd
        if cmd in COMMAND_EVENTS:
            return COMMAND_EVENTS[cmd], attrs
        level = cmd.split("=", 1)[1] if cmd.startswith("SET_LEVEL=") else cmd
        if level.isdigit():
            attrs["level"] = int(level)
            return "dim", attrs
        return "other", attrs

    if "CHIME" in data:
        attrs["melody"] = data["CHIME"]
        return "ring", attrs
    return None


def _is_whole_remote(device_id: str) -> bool:
    try:
        split_device_id(device_id)
    except ValueError:
        return True
    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RFLink event entities."""

    def factory(device_id: str, config: Any) -> DeviceHandle:
        return DeviceHandle(
            [
                RFLinkEvent(
                    entry, device_id, device_config_name(config, device_id), config
                )
            ]
        )

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_EVENTS, factory
    )


class RFLinkEvent(RFLinkEntity, EventEntity):
    """Fires an event for every received command of an RF button or remote."""

    _attr_name = None
    _attr_translation_key = "remote"
    _attr_event_types = EVENT_TYPES
    _attr_device_class = EventDeviceClass.BUTTON

    def __init__(
        self, entry: RFLinkConfigEntry, device_id: str, name: str, config: Any = None
    ) -> None:
        self._whole_remote = _is_whole_remote(device_id)
        protocol = (
            split_remote_id(device_id)[0]
            if self._whole_remote
            else split_device_id(device_id)[0]
        )
        super().__init__(entry, device_id, name, protocol, config)
        self._attr_unique_id = self._unique_id_for(device_id)

    def _unique_id_for(self, device_id: str) -> str | None:
        return f"{UNIQUE_ID_PREFIXES[OPT_EVENTS]}{device_id}"

    def _set_device_id(self, device_id: str) -> bool:
        # Switching between one button and a whole remote re-creates the entity.
        if _is_whole_remote(device_id) != self._whole_remote:
            return False
        return super()._set_device_id(device_id)

    def _signals(self) -> list[str]:
        # Packets addressed to the id itself (one button, or sensor-style
        # packets such as doorbell CHIME frames), plus every button of each
        # remote for a whole remote.
        signals = super()._signals()
        if self._whole_remote:
            signals += [signal_remote(self._entry_id, i) for i in self._all_ids]
        return signals

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        if (event := event_from_fields(data)) is None:
            return
        event_type, attrs = event
        _LOGGER.debug("Event %s: %s %s", self._device_id, event_type, attrs)
        self._trigger_event(event_type, attrs)
        self.async_write_ha_state()
