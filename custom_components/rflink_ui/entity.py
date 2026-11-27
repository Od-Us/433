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

from .const import DOMAIN, UNIQUE_ID_PREFIXES, signal_update
from .devices import device_aliases
from .hub import RFLinkCommandError
from .protocol import build_command, split_device_id

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
        self,
        entry: RFLinkConfigEntry,
        device_id: str,
        name: str,
        model: str,
        config: Any = None,
    ) -> None:
        self._entry = entry
        self._entry_id = entry.entry_id
        self._device_id = device_id
        self._device_name = name
        self._aliases: list[str] = device_aliases(config)
        self._rflink_unsubs: list[CALLBACK_TYPE] = []
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

    @property
    def _all_ids(self) -> list[str]:
        """Primary id followed by the additional ids."""
        return [self._device_id, *self._aliases]

    def _signals(self) -> list[str]:
        """Dispatcher signals this entity listens to."""
        if not self._subscribe_updates:
            return []
        return [signal_update(self._entry_id, i) for i in self._all_ids]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resubscribe()
        self.async_on_remove(self._unsubscribe)

    @callback
    def _unsubscribe(self) -> None:
        while self._rflink_unsubs:
            self._rflink_unsubs.pop()()

    @callback
    def _resubscribe(self) -> None:
        self._unsubscribe()
        for signal in dict.fromkeys(self._signals()):
            self._rflink_unsubs.append(
                async_dispatcher_connect(self.hass, signal, self._handle_rflink_update)
            )

    # ------------------------------------------------------------ live edit

    def _unique_id_for(self, device_id: str) -> str | None:
        """Unique id of this entity for another primary id (None: unsupported)."""
        return None

    def _set_device_id(self, device_id: str) -> bool:
        """Switch to a new primary id. Return False to force a re-creation."""
        unique_id = self._unique_id_for(device_id)
        if unique_id is None:
            return False
        self._device_id = device_id
        self._attr_unique_id = unique_id
        return True

    def _apply_settings(self, config: Any) -> bool:
        """Apply type specific settings. Return False to force a re-creation."""
        return True

    @callback
    def async_apply_config(self, device_id: str, config: Any) -> bool:
        """Apply a new configuration to the live entity.

        Changing the id, the additional ids or the settings does not remove the
        entity, so its state does not go through ``unavailable`` and state
        triggers of automations are not fired.
        """
        if device_id != self._device_id and not self._set_device_id(device_id):
            return False
        if not self._apply_settings(config):
            return False
        self._aliases = device_aliases(config)
        if self.hass is not None and self.platform is not None:
            self._resubscribe()
            self.async_write_ha_state()
        return True

    @callback
    def _handle_rflink_update(self, data: dict[str, str]) -> None:
        """Handle a packet for this device (override in subclasses)."""

    async def _async_send(self, protocol: str, *args: str | int) -> None:
        """Send a frame; raise a user visible error if it cannot be delivered."""
        try:
            await self._hub.async_send_command(build_command(protocol, *args))
        except RFLinkCommandError as err:
            raise HomeAssistantError(str(err)) from err


class RFLinkCommandEntity(RFLinkEntity):
    """Entity addressed with a ``protocol_id_switch`` id (switch, light, cover)."""

    _option_key: str

    def __init__(
        self, entry: RFLinkConfigEntry, device_id: str, name: str, config: Any
    ) -> None:
        self._protocol, self._rflink_id, self._rflink_switch = split_device_id(
            device_id
        )
        super().__init__(entry, device_id, name, self._protocol, config)
        self._attr_unique_id = self._unique_id_for(device_id)

    def _unique_id_for(self, device_id: str) -> str | None:
        return f"{UNIQUE_ID_PREFIXES[self._option_key]}{device_id}"

    def _set_device_id(self, device_id: str) -> bool:
        try:
            parts = split_device_id(device_id)
        except ValueError:
            return False
        if not super()._set_device_id(device_id):
            return False
        # Commands are sent to the new primary id from now on.
        self._protocol, self._rflink_id, self._rflink_switch = parts
        return True


# ---------------------------------------------------------------------------
# Dynamic add / remove of entities when the options change
# ---------------------------------------------------------------------------


@dataclass
class DeviceHandle:
    """Entities (and extra listeners) created for one configured device."""

    entities: list[Entity]
    unsubs: list[CALLBACK_TYPE] = field(default_factory=list)
    # Applies a new (device_id, config) in place; returns False when the
    # entities must be re-created. Defaults to RFLinkEntity.async_apply_config.
    apply: Callable[[str, Any], bool] | None = None

    def apply_config(self, device_id: str, config: Any) -> bool:
        if self.apply is not None:
            return self.apply(device_id, config)
        if not self.entities or not all(
            isinstance(e, RFLinkEntity) for e in self.entities
        ):
            return False
        return all(e.async_apply_config(device_id, config) for e in self.entities)


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
            renames = entry.runtime_data.pending_renames

            # Ids changed from the options flow: move the live entities.
            for new_id, old_id in list(renames.items()):
                if old_id not in devices or new_id not in wanted or new_id in devices:
                    continue
                del renames[new_id]
                config, handle = devices.pop(old_id)
                if handle.apply_config(new_id, wanted[new_id]):
                    devices[new_id] = (wanted[new_id], handle)
                else:
                    await _remove(old_id, handle, deleted=False)

            for device_id in list(devices):
                config, handle = devices[device_id]
                if device_id in wanted:
                    if wanted[device_id] == config:
                        continue
                    # Name, additional ids or settings changed.
                    if handle.apply_config(device_id, wanted[device_id]):
                        devices[device_id] = (wanted[device_id], handle)
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
