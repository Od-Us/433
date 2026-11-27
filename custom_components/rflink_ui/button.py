"""Button platform for RF433Link: re-emit a stored gateway command.

Buttons are created from the signal monitor ("re-emit this signal"). Each
button sends one ``10;...;`` command and belongs either to a device already
configured in the integration or to a new "virtual remote" device.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RFLinkConfigEntry
from .const import BUTTON_UNIQUE_ID_PREFIX, DOMAIN, OPT_BUTTONS
from .entity import DeviceHandle, async_setup_dynamic_platform
from .hub import RFLinkCommandError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the command buttons."""

    def factory(button_id: str, config: Any) -> DeviceHandle | None:
        if not isinstance(config, dict) or not config.get("command"):
            return None
        return DeviceHandle([RFLinkCommandButton(entry, button_id, config)])

    await async_setup_dynamic_platform(
        hass, entry, async_add_entities, OPT_BUTTONS, factory
    )


class RFLinkCommandButton(ButtonEntity):
    """Sends a stored RFLink command when pressed."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:remote"

    def __init__(
        self, entry: RFLinkConfigEntry, button_id: str, config: dict[str, Any]
    ) -> None:
        self._entry = entry
        self._command = str(config["command"]).strip()
        self._attr_name = config.get("name") or self._command
        self._attr_unique_id = f"{BUTTON_UNIQUE_ID_PREFIX}{button_id}"
        self._attr_extra_state_attributes = {"command": self._command}

        device_id = config.get("device_id") or f"remote_{button_id}"
        if config.get("device_name"):
            # New virtual remote created with the button.
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, device_id)},
                name=config["device_name"],
                manufacturer="RFLink",
                model="Virtual remote",
                via_device=(DOMAIN, entry.entry_id),
            )
        else:
            # Device already configured: link without renaming it.
            self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    async def async_press(self) -> None:
        """Send the command."""
        try:
            await self._entry.runtime_data.hub.async_send_command(self._command)
        except RFLinkCommandError as err:
            raise HomeAssistantError(str(err)) from err
