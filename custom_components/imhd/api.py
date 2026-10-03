"""imhd.sk HTTP client, realtime socket.io feed and payload parsers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from html import unescape
import json
import logging
import math
import re
import time
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import aiohttp
import socketio
from socketio.exceptions import SocketIOError

from homeassistant.util import dt as dt_util

from .const import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    BASE_URL,
    CONNECT_TIMEOUT,
    DEPARTED_GRACE,
    HTTP_TIMEOUT,
    REJECT_BACKOFF,
    REJECT_BACKOFF_MAX,
    SECTIONS,
    SIO_PATH,
    STALE_AFTER,
    USER_AGENT,
)
from .models import Departure, StopInfo, board_url, natural_key, normalize_text

_LOGGER = logging.getLogger(__name__)

_OPTIONS_RE = re.compile(r"\$\.extend\(\s*options\s*,\s*")
_POLES_RE = re.compile(r'<select[^>]*\bid="stopPolesForRT"[^>]*>(.*?)</select>', re.S | re.I)
_OPTION_RE = re.compile(r'<option\s+value="(\d+)"[^>]*>([^<]*)', re.I)
_SEARCH_VALUE_RE = re.compile(r"^g(\d+)$")
_SECTION_PATH_RE = re.compile(r"^/([a-z]+)/")
_INFO_SPLIT_RE = re.compile(r" {10,}")

# `cack` rejection codes (texts from the imhd.sk board page).
REJECTION_CODES: dict[int, str] = {
    -10: "too many users, try later",
    -11: "too many connections",
    -12: "too many connections from this IP address",
}

# Consecutive failed connection attempts before a warning is logged.
FAILURES_BEFORE_WARNING = 3


class ImhdError(Exception):
    """Base error of the imhd.sk client."""


class ImhdConnectionError(ImhdError):
    """imhd.sk could not be reached or returned garbage."""


class ImhdStopNotFoundError(ImhdError):
    """The requested stop does not exist."""


class ImhdInvalidStopError(ImhdError):
    """The stop input (id / URL / name) could not be understood."""


# --------------------------------------------------------------------------
# Pure helpers / parsers
# --------------------------------------------------------------------------


def resolve_section(value: str | None) -> str | None:
    """Return the section code for a section code or city name."""
    if not value:
        return None
    wanted = normalize_text(str(value))
    if wanted in SECTIONS:
        return wanted
    for code, city in SECTIONS.items():
        name = normalize_text(city)
        if wanted == name or wanted in name.split("-"):
            return code
    return None


def parse_stop_input(text: str | int) -> tuple[str | None, int]:
    """Parse a stop id or an imhd.sk URL into (section, stop id).

    Accepted: "83", board URLs with `st=83` (also `st=83;84`), and stop pages
    `/ba/zastavka/<name>/<token>` whose token encodes the stop id.
    """
    raw = str(text).strip()
    if raw.isdigit():
        return None, int(raw)
    if "st=" in raw or "/zastavka/" in raw:
        url = urlparse(raw if "://" in raw else f"https://{raw}")
        match = _SECTION_PATH_RE.match(url.path)
        section = match.group(1) if match and match.group(1) in SECTIONS else None
        first = (parse_qs(url.query).get("st") or [""])[0].split(";")[0].strip()
        if first.isdigit():
            return section, int(first)
        if (stop_id := _decode_stop_token(url.path.rstrip("/").rsplit("/", 1)[-1])) is not None:
            return section, stop_id
    raise ImhdInvalidStopError(f"Not a stop id or imhd.sk URL: {raw!r}")


def _decode_stop_token(token: str) -> int | None:
    """Decode a stop page token: hex of UTF-8 JSON bytes + 0x4F, e.g. {"g":"83"}."""
    try:
        raw = bytes((byte - 0x4F) & 0xFF for byte in bytes.fromhex(token))
        value = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    stop_id = str(value.get("g", "")) if isinstance(value, dict) else ""
    return int(stop_id) if stop_id.isdigit() else None


def parse_stop_page(html: str, section: str) -> StopInfo:
    """Extract the stop description from an online departure board page."""
    decoder = json.JSONDecoder()
    for match in _OPTIONS_RE.finditer(html):
        try:
            options, _ = decoder.raw_decode(html, match.end())
        except ValueError:
            continue
        if isinstance(options, dict) and "stopId" in options:
            break
    else:
        raise ImhdStopNotFoundError("No stop description found on the board page")

    stop_id = options.get("stopId")
    if not stop_id or not str(stop_id).isdigit():
        raise ImhdStopNotFoundError("The board page does not describe a stop")
    labels = options.get("platformsLabels") or {}
    labels = {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}
    # Unlabeled platforms (most cities) are only listed in the platform picker.
    if poles := _POLES_RE.search(html):
        for platform_id, text in _OPTION_RE.findall(poles.group(1)):
            label = unescape(text).strip()
            labels.setdefault(platform_id, label if label not in ("", "*") else platform_id)
    return StopInfo(
        stop_id=int(stop_id),
        name=options.get("stopName") or str(stop_id),
        name_long=options.get("stopNameLong") or None,
        section=options.get("section") or section,
        city=options.get("currentCity") or None,
        platform_labels=labels,
    )


def parse_nearest(payload: Any, section: str, latitude: float, longitude: float) -> list[StopInfo]:
    """Parse a GetNearestStop response."""
    stops: list[StopInfo] = []
    for item in (payload or {}).get("stops") or []:
        try:
            lat, lon = float(item["lat"]), float(item["lng"])
            stop_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        labels = item.get("platform_labels") or {}
        stops.append(
            StopInfo(
                stop_id=stop_id,
                name=item.get("name") or str(stop_id),
                name_long=item.get("name_long") or None,
                section=section,
                city=item.get("city") or None,
                latitude=lat,
                longitude=lon,
                platform_labels={str(k): str(v) for k, v in labels.items()}
                if isinstance(labels, dict)
                else {},
                distance_m=round(distance_m(latitude, longitude, lat, lon)),
            )
        )
    stops.sort(key=lambda stop: stop.distance_m or 0)
    return stops


def parse_search(payload: Any, section: str) -> list[StopInfo]:
    """Parse a name search response, keeping only stops."""
    stops: list[StopInfo] = []
    seen: set[int] = set()
    for item in (payload or {}).get("results") or []:
        if item.get("class") != "stop":
            continue
        match = _SEARCH_VALUE_RE.match(str(item.get("value") or ""))
        if not match or int(match.group(1)) in seen:
            continue
        seen.add(int(match.group(1)))
        stops.append(StopInfo(stop_id=int(match.group(1)), name=item["name"], section=section))
    return stops


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = rlat2 - rlat1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(a))


def _to_datetime(value: Any) -> datetime | None:
    """Convert epoch milliseconds to an aware datetime in HA's timezone."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return dt_util.as_local(datetime.fromtimestamp(value // 1000, UTC))


