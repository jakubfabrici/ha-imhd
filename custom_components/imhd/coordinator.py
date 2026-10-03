"""Push-driven coordinator for one imhd.sk stop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import (
    ImhdApi,
    ImhdError,
    ImhdRealtimeFeed,
    iter_platform_elements,
    parse_info_texts,
    parse_tabs,
)
from .const import (
    ALL_PLATFORMS,
    CONF_DIRECTION,
    CONF_EXCLUDE_LINES,
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_PLATFORMS,
    CONF_WALKING_TIME,
    DEFAULT_MAX_DEPARTURES,
    DOMAIN,
    EMPTY_BOARD_AFTER,
    ISSUE_REJECTED,
    REJECT_BACKOFF,
    TICK_INTERVAL,
    UNAVAILABLE_AFTER,
)
from .models import Departure, StopData, StopInfo, normalize_text

_LOGGER = logging.getLogger(__name__)

# Seconds to coalesce vehicle / info updates before publishing.
PUBLISH_DELAY = 1.0

type ImhdConfigEntry = ConfigEntry[ImhdCoordinator]


def as_list(value: Any, *, split: bool = True) -> list[str]:
    """Coerce a str / list option to a clean list of strings."""
    if value is None:
        return []
    items: Iterable[Any]
    if isinstance(value, (list, tuple, set, frozenset)):
        items = value
    elif split:
        items = str(value).split(",")
    else:
        items = [value]
    return [text for item in items if (text := str(item).strip())]


@dataclass(frozen=True, slots=True)
class DepartureFilter:
    """User filters applied to the departures of a stop."""

    platforms: frozenset[str] = frozenset()
    lines: frozenset[str] = frozenset()
    exclude_lines: frozenset[str] = frozenset()
    directions: tuple[str, ...] = ()
    walking_time: int = 0
    max_departures: int = DEFAULT_MAX_DEPARTURES

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> DepartureFilter:
        """Build the filter from config entry options."""
        platforms = {normalize_text(p) for p in as_list(options.get(CONF_PLATFORMS))}
        if ALL_PLATFORMS in platforms:
            platforms = set()
        return cls(
            platforms=frozenset(platforms),
            lines=frozenset(line.casefold() for line in as_list(options.get(CONF_LINES))),
            exclude_lines=frozenset(
                line.casefold() for line in as_list(options.get(CONF_EXCLUDE_LINES))
            ),
            directions=tuple(
                normalize_text(d) for d in as_list(options.get(CONF_DIRECTION), split=False)
            ),
            walking_time=int(options.get(CONF_WALKING_TIME) or 0),
            max_departures=int(options.get(CONF_MAX_DEPARTURES) or DEFAULT_MAX_DEPARTURES),
        )

    def matches(self, departure: Departure) -> bool:
        """Return True when a departure passes the platform/line/direction filters."""
        if self.platforms and not (
            normalize_text(departure.platform) in self.platforms
            or (departure.platform_id or "") in self.platforms
        ):
            return False
        line = departure.line.casefold()
        if self.lines and line not in self.lines:
            return False
        if line in self.exclude_lines:
            return False
        if self.directions:
            targets = normalize_text(f"{departure.destination}\n{departure.terminal or ''}")
            if not any(direction in targets for direction in self.directions):
                return False
        return True

    def apply(self, departures: Iterable[Departure], now: datetime) -> list[Departure]:
        """Filter departures and compute countdowns (not truncated)."""
        result: list[Departure] = []
        for candidate in departures:
            if not self.matches(candidate):
                continue
            departure = candidate.with_countdown(now, self.walking_time)
            if self.walking_time > 0 and departure.leave_in < 0:
                continue
            result.append(departure)
        return result


class ImhdCoordinator(DataUpdateCoordinator[StopData]):
    """Keep the realtime departures of one stop up to date."""

    config_entry: ImhdConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ImhdConfigEntry,
        api: ImhdApi,
        stop: StopInfo,
    ) -> None:
        """Initialize the coordinator (call `async_start` afterwards)."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {stop.section}/{stop.stop_id}",
            update_interval=None,
        )
        self.api = api
        self.stop = stop
        self.filter = DepartureFilter.from_options(entry.options)
        self.connected = False
        self.has_data = False
        self.rejected: str | None = None
        self.last_update: datetime | None = None
        self.last_raw: Any = None
        self.server_time_offset: float | None = None
        self._disconnected_at: datetime | None = None
        self._rows: dict[str, Mapping[str, Any]] = {}
        self._vehicles: dict[str, Mapping[str, Any]] = {}
        self._info: list[str] = []
        self._first_result = asyncio.Event()
        self._feed = self._create_feed()
        self._feed_task: asyncio.Task[None] | None = None
        self._unsub_tick: CALLBACK_TYPE | None = None
        self._unsub_publish: CALLBACK_TYPE | None = None
        self._unsub_empty: CALLBACK_TYPE | None = None
        self.data = StopData(stop=stop)

    # ------------------------------------------------------------------ setup

    def _create_feed(self) -> ImhdRealtimeFeed:
        return ImhdRealtimeFeed(
            section=self.stop.section,
            stop_id=self.stop.stop_id,
            listener=self,
            session=async_get_clientsession(self.hass),
        )

    @property
    def feed(self) -> ImhdRealtimeFeed:
        """Return the realtime feed."""
        return self._feed

    @callback
    def async_start(self) -> None:
        """Start the realtime feed and the countdown tick."""
        self._start_feed_task()
        if self._unsub_tick is None:
            self._unsub_tick = async_track_time_interval(
                self.hass, self._async_tick, TICK_INTERVAL, name=f"{self.name} tick"
            )

    @callback
    def _start_feed_task(self) -> None:
        self._feed_task = self.config_entry.async_create_background_task(
            self.hass, self._feed.run(), name=f"{self.name} realtime feed"
        )

    async def async_wait_first_result(self, max_wait: float) -> None:
        """Wait for the first departures, a rejection or a failed connection."""
        try:
            async with asyncio.timeout(max_wait):
                await self._first_result.wait()
        except TimeoutError:
            _LOGGER.debug("%s: no departures received within %.0f s", self.name, max_wait)

    async def async_shutdown(self) -> None:
        """Stop the feed and the tick (entry unload / HA stop)."""
        for unsub in (self._unsub_tick, self._unsub_publish, self._unsub_empty):
            if unsub is not None:
                unsub()
        self._unsub_tick = self._unsub_publish = self._unsub_empty = None
        await self._async_stop_feed()
        await super().async_shutdown()

    async def _async_stop_feed(self) -> None:
        await self._feed.async_stop()
        task, self._feed_task = self._feed_task, None
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=5)
            if not task.done():
                task.cancel()

    async def async_reconnect(self) -> None:
        """Re-fetch the stop description and force a fresh connection."""
        try:
            self.stop = await self.api.async_get_stop(self.stop.section, self.stop.stop_id)
        except ImhdError as err:
            _LOGGER.debug("%s: could not refresh stop info: %s", self.name, err)
        self._clear_rejection()
        if self._feed_task is None or self._feed_task.done():
            self._feed = self._create_feed()
            self._start_feed_task()
        else:
            self._feed.request_reconnect()

    # --------------------------------------------------------------- state

    @property
    def available(self) -> bool:
        """Return True while the published departures can be trusted."""
        if not self.has_data or self.rejected is not None:
            return False
        if self.connected or self._disconnected_at is None:
            return True
        return dt_util.utcnow() - self._disconnected_at < timedelta(seconds=UNAVAILABLE_AFTER)

    @property
    def platforms(self) -> list[str]:
        """Return the platforms covered (configured filter or all of the stop)."""
        configured = as_list(self.config_entry.options.get(CONF_PLATFORMS))
        if configured and ALL_PLATFORMS not in configured:
            return configured
        return self.stop.platforms

    @property
    def reconnects(self) -> int:
        """Return how many times the feed reconnected."""
        return self._feed.reconnects

    @property
    def vehicles(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the vehicle details received so far."""
        return self._vehicles

    def build(self, now: datetime | None = None) -> StopData:
        """Build a fresh snapshot (filters applied, countdowns for `now`)."""
        now = now or dt_util.now()
        departures = parse_tabs(
            list(self._rows.values()),
            self.stop.platform_labels,
            now,
            stop_id=self.stop.stop_id,
            vehicles=self._vehicles,
        )
        matching = self.filter.apply(departures, now)
        return StopData(
            stop=self.stop,
            departures=matching[: self.filter.max_departures],
            matching=matching,
            info=list(self._info),
            connected=self.connected,
            last_update=self.last_update,
            reconnects=self.reconnects,
        )

    @callback
    def _publish(self) -> None:
        if self._unsub_publish is not None:
            self._unsub_publish()
            self._unsub_publish = None
        self.async_set_updated_data(self.build())

    @callback
    def _schedule_publish(self) -> None:
        """Publish soon, coalescing bursts (vInfo arrives in batches)."""
        if self.has_data and self._unsub_publish is None:
            self._unsub_publish = async_call_later(self.hass, PUBLISH_DELAY, self._delayed_publish)

    @callback
    def _delayed_publish(self, _now: datetime) -> None:
        self._unsub_publish = None
        self._publish()

    async def _async_update_data(self) -> StopData:
        """Recompute the snapshot (used by `homeassistant.update_entity`)."""
        return self.build()

    @callback
    def _async_tick(self, _now: datetime) -> None:
        """Recompute countdowns and drop departed rows."""
        if self.has_data:
            self._publish()

    # --------------------------------------------------- feed listener API

    @callback
    def feed_connected(self) -> None:
        """Handle a (re)established subscription."""
        _LOGGER.debug("%s: connected", self.name)
        self.connected = True
        self._disconnected_at = None
        if not self.has_data and self._unsub_empty is None:
            self._unsub_empty = async_call_later(
                self.hass, EMPTY_BOARD_AFTER, self._async_assume_empty_board
            )
        self._publish()

    @callback
    def _async_assume_empty_board(self, _now: datetime) -> None:
        """Quiet stops may send no `tabs` at all: treat that as no departures."""
        self._unsub_empty = None
        if self.connected and not self.has_data:
            _LOGGER.debug("%s: no departures received, assuming an empty board", self.name)
            self.has_data = True
            self._first_result.set()
            self._publish()

    @callback
    def feed_disconnected(self) -> None:
        """Handle the end of a session (also a failed connection attempt)."""
        _LOGGER.debug("%s: disconnected", self.name)
        self.connected = False
        if self._disconnected_at is None:
            self._disconnected_at = dt_util.utcnow()
        # Do not keep the entry setup waiting while the feed retries.
        self._first_result.set()
        self._publish()

    @callback
    def feed_tabs(self, payload: Any) -> None:
        """Merge a `tabs` payload (one element per platform)."""
        self.last_raw = payload
        for element in iter_platform_elements(payload):
            stop, platform = element.get("zastavka"), element.get("nastupiste")
            if platform is None or (stop is not None and str(stop) != str(self.stop.stop_id)):
                continue
            # The server only re-sends platforms that changed.
            self._rows[str(platform)] = element
            if isinstance(timestamp := element.get("timestamp"), (int, float)):
                self.server_time_offset = timestamp / 1000 - dt_util.utcnow().timestamp()
        if not self.has_data or self.rejected is not None:
            self._clear_rejection()
        self.last_update = dt_util.now()
        self.has_data = True
        self._first_result.set()
        self._publish()

    @callback
    def feed_vehicle(self, payload: Any) -> None:
        """Remember vehicle details (`vInfo`)."""
        if isinstance(payload, Mapping) and payload.get("issi"):
            self._vehicles[str(payload["issi"])] = payload
            self._schedule_publish()

    @callback
    def feed_info(self, payload: Any) -> None:
        """Store the info / disruption texts (`iText`)."""
        info = parse_info_texts(payload)
        if info != self._info:
            self._info = info
            self._schedule_publish()

    @callback
    def feed_rejected(self, reason: str) -> None:
        """Handle a subscription rejected by imhd.sk."""
        _LOGGER.warning(
            "imhd.sk refused the realtime connection for stop %s (%s/%s): %s. "
            "Retrying in %.0f minutes or more; call imhd.refresh to retry now",
            self.stop.name,
            self.stop.section,
            self.stop.stop_id,
            reason,
            REJECT_BACKOFF / 60,
        )
        self.rejected = reason
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_REJECTED}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_REJECTED,
            translation_placeholders={
                "name": self.config_entry.title,
                "stop_id": str(self.stop.stop_id),
                "reason": reason,
            },
        )
        self._first_result.set()
        self.async_update_listeners()

    @callback
    def _clear_rejection(self) -> None:
        self.rejected = None
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_REJECTED}_{self.config_entry.entry_id}")
