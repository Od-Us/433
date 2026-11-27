"""Config flow for the RFLink UI integration."""

from __future__ import annotations

import glob
from typing import Any

import serialx
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .binary_sensor import _detect_device_class
from .const import (
    CONF_REPEATS,
    DEFAULT_REPEATS,
    DEFAULT_TITLE,
    DEVICE_OPTION_KEYS,
    DOMAIN,
    OPT_BINARY_SENSORS,
    OPT_COVERS,
    OPT_EVENTS,
    OPT_LIGHTS,
    OPT_SENSORS,
    OPT_SETTINGS,
    OPT_SWITCHES,
    SENSOR_UNIQUE_ID_PREFIX,
    UNIQUE_ID_PREFIXES,
    device_config_name,
)
from .devices import (
    CONF_ALIASES,
    LEVEL_DECODED,
    LEVEL_RAW,
    alias_candidates,
    describe_fields,
    describe_seen,
    device_aliases,
    devices_for_learned,
    id_index,
    monitor_lines,
    recent_info_for,
    with_aliases,
)
from .hub import RFLinkCommandError
from .protocol import SENSOR_FIELDS, TYPE_COVER, TYPE_SENSOR, TYPE_SWITCH

ENTER_MANUALLY = "Enter manually"

# User facing type label -> options key
TYPE_LABELS = {
    "Switch": OPT_SWITCHES,
    "Sensor": OPT_SENSORS,
    "Binary Sensor": OPT_BINARY_SENSORS,
    "Light": OPT_LIGHTS,
    "Cover": OPT_COVERS,
    "Event": OPT_EVENTS,
}
LABEL_BY_KEY = {v: k for k, v in TYPE_LABELS.items()}

# Number of "_" separated parts expected in the id of each kind.
ID_PARTS = {
    OPT_SWITCHES: (3,),
    OPT_LIGHTS: (3,),
    OPT_COVERS: (3,),
    OPT_BINARY_SENSORS: (2, 3),
    OPT_EVENTS: (2, 3),
    OPT_SENSORS: (2, 3),
}

# Fields of sensor-style packets that are really triggers (PIR, smoke, bell).
TRIGGER_FIELDS = {"PIR", "SMOKEALERT", "CHIME"}


def _valid_id(option_key: str, device_id: str) -> bool:
    parts = device_id.split("_")
    return (
        bool(device_id.strip())
        and all(parts)
        and (min(len(parts), 3) in ID_PARTS[option_key])
    )


class RFLinkTransmitterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RFLink."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return RFLinkOptionsFlowHandler()

    async def _async_try_create(
        self, port: str
    ) -> config_entries.ConfigFlowResult | None:
        self._async_abort_entries_match({CONF_PORT: port})
        try:
            await self.hass.async_add_executor_job(self._test_serial_port, port)
        except Exception:  # noqa: BLE001
            return None
        return self.async_create_entry(
            title=f"{DEFAULT_TITLE} ({port})", data={CONF_PORT: port}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input[CONF_PORT]
            if port == ENTER_MANUALLY:
                return await self.async_step_manual_port()
            if result := await self._async_try_create(port):
                return result
            errors["base"] = "cannot_connect"

        ports = await self.hass.async_add_executor_job(serialx.list_serial_ports)
        port_list = [port.device for port in ports]
        by_id = await self.hass.async_add_executor_job(glob.glob, "/dev/serial/by-id/*")
        port_list.extend(by_id)
        port_list.append(ENTER_MANUALLY)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_PORT, default=port_list[0]): vol.In(port_list)}
            ),
            errors=errors,
        )

    async def async_step_manual_port(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle manual port entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_try_create(user_input[CONF_PORT].strip()):
                return result
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="manual_port",
            data_schema=vol.Schema({vol.Required(CONF_PORT): str}),
            errors=errors,
        )

    def _test_serial_port(self, port: str) -> None:
        """Test if the serial port can be opened."""
        with serialx.serial_for_url(port, 57600, timeout=1):
            pass


