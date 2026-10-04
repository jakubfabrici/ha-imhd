"""Constants for the IMHD.sk Departures integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "imhd"

BASE_URL: Final = "https://imhd.sk"
SIO_PATH: Final = "/rt/sio2"
USER_AGENT: Final = "Mozilla/5.0 (HomeAssistant imhd)"
ATTRIBUTION: Final = "Data: imhd.sk"
MANUFACTURER: Final = "imhd.sk"

# imhd.sk sections (URL prefix) -> city / region name.
SECTIONS: Final[dict[str, str]] = {
    "ba": "Bratislava",
    "bb": "Banská Bystrica",
    "hc": "Hlohovec",
    "ke": "Košice",
    "lm": "Liptovský Mikuláš",
    "mt": "Martin",
    "nr": "Nitra",
    "nz": "Nové Zámky",
    "pb": "Považská Bystrica",
    "pd": "Prievidza",
    "pn": "Piešťany",
    "po": "Prešov",
    "rk": "Ružomberok",
    "se": "Senica",
    "si": "Skalica",
    "sn": "Spišská Nová Ves",
    "tatry": "Poprad-Tatry",
    "tn": "Trenčín",
    "transport": "Slovensko a svet",
    "tt": "Trnava",
    "za": "Žilina",
    "zv": "Zvolen",
}
DEFAULT_SECTION: Final = "ba"

# Config entry data keys
CONF_SECTION: Final = "section"
CONF_STOP_ID: Final = "stop_id"
CONF_STOP_NAME: Final = "stop_name"
CONF_STOP_CITY: Final = "stop_city"
CONF_PLATFORM_LABELS: Final = "platform_labels"

# YAML / options keys
CONF_CITY: Final = "city"
CONF_STOP: Final = "stop"
CONF_PLATFORMS: Final = "platforms"
CONF_LINES: Final = "lines"
CONF_EXCLUDE_LINES: Final = "exclude_lines"
CONF_DIRECTION: Final = "direction"
CONF_MAX_DEPARTURES: Final = "max_departures"
CONF_WALKING_TIME: Final = "walking_time"
CONF_LEAVE_WINDOW: Final = "time_to_leave_window"
CONF_DEPARTURE_SENSORS: Final = "departure_sensors"

# Config flow helper keys
CONF_METHOD: Final = "method"
CONF_QUERY: Final = "query"
CONF_LOCATION: Final = "location"
METHOD_NEAREST: Final = "nearest"
METHOD_LOCATION: Final = "location"
METHOD_SEARCH: Final = "search"
METHOD_STOP_ID: Final = "stop_id"
METHODS: Final = [METHOD_NEAREST, METHOD_LOCATION, METHOD_SEARCH, METHOD_STOP_ID]

ALL_PLATFORMS: Final = "*"

DEFAULT_MAX_DEPARTURES: Final = 10
DEFAULT_WALKING_TIME: Final = 0
DEFAULT_LEAVE_WINDOW: Final = 2
DEFAULT_DEPARTURE_SENSORS: Final = 3
MAX_DEPARTURES_LIMIT: Final = 30
MAX_DEPARTURE_SENSORS: Final = 10
MAX_WALKING_TIME: Final = 120
MAX_LEAVE_WINDOW: Final = 60

DEFAULT_OPTIONS: Final[dict[str, object]] = {
    CONF_PLATFORMS: [],
    CONF_LINES: [],
    CONF_EXCLUDE_LINES: [],
    CONF_DIRECTION: [],
    CONF_MAX_DEPARTURES: DEFAULT_MAX_DEPARTURES,
    CONF_WALKING_TIME: DEFAULT_WALKING_TIME,
    CONF_LEAVE_WINDOW: DEFAULT_LEAVE_WINDOW,
    CONF_DEPARTURE_SENSORS: DEFAULT_DEPARTURE_SENSORS,
}

# Realtime feed timing
TICK_INTERVAL: Final = timedelta(seconds=30)
# Reconnect when a board with departures sent nothing for this long (quiet
# boards may legitimately stay silent; dead links are caught by the heartbeat).
STALE_AFTER: Final = 900.0
# Entities become unavailable after being disconnected for this long.
UNAVAILABLE_AFTER: Final = 300.0
# Back-off after imhd.sk refused the connection (`cack` != true).
REJECT_BACKOFF: Final = 900.0
REJECT_BACKOFF_MAX: Final = 3600.0
CONNECT_TIMEOUT: Final = 20.0
# Extra time allowed for the websocket handshake on top of CONNECT_TIMEOUT.
HANDSHAKE_TIMEOUT: Final = 10.0
# A session that lasted this long resets the reconnect back-off.
STABLE_SESSION: Final = 60.0
BACKOFF_MIN: Final = 2.0
BACKOFF_MAX: Final = 60.0
FIRST_DATA_TIMEOUT: Final = 15.0
# Connected but no `tabs` after this many seconds -> the board is empty.
EMPTY_BOARD_AFTER: Final = 6.0
# A departure is kept until its expected time has passed by more than this.
DEPARTED_GRACE: Final = timedelta(seconds=30)
# imhd.sk lists a departure whose vehicle is on its way until the vehicle leaves
# the stop ("*", seen up to 46 s past its expected time): it is kept while
# listed, but no longer than this (imhd.sk can keep resending a frozen platform).
# Checked on feed messages and the countdown tick: up to TICK_INTERVAL later.
REALTIME_DEPARTED_GRACE: Final = timedelta(seconds=90)
HTTP_TIMEOUT: Final = 15.0

# Services
SERVICE_REFRESH: Final = "refresh"
SERVICE_GET_DEPARTURES: Final = "get_departures"
SERVICE_FIND_STOPS: Final = "find_stops"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_LINE: Final = "line"
ATTR_DIRECTION: Final = "direction"
ATTR_LIMIT: Final = "limit"
ATTR_MIN_MINUTES: Final = "min_minutes"
ATTR_CITY: Final = "city"
ATTR_QUERY: Final = "query"

# Repairs issue ids
ISSUE_REJECTED: Final = "subscription_rejected"
ISSUE_YAML_REMOVED: Final = "yaml_entry_removed"

# hass.data key: unique ids confirmed by the last YAML import run
DATA_YAML_IMPORTED: Final = f"{DOMAIN}_yaml_imported"
