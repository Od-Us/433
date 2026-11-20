"""Config flow for the RFLink UI integration."""

from __future__ import annotations

import glob
import time
from typing import Any

import serialx
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_REPEATS,
    DEFAULT_REPEATS,
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
        return self.async_create_entry(title=f"RFLink ({port})", data={CONF_PORT: port})

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
        return any(device_id in self.options[key] for key in DEVICE_OPTION_KEYS)

    def _save(self) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(title="", data=self.options)

    # ------------------------------------------------------------------ menu

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        self._load_options()
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_learned", "add_manual", "modify", "remove", "settings"],
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
            elif selection != "refresh":
                name = (user_input.get("name") or "").strip()
                if not name:
                    errors["name"] = "missing_name"
                elif selection in recent:
                    self._temp_device_id = selection
                    self._temp_name = name
                    info = recent[selection]
                    device_type = info.get("type")
                    if device_type == TYPE_COVER:
                        self.options[OPT_COVERS][selection] = {
                            "name": name,
                            "inverted": False,
                        }
                        return self._save()
                    if device_type == TYPE_SWITCH:
                        return await self.async_step_select_type()
                    if device_type == TYPE_SENSOR and TRIGGER_FIELDS & set(
                        info.get("data", {})
                    ):
                        return await self.async_step_select_sensor_type()
                    self.options[OPT_SENSORS][selection] = name
                    return self._save()

        devices = {"refresh": "Refresh list", "clear": "Clear signals"}
        now = time.time()
        for dev_id, info in reversed(recent.items()):
            if self._configured(dev_id):
                continue
            age = int(now - info.get("last_seen", now))
            devices[dev_id] = f"{dev_id} ({info.get('type')}, {age}s ago)"

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
            if new_id != old_id and self._configured(new_id):
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
                self.options[key][new_id] = self.options[key].pop(old_id)
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
