import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.util import dt as dt_util

try:
    from homeassistant.components.radio_frequency import (
        RadioFrequencyTransmitterEntity as RadioFrequencyEntity,
    )

    HAS_RF = True
except ImportError:
    # Fallback for HA versions before 2026.5
    from homeassistant.helpers.entity import Entity as RadioFrequencyEntity

    HAS_RF = False

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hub import RFLinkCommandError
from .protocol import build_command

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RFLink radio_frequency platform."""
    hub = entry.runtime_data.hub

    async_add_entities([RFLinkTransmitterDevice(hub, entry.entry_id, entry.title)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "send_command",
        {
            vol.Required("protocol"): cv.string,
            vol.Required("command"): cv.string,
        },
        "async_send_rf_command",
    )


class RFLinkTransmitterDevice(RadioFrequencyEntity):
    """RFLink Transmitter Entity."""

    _attr_has_entity_name = True
    _attr_name = "Transmitter"
    _attr_should_poll = False
    _attr_state = "ready"

    def __init__(self, data: Any, entry_id: str, title: str) -> None:
        """Initialize the transmitter."""
        self._data = data
        self._attr_unique_id = f"{entry_id}_rf_transmitter"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": title,
            "manufacturer": "RFLink",
            "model": "Arduino RFLink",
        }
        self._attr_supported_frequencies = [433.92]

    @property
    def supported_frequencies(self) -> list[float]:
        """Return the list of supported frequencies in MHz."""
        return self._attr_supported_frequencies

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "supported_frequencies": self.supported_frequencies,
        }

    if HAS_RF:

        @property
        def supported_frequency_ranges(self) -> list[tuple[int, int]]:
            """Return list of (min_hz, max_hz) tuples."""
            return [(433920000, 433920000)]

        async def async_send_command(self, command: Any) -> None:
            """Send an RF command."""

    async def async_send_rf_command(
        self, protocol: str, command: str, **kwargs: Any
    ) -> None:
        """Send an RF command via RFLink."""
        # RFLink serial syntax: "10;{protocol};{command};\n"
        rflink_command = build_command(protocol.strip(), command.strip().strip(";"))

        try:
            await self._data.async_send_command(rflink_command)
            if HAS_RF:
                self._RadioFrequencyTransmitterEntity__last_command_sent = (
                    dt_util.utcnow().isoformat(timespec="milliseconds")
                )
            else:
                self._attr_state = "ready"
            self.async_write_ha_state()
        except RFLinkCommandError as err:
            raise HomeAssistantError(str(err)) from err
