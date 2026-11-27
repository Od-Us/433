"""The RFLink UI integration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import serialx
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_REPEATS,
    DEFAULT_REPEATS,
    DEVICE_OPTION_KEYS,
    DOMAIN,
    EVENT_RECEIVED,
    OPT_SENSORS,
    OPT_SETTINGS,
    SENSOR_UNIQUE_ID_PREFIX,
    UNIQUE_ID_PREFIXES,
    signal_connection,
    signal_remote,
    signal_update,
)
from .hub import RFLinkHub
from .protocol import SENSOR_FIELDS, Packet

_LOGGER = logging.getLogger(__name__)

__all__ = ["DOMAIN", "RFLinkRuntimeData"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform("radio_frequency"),
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.COVER,
    Platform.EVENT,
]

SERVICE_SIMULATE_PACKET = "simulate_packet"
ATTR_PACKET = "packet"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SIMULATE_PACKET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PACKET): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


@dataclass
class RFLinkRuntimeData:
    """Runtime data stored on the config entry."""

    hub: RFLinkHub
    # Callbacks registered by the platforms to (re)synchronise their entities
    # with the options, so that adding/removing a device does not reload the
    # integration (which would close the serial port and reset the board).
    platform_syncs: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    # new id -> old id, set by the options flow when a device id is changed so
    # that the platforms move the live entities instead of re-creating them.
    pending_renames: dict[str, str] = field(default_factory=dict)


type RFLinkConfigEntry = ConfigEntry[RFLinkRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide services."""

    async def async_simulate_packet(call: ServiceCall) -> None:
        packet = call.data[ATTR_PACKET]
        wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and (wanted is None or entry.entry_id == wanted)
        ]
        if not entries:
            raise ServiceValidationError(
                "No loaded RFLink gateway matches this request"
            )
        for entry in entries:
            if entry.runtime_data.hub.handle_line(packet) is None:
                raise ServiceValidationError(f"'{packet}' is not a valid RFLink packet")

    hass.services.async_register(
        DOMAIN,
        SERVICE_SIMULATE_PACKET,
        async_simulate_packet,
        schema=SIMULATE_PACKET_SCHEMA,
    )
    return True


def _settings(entry: ConfigEntry) -> dict:
    return entry.options.get(OPT_SETTINGS, {})


async def async_setup_entry(hass: HomeAssistant, entry: RFLinkConfigEntry) -> bool:
    """Set up RFLink UI from a config entry."""
    entry_id = entry.entry_id

    @callback
    def _on_packet(packet: Packet) -> None:
        for device_id in (packet.device_id, packet.alias_id):
            if device_id:
                async_dispatcher_send(
                    hass, signal_update(entry_id, device_id), packet.fields
                )
        if packet.remote_id:
            async_dispatcher_send(
                hass, signal_remote(entry_id, packet.remote_id), packet.fields
            )
        hass.bus.async_fire(
            EVENT_RECEIVED,
            {
                ATTR_CONFIG_ENTRY_ID: entry_id,
                "device_id": packet.device_id,
                "protocol": packet.protocol,
                "type": packet.device_type,
                "fields": packet.fields,
                "packet": packet.raw,
            },
        )

    @callback
    def _on_connection(connected: bool) -> None:
        async_dispatcher_send(hass, signal_connection(entry_id), connected)

    async def _opener(port: str, baudrate: int):
        return await serialx.open_serial_connection(url=port, baudrate=baudrate)

    hub = RFLinkHub(
        entry.data[CONF_PORT],
        opener=_opener,
        on_packet=_on_packet,
        on_connection=_on_connection,
        repeats=_settings(entry).get(CONF_REPEATS, DEFAULT_REPEATS),
        create_task=lambda coro, name: entry.async_create_background_task(
            hass, coro, name
        ),
    )
    entry.runtime_data = RFLinkRuntimeData(hub=hub)

    # The gateway device must exist before devices that reference it via_device.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, entry_id)},
        name=f"RFLink ({entry.data[CONF_PORT]})",
        manufacturer="RFLink",
        model="Arduino RFLink",
    )
    async_cleanup_registries(hass, entry)

    hub.start()

    async def _stop(_: Event) -> None:
        await hub.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


def _parse_unique_id(unique_id: str) -> tuple[str, str] | None:
    """Return (options key, device id) for a device entity unique id."""
    for option_key, prefix in UNIQUE_ID_PREFIXES.items():
        if unique_id.startswith(prefix):
            return option_key, unique_id[len(prefix) :]
    if unique_id.startswith(SENSOR_UNIQUE_ID_PREFIX):
        rest = unique_id[len(SENSOR_UNIQUE_ID_PREFIX) :]
        # Longest keys first so "wind_speed" is not mistaken for another key.
        for key in sorted((f.key for f in SENSOR_FIELDS), key=len, reverse=True):
            if rest.startswith(f"{key}_"):
                return OPT_SENSORS, rest[len(key) + 1 :]
    return None


@callback
def async_cleanup_registries(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove entities and devices that are no longer in the options."""
    configured = {key: set(entry.options.get(key, {})) for key in DEVICE_OPTION_KEYS}
    all_ids = set().union(*configured.values())

    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        parsed = _parse_unique_id(ent.unique_id)
        if parsed and parsed[1] not in configured[parsed[0]]:
            ent_reg.async_remove(ent.entity_id)

    dev_reg = dr.async_get(hass)
    for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        ids = {ident for domain, ident in dev.identifiers if domain == DOMAIN}
        if ids and not ids & (all_ids | {entry.entry_id}):
            dev_reg.async_remove_device(dev.id)


async def update_listener(hass: HomeAssistant, entry: RFLinkConfigEntry) -> None:
    """Apply options changes without reconnecting to the gateway."""
    runtime = entry.runtime_data
    runtime.hub.repeats = max(
        1, int(_settings(entry).get(CONF_REPEATS, DEFAULT_REPEATS))
    )
    for sync in runtime.platform_syncs:
        await sync()
    runtime.pending_renames.clear()
    async_cleanup_registries(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: RFLinkConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.hub.stop()
    return unload_ok