class RFLinkOptionsFlowHandler(config_entries.OptionsFlow):
    """Manage the RF devices of a gateway."""

    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self._temp_device_id: str | None = None
        self._temp_name: str | None = None
        self._edit_key: str = OPT_SWITCHES
        self._monitor: dict[str, Any] = {
            "level": LEVEL_DECODED,
            "raw_mode": "off",
            "filter": "",
        }
        self._monitor_debug = False

    def _load_options(self) -> None:
        if self.options:
            return
        self.options = dict(self.config_entry.options)
        for key in (*DEVICE_OPTION_KEYS, OPT_SETTINGS):
            self.options[key] = dict(self.options.get(key, {}))

    def _hub(self):
        runtime = getattr(self.config_entry, "runtime_data", None)
        return getattr(runtime, "hub", None)

    def _configured(self, device_id: str) -> bool:
        """True if the id is used by a device, as primary or additional id."""
        return device_id in id_index(self.options)

    def _recent(self) -> dict[str, dict[str, Any]]:
        hub = self._hub()
        return dict(hub.recent_devices) if hub else {}

    def _fr(self) -> bool:
        return str(self.hass.config.language or "").lower().startswith("fr")

    def _txt(self, en: str, fr: str) -> str:
        """Text for labels built at runtime (not covered by translations)."""
        return fr if self._fr() else en

    def _fmt_ts(self, ts: float) -> str:
        """Local time of a timestamp (date added when it is not today)."""
        local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
        if local.date() == dt_util.now().date():
            return local.strftime("%H:%M:%S")
        return local.strftime("%d/%m %H:%M:%S")

    def _describe(self, info: dict[str, Any] | None) -> str | None:
        """Last values plus first / last reception time of a received id."""
        if not info:
            return None
        seen = describe_seen(
            info,
            self._fmt_ts,
            self._txt("first", "1re"),
            self._txt("last", "dern."),
        )
        return f"{describe_fields(info.get('data', {}))} — {seen}"

    def _preview_of(self, device_id: str) -> str:
        text = self._describe(recent_info_for(self._recent(), device_id))
        return text or self._txt("not received recently", "pas reçu récemment")

    def _names(self) -> dict[str, str]:
        """Name of the configured device for every id (primary and aliases)."""
        return {
            any_id: device_config_name(self.options[key][primary], primary)
            for any_id, (key, primary) in id_index(self.options).items()
        }

    def _save(self) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(title="", data=self.options)

    # ------------------------------------------------------------------ menu

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        self._load_options()
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_learned",
                "add_manual",
                "edit",
                "modify",
                "remove",
                "monitor",
                "settings",
            ],
        )

    # ------------------------------------------------------- signal monitor

    async def async_step_monitor(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the signals exchanged with the gateway. Submit to refresh."""
        hub = self._hub()
        if hub is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}

        if user_input is not None:
            self._monitor = {
                "level": user_input.get("level", LEVEL_DECODED),
                "raw_mode": user_input.get("raw_mode", "off"),
                "filter": user_input.get("filter") or "",
            }
            wanted = (
                self._monitor["raw_mode"]
                if self._monitor["level"] == LEVEL_RAW
                and self._monitor["raw_mode"] != "off"
                else None
            )
            try:
                await hub.async_set_debug_mode(wanted)
            except RFLinkCommandError:
                errors["base"] = "debug_failed"
            self._monitor_debug = self._monitor_debug or hub.debug_mode is not None

        level = self._monitor["level"]
        lines = monitor_lines(
            list(hub.history),
            level,
            self._fmt_ts,
            names=self._names(),
            text_filter=self._monitor["filter"],
            limit=40 if level == LEVEL_DECODED else 25,
        )
        block = "\n".join(lines) or self._txt(
            "(nothing received yet)", "(rien reçu pour l'instant)"
        )
        debug_labels = {
            None: self._txt("off", "désactivé"),
            "unknown": self._txt(
                "unrecognised signals (RFUDEBUG)", "signaux non reconnus (RFUDEBUG)"
            ),
            "all": self._txt("all signals (RFDEBUG)", "tous les signaux (RFDEBUG)"),
        }
        status = self._txt(
            f"Gateway {'connected' if hub.connected else 'DISCONNECTED'}"
            f" · firmware {hub.firmware or '?'} · raw pulse mode: "
            f"{debug_labels.get(hub.debug_mode)}",
            f"Passerelle {'connectée' if hub.connected else 'DÉCONNECTÉE'}"
            f" · firmware {hub.firmware or '?'} · mode impulsions brutes : "
            f"{debug_labels.get(hub.debug_mode)}",
        )

        return self.async_show_form(
            step_id="monitor",
            data_schema=vol.Schema(
                {
                    vol.Required("level", default=level): vol.In(
                        {
                            LEVEL_DECODED: self._txt(
                                "Decoded (recognised protocols)",
                                "Décodé (protocoles reconnus)",
                            ),
                            LEVEL_RAW: self._txt(
                                "Raw (every line from the gateway)",
                                "Brut (toutes les lignes de la passerelle)",
                            ),
                        }
                    ),
                    vol.Required("raw_mode", default=self._monitor["raw_mode"]): vol.In(
                        {
                            "off": self._txt(
                                "No pulse sequences", "Pas de séquences d'impulsions"
                            ),
                            "unknown": debug_labels["unknown"],
                            "all": debug_labels["all"],
                        }
                    ),
                    vol.Optional(
                        "filter",
                        description={"suggested_value": self._monitor["filter"]},
                    ): str,
                }
            ),
            description_placeholders={
                "status": status,
                "count": str(len(lines)),
                "lines": f"```\n{block}\n```",
            },
            errors=errors,
        )

    @callback
    def async_remove(self) -> None:
        """Flow closed: switch the gateway debug mode off if we enabled it."""
        hub = self._hub()
        if self._monitor_debug and hub is not None and hub.debug_mode is not None:
            self.hass.async_create_background_task(
                hub.async_disable_debug(), "rflink_ui monitor debug off"
            )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Gateway wide settings."""
        if user_input is not None:
            self.options[OPT_SETTINGS] = {CONF_REPEATS: int(user_input[CONF_REPEATS])}
            return self._save()

        current = self.options[OPT_SETTINGS].get(CONF_REPEATS, DEFAULT_REPEATS)
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REPEATS, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=10)
                    )
                }
            ),
        )

    # ------------------------------------------------------- learning mode

    async def async_step_add_learned(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add a recently seen device."""
        errors: dict[str, str] = {}
        hub = self._hub()
        recent = hub.recent_devices if hub else {}

        if user_input is not None:
            selection = user_input.get("device_id")
            if selection == "clear":
                if hub:
                    hub.recent_devices.clear()
            elif selection != "refresh" and selection in recent:
                name = (user_input.get("name") or "").strip()
                self._temp_device_id = selection
                self._temp_name = name
                info = recent[selection]
                if devices_for_learned(
                    self.options, selection, info.get("type"), info.get("protocol")
                ):
                    # Same protocol as a configured device: may be a new id of
                    # it (battery change, replaced remote...).
                    return await self.async_step_learned_target()
                if not name:
                    errors["name"] = "missing_name"
                else:
                    return await self._async_add_learned_new()

        devices = {
            "refresh": self._txt("Refresh list", "Actualiser la liste"),
            "clear": self._txt("Clear signals", "Effacer les signaux"),
        }
        for dev_id, info in reversed(recent.items()):
            if self._configured(dev_id):
                continue
            devices[dev_id] = f"{dev_id} — {self._describe(info)} [{info.get('type')}]"

        if len(devices) == 2 and not errors:
            errors["base"] = "no_devices_found"

        return self.async_show_form(
            step_id="add_learned",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id", default="refresh"): vol.In(devices),
                    vol.Optional("name"): str,
                }
            ),
            errors=errors,
        )

    async def _async_add_learned_new(self) -> config_entries.ConfigFlowResult:
        """Create a new device from the learned id in ``_temp_device_id``."""
        selection = self._temp_device_id
        info = self._recent().get(selection, {})
        device_type = info.get("type")
        if device_type == TYPE_COVER:
            self.options[OPT_COVERS][selection] = {
                "name": self._temp_name,
                "inverted": False,
            }
            return self._save()
        if device_type == TYPE_SWITCH:
            return await self.async_step_select_type()
        if device_type == TYPE_SENSOR and TRIGGER_FIELDS & set(info.get("data", {})):
            return await self.async_step_select_sensor_type()
        self.options[OPT_SENSORS][selection] = self._temp_name
        return self._save()

    async def async_step_learned_target(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """New device, or additional id of a device with the same protocol."""
        seen_id = self._temp_device_id or ""
        info = self._recent().get(seen_id, {})
        matches = devices_for_learned(
            self.options, seen_id, info.get("type"), info.get("protocol")
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            target = user_input["target"]
            if target == "new":
                self._temp_name = (user_input.get("name") or "").strip()
                if self._temp_name:
                    return await self._async_add_learned_new()
                errors["name"] = "missing_name"
            else:
                for key, device_id, alias in matches:
                    if target == f"{key}|{device_id}":
                        config = self.options[key][device_id]
                        self.options[key][device_id] = with_aliases(
                            config, [*device_aliases(config), alias]
                        )
                        return self._save()

        options = [
            SelectOptionDict(
                value="new", label=self._txt("Add as a new device", "Nouvel appareil")
            )
        ]
        for key, device_id, alias in matches:
            name = device_config_name(self.options[key][device_id], device_id)
            label = self._txt(
                f"Additional id of {name} ({LABEL_BY_KEY[key]}, {device_id})",
                f"ID supplémentaire de {name} ({LABEL_BY_KEY[key]}, {device_id})",
            )
            options.append(
                SelectOptionDict(
                    value=f"{key}|{device_id}",
                    label=f"{label} — {self._preview_of(device_id)}",
                )
            )

        return self.async_show_form(
            step_id="learned_target",
            data_schema=vol.Schema(
                {
                    vol.Required("target", default="new"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    ),
                    vol.Optional(
                        "name", description={"suggested_value": self._temp_name}
                    ): str,
                }
            ),
            description_placeholders={
                "device_id": seen_id,
                "values": self._describe(info) or "-",
            },
            errors=errors,
        )

    async def async_step_select_type(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose the entity type for a discovered command (button) device."""
        if user_input is not None:
            return await self._async_route_type(user_input["device_type"])

        return self.async_show_form(
            step_id="select_type",
            data_schema=vol.Schema(
                {
                    vol.Required("device_type", default="Switch"): vol.In(
                        ["Switch", "Binary Sensor", "Light", "Cover", "Event"]
                    )
                }
            ),
        )

    async def async_step_select_sensor_type(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose the entity type for a discovered trigger-like sensor (PIR, bell)."""
        if user_input is not None:
            return await self._async_route_type(user_input["device_type"])

        return self.async_show_form(
            step_id="select_sensor_type",
            data_schema=vol.Schema(
                {
                    vol.Required("device_type", default="Binary Sensor"): vol.In(
                        ["Binary Sensor", "Event", "Sensor"]
                    )
                }
            ),
        )

    async def _async_route_type(self, label: str) -> config_entries.ConfigFlowResult:
        key = TYPE_LABELS[label]
        if key == OPT_BINARY_SENSORS:
            return await self.async_step_binary_sensor_options()
        if key == OPT_LIGHTS:
            return await self.async_step_light_options()
        if key == OPT_COVERS:
            return await self.async_step_cover_options()
        if key == OPT_EVENTS:
            return await self.async_step_event_options()
        self.options[key][self._temp_device_id] = self._temp_name
        return self._save()

    # --------------------------------------------------------- manual entry

    async def async_step_add_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add a device manually."""
        errors: dict[str, str] = {}
        if user_input is not None:
            dev_id = user_input["device_id"].strip()
            key = TYPE_LABELS[user_input["device_type"]]
            if self._configured(dev_id):
                errors["device_id"] = "already_configured"
            elif not _valid_id(key, dev_id):
                errors["device_id"] = "invalid_device_id"
            else:
                self._temp_device_id = dev_id
                self._temp_name = user_input["name"].strip()
                return await self._async_route_type(user_input["device_type"])

        return self.async_show_form(
            step_id="add_manual",
            data_schema=vol.Schema(
                {
                    vol.Required("device_type", default="Switch"): vol.In(
                        list(TYPE_LABELS)
                    ),
                    vol.Required("device_id"): str,
                    vol.Required("name"): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------ type specifics

    async def async_step_binary_sensor_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            device_class = user_input.get("device_class")
            self.options[OPT_BINARY_SENSORS][self._temp_device_id] = {
                "name": self._temp_name,
                "device_class": device_class if device_class != "none" else None,
                "off_delay": user_input.get("off_delay") or None,
            }
            return self._save()

        device_classes = ["none"] + [c.value for c in BinarySensorDeviceClass]
        return self.async_show_form(
            step_id="binary_sensor_options",
            data_schema=vol.Schema(
                {
                    vol.Optional("device_class", default="none"): vol.In(
                        device_classes
                    ),
                    vol.Optional("off_delay"): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                }
            ),
        )

    async def async_step_light_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self.options[OPT_LIGHTS][self._temp_device_id] = {
                "name": self._temp_name,
                "type": user_input.get("type"),
            }
            return self._save()

        return self.async_show_form(
            step_id="light_options",
            data_schema=vol.Schema(
                {
                    vol.Required("type", default="dimmable"): vol.In(
                        ["dimmable", "hybrid", "switchable", "toggle"]
                    )
                }
            ),
        )

    async def async_step_cover_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self.options[OPT_COVERS][self._temp_device_id] = {
                "name": self._temp_name,
                "inverted": bool(user_input.get("inverted", False)),
            }
            return self._save()

        return self.async_show_form(
            step_id="cover_options",
            data_schema=vol.Schema({vol.Optional("inverted", default=False): bool}),
        )

    async def async_step_event_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Event: one button, or every button of the remote."""
        dev_id = self._temp_device_id or ""
        has_button = dev_id.count("_") >= 2
        if user_input is not None or not has_button:
            if user_input and user_input.get("whole_remote"):
                dev_id = dev_id.rsplit("_", 1)[0]
                if self._configured(dev_id):
                    return self.async_abort(reason="already_configured")
            self.options[OPT_EVENTS][dev_id] = {"name": self._temp_name}
            return self._save()

        return self.async_show_form(
            step_id="event_options",
            data_schema=vol.Schema({vol.Optional("whole_remote", default=False): bool}),
            description_placeholders={
                "button": dev_id.rsplit("_", 1)[1],
                "remote": dev_id.rsplit("_", 1)[0],
            },
        )

    # ------------------------------------------------------ modify / remove

    def _device_choices(self) -> dict[str, str]:
        choices: dict[str, str] = {}
        for key in DEVICE_OPTION_KEYS:
            for dev_id, config in self.options[key].items():
                choices[f"[{LABEL_BY_KEY[key]}] {dev_id}"] = (
                    f"{device_config_name(config, dev_id)} ({LABEL_BY_KEY[key]}, {dev_id})"
                )
        return choices

    @staticmethod
    def _split_choice(selection: str) -> tuple[str, str]:
        label, dev_id = selection[1:].split("] ", 1)
        return TYPE_LABELS[label], dev_id

    def _unique_ids(self, key: str, dev_id: str) -> list[tuple[str, str]]:
        """(platform, unique id) of the entities of a device."""
        if key == OPT_SENSORS:
            return [
                ("sensor", f"{SENSOR_UNIQUE_ID_PREFIX}{f.key}_{dev_id}")
                for f in SENSOR_FIELDS
            ]
        platform = {
            OPT_SWITCHES: "switch",
            OPT_BINARY_SENSORS: "binary_sensor",
            OPT_LIGHTS: "light",
            OPT_COVERS: "cover",
            OPT_EVENTS: "event",
        }[key]
        return [(platform, f"{UNIQUE_ID_PREFIXES[key]}{dev_id}")]

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose the device to edit."""
        choices = self._device_choices()
        if not choices:
            return self.async_abort(reason="no_configured_devices")
        if user_input is not None:
            self._edit_key, self._temp_device_id = self._split_choice(
                user_input["device_id"]
            )
            return await self.async_step_edit_device()
        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema({vol.Required("device_id"): vol.In(choices)}),
        )

    async def async_step_edit_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit name, additional ids and settings of a device, in place."""
        key = self._edit_key
        device_id = self._temp_device_id or ""
        config = self.options[key][device_id]
        name = device_config_name(config, device_id)
        aliases = device_aliases(config)
        errors: dict[str, str] = {}

        if user_input is not None:
            new_name = (user_input.get("name") or "").strip()
            new_aliases = [
                a.strip() for a in user_input.get(CONF_ALIASES, []) if a.strip()
            ]
            index = id_index(self.options)
            if not new_name:
                errors["name"] = "missing_name"
            for alias in new_aliases:
                if alias == device_id or not _valid_id(key, alias):
                    errors[CONF_ALIASES] = "invalid_alias"
                elif alias in index and index[alias][1] != device_id:
                    errors[CONF_ALIASES] = "alias_conflict"
            if not errors:
                new_config = self._edited_config(key, config, user_input)
                new_config["name"] = new_name
                self.options[key][device_id] = with_aliases(new_config, new_aliases)
                if new_name != name:
                    dev_reg = dr.async_get(self.hass)
                    if dev := dev_reg.async_get_device(
                        identifiers={(DOMAIN, device_id)}
                    ):
                        dev_reg.async_update_device(dev.id, name=new_name)
                return self._save()

        recent = self._recent()
        alias_options: list[SelectOptionDict] = []
        for alias in aliases:
            alias_options.append(
                SelectOptionDict(
                    value=alias, label=f"{alias} — {self._preview_of(alias)}"
                )
            )
        for alias, info in alias_candidates(recent, self.options, key, device_id):
            if alias not in aliases:
                alias_options.append(
                    SelectOptionDict(
                        value=alias, label=f"{alias} — {self._describe(info)}"
                    )
                )

        schema: dict[Any, Any] = {
            vol.Required("name", default=name): str,
            vol.Optional(CONF_ALIASES, default=aliases): SelectSelector(
                SelectSelectorConfig(
                    options=alias_options,
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        schema.update(self._settings_schema(key, device_id, config))

        return self.async_show_form(
            step_id="edit_device",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "device_id": device_id,
                "values": self._preview_of(device_id),
                "candidates": str(len(alias_options) - len(aliases)),
            },
            errors=errors,
        )

    def _settings_schema(self, key: str, device_id: str, config: Any) -> dict:
        """Type specific fields of the edit form."""
        cfg = config if isinstance(config, dict) else {}
        if key == OPT_COVERS:
            return {vol.Optional("inverted", default=bool(cfg.get("inverted"))): bool}
        if key == OPT_LIGHTS:
            return {
                vol.Required("type", default=cfg.get("type", "dimmable")): vol.In(
                    ["dimmable", "hybrid", "switchable", "toggle"]
                )
            }
        if key == OPT_BINARY_SENSORS:
            if isinstance(config, dict):
                current = cfg.get("device_class") or "none"
            else:
                detected = _detect_device_class(
                    device_id, device_config_name(config, device_id)
                )
                current = detected.value if detected else "none"
            return {
                vol.Optional("device_class", default=current): vol.In(
                    ["none"] + [c.value for c in BinarySensorDeviceClass]
                ),
                vol.Optional(
                    "off_delay", description={"suggested_value": cfg.get("off_delay")}
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
            }
        return {}

    @staticmethod
    def _edited_config(key: str, config: Any, user_input: dict[str, Any]) -> dict:
        new = dict(config) if isinstance(config, dict) else {}
        if key == OPT_COVERS:
            new["inverted"] = bool(user_input.get("inverted", False))
        elif key == OPT_LIGHTS:
            new["type"] = user_input.get("type", "dimmable")
        elif key == OPT_BINARY_SENSORS:
            device_class = user_input.get("device_class")
            new["device_class"] = device_class if device_class != "none" else None
            new["off_delay"] = user_input.get("off_delay") or None
        return new

    async def async_step_modify(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Change the RFLink id of a device, keeping its entities and history."""
        choices = self._device_choices()
        if not choices:
            return self.async_abort(reason="no_configured_devices")

        errors: dict[str, str] = {}
        if user_input is not None:
            key, old_id = self._split_choice(user_input["device_id"])
            new_id = user_input["new_device_id"].strip()
            owner = id_index(self.options).get(new_id)
            if new_id != old_id and owner is not None and owner != (key, old_id):
                errors["new_device_id"] = "already_configured"
            elif not _valid_id(key, new_id):
                errors["new_device_id"] = "invalid_device_id"
            else:
                dev_reg = dr.async_get(self.hass)
                ent_reg = er.async_get(self.hass)
                if dev := dev_reg.async_get_device(identifiers={(DOMAIN, old_id)}):
                    dev_reg.async_update_device(
                        dev.id, new_identifiers={(DOMAIN, new_id)}
                    )
                for (platform, old_uid), (_, new_uid) in zip(
                    self._unique_ids(key, old_id), self._unique_ids(key, new_id)
                ):
                    if entity_id := ent_reg.async_get_entity_id(
                        platform, DOMAIN, old_uid
                    ):
                        ent_reg.async_update_entity(entity_id, new_unique_id=new_uid)
                config = self.options[key].pop(old_id)
                if new_id in device_aliases(config):
                    # Promote an additional id to primary id.
                    config = with_aliases(
                        config, [a for a in device_aliases(config) if a != new_id]
                    )
                self.options[key][new_id] = config
                if new_id != old_id:
                    runtime = getattr(self.config_entry, "runtime_data", None)
                    if runtime is not None:
                        runtime.pending_renames[new_id] = old_id
                return self._save()

        return self.async_show_form(
            step_id="modify",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): vol.In(choices),
                    vol.Required("new_device_id"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Remove a device."""
        choices = self._device_choices()
        if not choices:
            return self.async_abort(reason="no_configured_devices")

        if user_input is not None:
            key, dev_id = self._split_choice(user_input["device_id"])
            self.options[key].pop(dev_id, None)
            return self._save()

        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema({vol.Required("device_id"): vol.In(choices)}),
        )
