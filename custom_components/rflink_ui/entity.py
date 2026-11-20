"""Shared entity helpers for RFLink UI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, signal_update
from .hub import RFLinkCommandError
from .protocol import build_command

if TYPE_CHECKING:
    from . import RFLinkConfigEntry

_LOGGER = logging.getLogger(__name__)


class RFLinkEntity(Entity):
    """Base class for entities bound to one RF device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Subscribe to the packets of ``device_id`` (disabled for sensors, whose
    # packets are routed by a per-device manager).
    _subscribe_updates = True

    def __init__(
        self, entry: RFLinkConfigEntry, device_id: str, name: str, model: str
    ) -> None:
        self._entry = entry
        self._entry_id = entry.entry_id
        self._device_id = device_id
        self._device_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=name,
            manufacturer="RFLink",
            model=model,
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def _hub(self):
        return self._entry.runtime_data.hub

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._subscribe_updates:
            self._subscribe(signal_update(self._entry_id, self._device_id))

    def _subscribe(self, signal: str) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self._handle_rflink_update)
        )

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        """Handle a packet for this device (override in subclasses)."""

    async def _async_send(self, protocol: str, *args: str | int) -> None:
        """Send a frame; raise a user visible error if it cannot be delivered."""
        try:
            await self._hub.async_send_command(build_command(protocol, *args))
        except RFLinkCommandError as err:
            raise HomeAssistantError(str(err)) from err


# ---------------------------------------------------------------------------
# Dynamic add / remove of entities when the options change
# ---------------------------------------------------------------------------


@dataclass
class DeviceHandle:
    """Entities (and extra listeners) created for one configured device."""

    entities: list[Entity]
    unsubs: list[CALLBACK_TYPE] = field(default_factory=list)


async def async_setup_dynamic_platform(
    hass: HomeAssistant,
    entry: RFLinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
    option_key: str,
    factory: Callable[[str, Any], DeviceHandle | None],
) -> None:
    """Create entities for ``entry.options[option_key]`` and keep them in sync.

    When the options change, devices that were removed or whose settings changed
    are removed and new or changed devices are added, without reloading the
    config entry.
    """
    devices: dict[str, tuple[Any, DeviceHandle]] = {}
    lock = asyncio.Lock()
    ent_reg = er.async_get(hass)

    async def _remove(device_id: str, handle: DeviceHandle, deleted: bool) -> None:
        for unsub in handle.unsubs:
            unsub()
        for entity in handle.entities:
            if entity.hass is None or entity.platform is None:
                continue
            entity_id = (
                ent_reg.async_get_entity_id(
                    entity.platform.domain, DOMAIN, entity.unique_id
                )
                if deleted and entity.unique_id
                else None
            )
            if entity_id:
                # Device deleted: also forget the registry entry.
                ent_reg.async_remove(entity_id)
            else:
                # Settings changed or id renamed (registry already updated):
                # keep the registry entry, so the new entity reuses its entity_id.
                await entity.async_remove(force_remove=True)

    async def _sync() -> None:
        async with lock:
            wanted = dict(entry.options.get(option_key, {}))
            for device_id in list(devices):
                config, handle = devices[device_id]
                if device_id in wanted and wanted[device_id] == config:
                    continue
                del devices[device_id]
                await _remove(device_id, handle, deleted=device_id not in wanted)

            new_entities: list[Entity] = []
            for device_id, config in wanted.items():
                if device_id in devices:
                    continue
                try:
                    handle = factory(device_id, config)
                except ValueError as err:
                    _LOGGER.error("Ignoring RFLink device %s: %s", device_id, err)
                    continue
                if handle is None:
                    continue
                devices[device_id] = (config, handle)
                new_entities.extend(handle.entities)
            if new_entities:
                async_add_entities(new_entities)

    @callback
    def _cleanup() -> None:
        for _, handle in devices.values():
            for unsub in handle.unsubs:
                unsub()
        devices.clear()
        if _sync in entry.runtime_data.platform_syncs:
            entry.runtime_data.platform_syncs.remove(_sync)

    await _sync()
    entry.runtime_data.platform_syncs.append(_sync)
    entry.async_on_unload(_cleanup)
