"""Constants for the RFLink UI integration."""

DOMAIN = "rflink_ui"
# Displayed name (the technical domain stays rflink_ui).
DEFAULT_TITLE = "RF433Link"

# Options keys holding the configured devices, and the platform of each.
OPT_SWITCHES = "switches"
OPT_SENSORS = "sensors"
OPT_BINARY_SENSORS = "binary_sensors"
OPT_LIGHTS = "lights"
OPT_COVERS = "covers"
OPT_EVENTS = "events"
OPT_SETTINGS = "settings"

DEVICE_OPTION_KEYS = (
    OPT_SWITCHES,
    OPT_SENSORS,
    OPT_BINARY_SENSORS,
    OPT_LIGHTS,
    OPT_COVERS,
    OPT_EVENTS,
)

CONF_REPEATS = "repeats"
DEFAULT_REPEATS = 1

# Unique id prefix of each entity kind (kept identical to previous releases so
# existing entities keep their history and customisations).
UNIQUE_ID_PREFIXES = {
    OPT_SWITCHES: "rflink_switch_",
    OPT_BINARY_SENSORS: "rflink_binary_sensor_",
    OPT_LIGHTS: "rflink_light_",
    OPT_COVERS: "rflink_cover_",
    OPT_EVENTS: "rflink_event_",
}
SENSOR_UNIQUE_ID_PREFIX = "rflink_sensor_"

EVENT_RECEIVED = f"{DOMAIN}_received"


def signal_update(entry_id: str, device_id: str) -> str:
    """Dispatcher signal for packets of one device on one gateway."""
    return f"{DOMAIN}_{entry_id}_update_{device_id}"


def signal_remote(entry_id: str, remote_id: str) -> str:
    """Dispatcher signal for all command packets of one remote (any button)."""
    return f"{DOMAIN}_{entry_id}_remote_{remote_id}"


def signal_connection(entry_id: str) -> str:
    """Dispatcher signal for gateway connection changes."""
    return f"{DOMAIN}_{entry_id}_connection"


def device_config_name(config: object, default: str) -> str:
    """Return the name stored for a device (str or dict config)."""
    if isinstance(config, dict):
        return str(config.get("name") or default)
    return str(config or default)