def _int(value: Any) -> int | None:
    """Return value as int when it is a number (not bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return None
    return int(value)


def _flag(info: Mapping[str, Any] | None, key: str) -> bool | None:
    """Return a 0/1 vehicle flag as bool (None when unknown)."""
    if not info or info.get(key) is None:
        return None
    return bool(int(info[key])) if str(info[key]).isdigit() else bool(info[key])


def vehicle_number(issi: Any) -> str | None:
    """Return the vehicle number of an issi like "ba:4412"."""
    if issi in (None, ""):
        return None
    return str(issi).rsplit(":", 1)[-1]


def _parse_row(
    row: Mapping[str, Any],
    platform_id: str | None,
    platform_label: str,
    vehicles: Mapping[str, Mapping[str, Any]],
) -> Departure | None:
    """Parse one row of a `tabs` platform element."""
    expected = _to_datetime(row.get("cas"))
    if expected is None:
        return None
    scheduled = _to_datetime(row.get("casCP"))
    realtime = row.get("typ") == "online"
    issi = row.get("issi") if realtime else None
    # casDelta is imhd's (truncated) delay in minutes; never derive it from cas - casCP.
    delay = _int(row.get("casDelta")) if realtime else None
    info = vehicles.get(str(issi)) if issi else None
    this_idx, previous_idx = _int(row.get("tuZidx")), _int(row.get("predoslaZidx"))
    terminal = str(row.get("konecnaZstr") or "").strip()
    return Departure(
        line=str(row.get("linka") or "").strip(),
        destination=str(row.get("cielStr") or "").strip() or terminal,
        destination_city=row.get("konecnaZobec") or None,
        departure=expected,
        scheduled=scheduled,
        delay=delay,
        realtime=realtime,
        platform=platform_label,
        platform_id=platform_id,
        vehicle=vehicle_number(issi),
        low_floor=_flag(info, "lf"),
        air_conditioning=_flag(info, "ac"),
        stuck=bool(row.get("uviaznute")),
        text=row.get("odjazd"),
        trip_id=_int(row.get("i")),
        terminal=terminal or None,
        previous_stop=row.get("predoslaZstr") or None,
        stops_away=(
            this_idx - previous_idx if this_idx is not None and previous_idx is not None else None
        ),
        vehicle_type=info.get("type") if info else None,
    )


def describe_rejection(value: Any) -> str:
    """Describe a `cack` rejection like `[-12]` ("-12 too many connections from this IP")."""
    code = value[0] if isinstance(value, list) and value else value
    text = REJECTION_CODES.get(code) if isinstance(code, int) else None
    raw = json.dumps(value, ensure_ascii=False)
    return f"{code} {text}" if text else raw


def parse_info_texts(payload: Any) -> list[str]:
    """Return the non-empty, de-duplicated messages of an `iText` payload.

    imhd.sk separates language variants of one message by a run of spaces;
    they are kept in one message, one variant per line.
    """
    texts = payload if isinstance(payload, list) else [payload]
    messages = (
        "\n".join(
            cleaned for part in _INFO_SPLIT_RE.split(text) if (cleaned := " ".join(part.split()))
        )
        for text in texts
        if isinstance(text, str)
    )
    return list(dict.fromkeys(message for message in messages if message))


def iter_platform_elements(payload: Any) -> Iterable[Mapping[str, Any]]:
    """Yield the per-platform elements of a `tabs` payload (list or dict)."""
    if isinstance(payload, Mapping):
        payload = list(payload.values())
    if not isinstance(payload, list):
        return
    for element in payload:
        if isinstance(element, Mapping):
            yield element


def parse_tabs(
    payload: Any,
    platform_labels: Mapping[str, str],
    now: datetime | None = None,
    *,
    stop_id: int | None = None,
    vehicles: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Departure]:
    """Parse a `tabs` payload into departures sorted by expected time.

    With `now`, rows that departed more than the grace period ago are dropped
    and `minutes`/`leave_in` are computed.
    """
    departures: list[Departure] = []
    for element in iter_platform_elements(payload):
        stop = element.get("zastavka")
        if stop_id is not None and stop is not None and str(stop) != str(stop_id):
            continue
        raw_pid = element.get("nastupiste")
        platform_id = str(raw_pid) if raw_pid is not None else None
        label = platform_labels.get(platform_id or "") or platform_id or ""
        for row in element.get("tab") or []:
            if not isinstance(row, Mapping):
                continue
            departure = _parse_row(row, platform_id, label, vehicles or {})
            if departure is None:
                continue
            if now is not None:
                if departure.departure < now - DEPARTED_GRACE:
                    continue
                departure = departure.with_countdown(now)
            departures.append(departure)
    departures.sort(key=lambda dep: (dep.departure, natural_key(dep.line)))
    return departures


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------


class ImhdApi:
    """Small client for the imhd.sk HTTP endpoints."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize with an aiohttp session (HA's shared one)."""
        self._session = session

    async def _get(
        self, path: str, params: Mapping[str, Any] | None = None, *, as_json: bool
    ) -> Any:
        """GET a path below BASE_URL."""
        url = f"{BASE_URL}/{path}"
        headers = {"User-Agent": USER_AGENT, "Referer": f"{BASE_URL}/"}
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                async with self._session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 404:
                        raise ImhdStopNotFoundError(f"{url} returned 404")
                    resp.raise_for_status()
                    if as_json:
                        return await resp.json(content_type=None)
                    return await resp.text()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ImhdConnectionError(f"Request to {url} failed: {err}") from err

    async def async_get_stop(self, section: str, stop_id: int) -> StopInfo:
        """Fetch the description of a stop from its online board page."""
        _LOGGER.debug("Fetching stop info %s/%s", section, stop_id)
        html = await self._get(
            f"{section}/online-zastavkova-tabula", {"st": stop_id}, as_json=False
        )
        stop = parse_stop_page(html, section)
        if stop.stop_id != stop_id:
            raise ImhdStopNotFoundError(f"Stop {stop_id} not found in section {section}")
        return stop

    async def async_nearest_stops(
        self, section: str, latitude: float, longitude: float
    ) -> list[StopInfo]:
        """Return the stops nearest to a location (closest first)."""
        _LOGGER.debug("Looking up stops near %.4f,%.4f in %s", latitude, longitude, section)
        payload = await self._get(
            f"{section}/api/cepo",
            {
                "op": "GetNearestStop",
                "lat": f"{latitude:.6f}",
                "lng": f"{longitude:.6f}",
                "longName": 1,
                "ss": 1,
            },
            as_json=True,
        )
        return parse_nearest(payload, section, latitude, longitude)

    async def async_search_stops(self, section: str, query: str) -> list[StopInfo]:
        """Search stops by name (imhd.sk site search, stops only)."""
        _LOGGER.debug("Searching stops %r in %s", query, section)
        payload = await self._get(f"{section}/api/sk/vyhladavanie", {"q": query}, as_json=True)
        return parse_search(payload, section)

    async def async_find_stop_by_name(self, section: str, name: str) -> StopInfo:
        """Return the stop whose name matches `name` exactly (case/diacritics-insensitive)."""
        wanted = normalize_text(name)
        for stop in await self.async_search_stops(section, name):
            if normalize_text(stop.name) == wanted:
                return stop
        raise ImhdStopNotFoundError(f"No stop named {name!r} in section {section}")


# --------------------------------------------------------------------------
# Realtime feed
# --------------------------------------------------------------------------


class FeedListener(Protocol):
    """Receiver of realtime feed events (implemented by the coordinator)."""

    def feed_connected(self) -> None:
        """Handle a (re)established subscription."""

    def feed_disconnected(self) -> None:
        """Handle the end of a session (also a failed connection attempt)."""

    def feed_tabs(self, payload: Any) -> None:
        """Handle a `tabs` payload."""

    def feed_vehicle(self, payload: Any) -> None:
        """Handle a `vInfo` payload."""

    def feed_info(self, payload: Any) -> None:
        """Handle an `iText` payload."""

    def feed_rejected(self, reason: str) -> None:
        """Handle a rejected subscription (`cack` != true)."""


type ClientFactory = Callable[[aiohttp.ClientSession | None], Any]


def default_client_factory(session: aiohttp.ClientSession | None) -> socketio.AsyncClient:
    """Create a socket.io client that never reconnects by itself."""
    kwargs: dict[str, Any] = {
        "reconnection": False,
        "logger": False,
        "engineio_logger": False,
        "handle_sigint": False,
    }
    if session is not None:
        kwargs["http_session"] = session
    return socketio.AsyncClient(**kwargs)


class ImhdRealtimeFeed:
    """One socket.io subscription to the departure board of one stop."""

    def __init__(
        self,
        *,
        section: str,
        stop_id: int,
        listener: FeedListener,
        session: aiohttp.ClientSession | None = None,
        client_factory: ClientFactory = default_client_factory,
        stale_after: float = STALE_AFTER,
        backoff_min: float = BACKOFF_MIN,
        backoff_max: float = BACKOFF_MAX,
        reject_backoff: float = REJECT_BACKOFF,
    ) -> None:
        """Initialize the feed (call `run` in a background task)."""
        self.section = section
        self.stop_id = stop_id
        self._listener = listener
        self._session = session
        self._client_factory = client_factory
        self._stale_after = stale_after
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._reject_backoff = reject_backoff
        self._client: Any = None
        self._session_end = asyncio.Event()
        self._wake = asyncio.Event()
        self._stopped = False
        self._got_tabs = False
        self._last_tabs = 0.0
        self._rows_per_platform: dict[str, int] = {}
        self._failures = 0
        self.connected = False
        self.sessions = 0
        self.rejected: str | None = None

    @property
    def reconnects(self) -> int:
        """Return how many times the feed had to reconnect."""
        return max(0, self.sessions - 1)

    @property
    def headers(self) -> dict[str, str]:
        """Return the HTTP headers used for the websocket handshake."""
        return {
            "User-Agent": USER_AGENT,
            "Origin": BASE_URL,
            "Referer": board_url(self.section, self.stop_id),
        }

    async def run(self) -> None:
        """Keep the subscription alive until stopped (backing off on errors)."""
        backoff = self._backoff_min
        reject_backoff = self._reject_backoff
        while not self._stopped:
            self._wake.clear()
            got_tabs = await self._run_session()
            if self._stopped:
                break
            if self.rejected is not None:
                # Admission refused (e.g. too many connections): wait long.
                delay, reject_backoff = reject_backoff, min(reject_backoff * 2, REJECT_BACKOFF_MAX)
            else:
                reject_backoff = self._reject_backoff
                if got_tabs:
                    backoff = self._backoff_min
                delay, backoff = backoff, min(backoff * 2, self._backoff_max)
            if not self._wake.is_set():
                _LOGGER.debug("imhd feed %s: reconnecting in %.0f s", self.stop_id, delay)
                with suppress(TimeoutError):
                    async with asyncio.timeout(delay):
                        await self._wake.wait()
        _LOGGER.debug("imhd feed %s: stopped", self.stop_id)

    async def async_stop(self) -> None:
        """Stop the feed and close the connection."""
        self._stopped = True
        self._session_end.set()
        self._wake.set()
        if self._client is not None:
            await self._safe_disconnect(self._client)

    def request_reconnect(self) -> None:
        """Drop the current connection and reconnect immediately."""
        self.rejected = None
        self._session_end.set()
        self._wake.set()

    async def _run_session(self) -> bool:
        """Run one connection; return True when it delivered `tabs`."""
        self._session_end.clear()
        self._got_tabs = False
        self._rows_per_platform = {}
        self.sessions += 1
        client = self._client_factory(self._session)
        self._register_handlers(client)
        self._client = client
        try:
            _LOGGER.debug("imhd feed %s: connecting", self.stop_id)
            await client.connect(
                f"{BASE_URL}/",
                headers=self.headers,
                transports=["websocket"],
                socketio_path=SIO_PATH,
                wait_timeout=CONNECT_TIMEOUT,
            )
            # Ids must be ints (strings get no answer); the section is informative.
            await client.emit("tabStart", [int(self.stop_id), ["*"], self.section])
            await client.emit("infoStart")
            self._last_tabs = time.monotonic()
            self.connected = True
            if self._failures >= FAILURES_BEFORE_WARNING:
                _LOGGER.info("imhd feed %s: connection to imhd.sk restored", self.stop_id)
            self._failures = 0
            self._notify(self._listener.feed_connected)
            await self._watch()
        except (SocketIOError, aiohttp.ClientError, OSError, TimeoutError) as err:
            self._failures += 1
            log = _LOGGER.warning if self._failures == FAILURES_BEFORE_WARNING else _LOGGER.debug
            log("imhd feed %s: cannot connect to imhd.sk (%s), retrying", self.stop_id, err)
        except Exception:
            # Keep the reconnect loop alive whatever the socket library raises.
            _LOGGER.exception("imhd feed %s: unexpected error", self.stop_id)
        finally:
            self.connected = False
            self._client = None
            await self._safe_disconnect(client)
            # Always reported, also when the connection never came up.
            self._notify(self._listener.feed_disconnected)
        return self._got_tabs

    async def _watch(self) -> None:
        """Wait until the session ends or goes stale.

        Boards with departures are re-sent about every 60 s; quiet boards may
        stay silent for long, so the watchdog only runs while there are rows.
        """
        while not self._session_end.is_set():
            remaining = self._stale_after - (time.monotonic() - self._last_tabs)
            if remaining <= 0 and any(self._rows_per_platform.values()):
                _LOGGER.info(
                    "imhd feed %s: no data for %.0f s, reconnecting",
                    self.stop_id,
                    self._stale_after,
                )
                return
            with suppress(TimeoutError):
                async with asyncio.timeout(max(remaining, 1.0)):
                    await self._session_end.wait()

    def _register_handlers(self, client: Any) -> None:
        """Attach the socket.io event handlers to a client."""
        client.on("disconnect", self._on_disconnect)
        client.on("cack", self._on_cack)
        client.on("tabs", self._on_tabs)
        client.on("vInfo", self._on_vinfo)
        client.on("iText", self._on_itext)

    async def _on_disconnect(self, *args: Any) -> None:
        _LOGGER.debug("imhd feed %s: disconnected %s", self.stop_id, args)
        self._session_end.set()

    async def _on_cack(self, *args: Any) -> None:
        # Connection admission: `true`, or e.g. [-12] (too many connections from this IP).
        if args and args[0] is True:
            _LOGGER.debug("imhd feed %s: connection admitted", self.stop_id)
            self.rejected = None
            return
        self.rejected = describe_rejection(args[0] if args else None)
        self._notify(self._listener.feed_rejected, self.rejected)
        self._session_end.set()

    async def _on_tabs(self, *args: Any) -> None:
        payload = args[0] if args else []
        self._last_tabs = time.monotonic()
        self._got_tabs = True
        for element in iter_platform_elements(payload):
            self._rows_per_platform[str(element.get("nastupiste"))] = len(element.get("tab") or [])
        self._notify(self._listener.feed_tabs, payload)

    async def _on_vinfo(self, *args: Any) -> None:
        if args:
            self._notify(self._listener.feed_vehicle, args[0])

    async def _on_itext(self, *args: Any) -> None:
        self._notify(self._listener.feed_info, args[0] if args else [])

    def _notify(self, callback: Callable[..., None], *args: Any) -> None:
        """Call a listener callback, never letting it break the feed."""
        try:
            callback(*args)
        except Exception:
            _LOGGER.exception("imhd feed %s: error handling event", self.stop_id)

    async def _safe_disconnect(self, client: Any) -> None:
        """Disconnect a client, ignoring errors."""
        with suppress(Exception):
            await client.disconnect()
