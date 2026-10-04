"""Push-driven coordinator for one imhd.sk stop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
import logging
import math
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
    CONF_DEPARTURE_SENSORS,
    CONF_DIRECTION,
    CONF_EXCLUDE_LINES,
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_PLATFORMS,
    CONF_WALKING_TIME,
    DEFAULT_DEPARTURE_SENSORS,
    DEFAULT_MAX_DEPARTURES,
    DOMAIN,
    EMPTY_BOARD_AFTER,
    ISSUE_REJECTED,
    REALTIME_DEPARTED_GRACE,
    REJECT_BACKOFF,
    TICK_INTERVAL,
    UNAVAILABLE_AFTER,
)
from .models import Departure, StopData, StopInfo, normalize_text

_LOGGER = logging.getLogger(__name__)

# Seconds to coalesce vehicle / info updates before publishing.
PUBLISH_DELAY = 1.0
# imhd.sk sends the info texts (`iText`) right after `infoStart`, and nothing at
# all for stops without any: wait this long after a (re)connect before taking
# their absence as "no info". Until then the previous texts are kept.
INFO_GRACE = 15.0
# Now and then imhd.sk sends every platform empty and refills it within a second
# (at worst on its next 3 s tick): an emptied platform keeps its departures
# unless it stays empty this long.
EMPTY_PLATFORM_GRACE = 5.0
# A dropped connection is usually back within seconds (BACKOFF_MIN, doubled on
# each failed attempt, and a second or so to connect): vehicles at the stop are
# kept for the next session to confirm them, unless none sends departures within
# this long after the drop.
RECONNECT_GRACE = 30.0
# A due departure that went (dropped past its time or no longer listed) stays
# gone unless imhd.sk moves its time on by this much.
MOVED_ON = timedelta(minutes=1)

type ImhdConfigEntry = ConfigEntry[ImhdCoordinator]
# A departure on the board: (platform id, trip id).
type TripKey = tuple[str | None, int | None]


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


# Departure fields that feed events don't publish (the tick does).
# Countdowns change every tick, and the second-precision timestamps move with every
# prediction tweak on busy boards; the minute-precision `time`/`scheduled_time`
# fields still capture real changes. The vehicle's position changes at every stop
# it passes, every few seconds on busy boards, and no entity state depends on it.
_VOLATILE_FIELDS = frozenset(
    {"minutes", "leave_in", "text", "departure", "scheduled", "previous_stop", "stops_away"}
)


def content_key(data: StopData, shown: int | None = None) -> tuple[Any, ...]:
    """Return what identifies the published content (minute precision, no countdowns).

    Vehicle positions don't count (the tick publishes them). Only the first
    `shown` departures count: no entity shows the others. They are compared in a
    fixed order: their order follows the countdowns, which the tick publishes
    (jitter across a minute would otherwise swap rows every few seconds).
    """
    departures = tuple(
        tuple(item for item in dep.as_dict().items() if item[0] not in _VOLATILE_FIELDS)
        for dep in sorted(data.matching[:shown], key=lambda dep: dep.shown_order)
    )
    return (departures, tuple(data.info), data.info_known)


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
        # Departures shown by entities (main sensor list, departure_N sensors).
        self._shown = max(
            self.filter.max_departures,
            int(entry.options.get(CONF_DEPARTURE_SENSORS, DEFAULT_DEPARTURE_SENSORS)),
        )
        self.connected = False
        self.has_data = False
        self.rejected: str | None = None
        # When the departures (or info texts) last changed / imhd.sk last sent tabs.
        self.last_update: datetime | None = None
        self.last_message: datetime | None = None
        self.last_raw: Any = None
        self.server_time_offset: float | None = None
        self._disconnected_at: datetime | None = None
        self._rows: dict[str, Mapping[str, Any]] = {}
        self._vehicles: dict[str, Mapping[str, Any]] = {}
        self._info: list[str] = []
        self._info_known = False
        # True from a disconnect until the next session confirmed the info texts.
        self._awaiting_info = True
        self._first_result = asyncio.Event()
        self._feed = self._create_feed()
        self._feed_task: asyncio.Task[None] | None = None
        self._unsub_tick: CALLBACK_TYPE | None = None
        self._unsub_publish: CALLBACK_TYPE | None = None
        self._unsub_empty: CALLBACK_TYPE | None = None
        self._unsub_info: CALLBACK_TYPE | None = None
        # Platforms imhd.sk sent empty, still shown until EMPTY_PLATFORM_GRACE after
        # they first came empty (loop time, kept across reconnects).
        self._emptied_since: dict[str, float] = {}
        # Their empty elements from this session, with the timers ending the grace.
        self._emptied: dict[str, tuple[Mapping[str, Any], CALLBACK_TYPE]] = {}
        # Due departures: where each was when it became due, and its latest time.
        self._due: dict[TripKey, tuple[tuple[Any, ...], datetime]] = {}
        # Due departures that went, with their latest time.
        self._gone: dict[TripKey, datetime] = {}
        # True once no session sent departures for RECONNECT_GRACE after a drop:
        # nothing confirms that vehicles are still at the stop.
        self._unconfirmed = False
        self._unsub_unconfirmed: CALLBACK_TYPE | None = None
        self._content: tuple[Any, ...] | None = None
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
        for unsub in (self._unsub_tick, self._unsub_publish, self._unsub_empty, self._unsub_info):
            if unsub is not None:
                unsub()
        self._unsub_tick = self._unsub_publish = self._unsub_empty = self._unsub_info = None
        self._cancel_emptied()
        self._cancel_unconfirmed()
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
            vehicle_grace=not self._unconfirmed,
        )
        departures = self._track_due(departures, now)
        matching = self.filter.apply(departures, now)
        return StopData(
            stop=self.stop,
            departures=matching[: self.filter.max_departures],
            matching=matching,
            info=list(self._info),
            info_known=self._info_known,
            connected=self.connected,
            last_update=self.last_update,
            reconnects=self.reconnects,
        )

    def _track_due(self, departures: list[Departure], now: datetime) -> list[Departure]:
        """Return the departures without those that went, due ones kept in place.

        imhd.sk moves the prediction of a vehicle waiting at the stop on: sorted
        by it, two vehicles at the stop would swap places (N95 -> N70 -> N95). One
        moved a minute or more ahead (a late start) is sorted by it again.

        A due departure that went stays gone: listed again with its time moved on
        by less than a minute, a vehicle at the stop dropped while disconnected
        came back with the next session (Next line N95 -> N55 -> N95).
        """
        now = dt_util.as_utc(now)
        due: dict[TripKey, tuple[tuple[Any, ...], datetime]] = {}
        kept: list[Departure] = []
        for dep in departures:
            key, when = (dep.platform_id, dep.trip_id), dt_util.as_utc(dep.departure)
            if (gone := self._gone.get(key)) is not None and when < gone + MOVED_ON:
                continue
            kept.append(dep)
            if key in self._due and dep.minutes == 0:
                due[key] = (self._due[key][0], when)
            elif dep.trip_id is not None and when <= now:
                due[key] = (dep.shown_order, when)
        listed = {(dep.platform_id, dep.trip_id) for dep in kept}
        self._gone.update((key, when) for key, (_, when) in self._due.items() if key not in listed)
        # Listed again later, they would be dropped past their time anyway.
        self._gone = {
            key: when
            for key, when in self._gone.items()
            if now < when + MOVED_ON + REALTIME_DEPARTED_GRACE
        }
        self._due = due
        order = {key: place for key, (place, _when) in due.items()}
        kept.sort(
            key=lambda dep: (
                dep.minutes,
                order.get((dep.platform_id, dep.trip_id), dep.shown_order),
            )
        )
        return kept

    @callback
    def _publish(self, *, from_feed: bool = False, force: bool = False) -> None:
        """Publish a fresh snapshot.

        Feed events publish only when the shown departures or info texts changed
        (busy boards repeat identical data every few seconds and keep changing
        rows no entity shows); they also move `last_update`, as does a publish
        that takes over a pending feed publish. The 30 s tick and connection
        changes always publish.
        """
        pending, self._unsub_publish = self._unsub_publish, None
        if pending is not None:
            pending()
        data = self.build()
        content = content_key(data, self._shown)
        if content != self._content or force:
            if from_feed or pending is not None:
                self.last_update = data.last_update = dt_util.now()
        elif from_feed:
            return
        self._content = content
        self.async_set_updated_data(data)

    @callback
    def _schedule_publish(self) -> None:
        """Publish soon, coalescing bursts (vInfo arrives in batches)."""
        if self.has_data and self._unsub_publish is None:
            self._unsub_publish = async_call_later(self.hass, PUBLISH_DELAY, self._delayed_publish)

    @callback
    def _delayed_publish(self, _now: datetime) -> None:
        self._unsub_publish = None
        self._publish(from_feed=True)

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
        if self._awaiting_info and self._unsub_info is None:
            self._unsub_info = async_call_later(self.hass, INFO_GRACE, self._async_info_timeout)
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
        # Keep the info texts (no off/on flicker), the departures of platforms
        # just sent empty and the vehicles at the stop (for RECONNECT_GRACE) until
        # the next session confirms them.
        if not self._unconfirmed and self._unsub_unconfirmed is None:
            self._unsub_unconfirmed = async_call_later(
                self.hass, RECONNECT_GRACE, self._async_unconfirmed
            )
        self._awaiting_info = True
        self._cancel_info_timeout()
        self._cancel_emptied()
        # Do not keep the entry setup waiting while the feed retries.
        self._first_result.set()
        self._publish()

    @callback
    def feed_tabs(self, payload: Any) -> None:
        """Merge a `tabs` payload (one element per platform)."""
        self.last_raw = payload
        self.last_message = dt_util.now()
        # A session sending departures confirms the vehicles at the stop.
        self._unconfirmed = False
        self._cancel_unconfirmed()
        for element in iter_platform_elements(payload):
            stop, platform = element.get("zastavka"), element.get("nastupiste")
            if platform is None or (stop is not None and str(stop) != str(self.stop.stop_id)):
                continue
            if not isinstance(element.get("tab"), list):
                _LOGGER.debug("%s: ignoring malformed platform %.200r", self.name, element)
                continue
            # The server only re-sends platforms that changed.
            key = str(platform)
            previous = self._rows.get(key)
            if element["tab"] or previous is None or not previous["tab"]:
                self._emptied_since.pop(key, None)
                if (emptied := self._emptied.pop(key, None)) is not None:
                    _element, unsub = emptied
                    unsub()
                self._rows[key] = element
            elif key not in self._emptied:
                # Maybe one of imhd.sk's empty bursts: keep the departures for now.
                # A new session sending it empty again doesn't restart the grace.
                now = self.hass.loop.time()
                since = self._emptied_since.setdefault(key, now)
                unsub = async_call_later(
                    self.hass,
                    max(0.0, since + EMPTY_PLATFORM_GRACE - now),
                    partial(self._async_platform_emptied, key),
                )
                self._emptied[key] = (element, unsub)
            timestamp = element.get("timestamp")
            if (
                isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                and math.isfinite(timestamp)
            ):
                self.server_time_offset = timestamp / 1000 - dt_util.utcnow().timestamp()
        first = not self.has_data
        if first or self.rejected is not None:
            self._clear_rejection()
        self.has_data = True
        self._first_result.set()
        self._publish(from_feed=True, force=first)

    @callback
    def _async_platform_emptied(self, key: str, _now: datetime) -> None:
        """A platform stayed empty: its departures are gone.

        Platforms emptied together clear together, so no later bus is shown on the
        way: those whose grace ends before the coalesced publish clear now too.
        Their own timers, a few ms apart, would let a publish in between (the
        tick, a disconnect) show some of them cleared and the others not.
        """
        due = self.hass.loop.time() + PUBLISH_DELAY
        for other, (element, unsub) in list(self._emptied.items()):
            if other == key or self._emptied_since[other] + EMPTY_PLATFORM_GRACE <= due:
                unsub()
                del self._emptied[other], self._emptied_since[other]
                self._rows[other] = element
        self._schedule_publish()

    @callback
    def _async_unconfirmed(self, _now: datetime) -> None:
        """No session sent departures since the drop: vehicles at the stop go."""
        self._unsub_unconfirmed = None
        self._unconfirmed = True
        if self.has_data:
            self._publish()

    @callback
    def _cancel_unconfirmed(self) -> None:
        if self._unsub_unconfirmed is not None:
            self._unsub_unconfirmed()
            self._unsub_unconfirmed = None

    @callback
    def _cancel_emptied(self) -> None:
        for _element, unsub in self._emptied.values():
            unsub()
        self._emptied.clear()

    @callback
    def feed_vehicle(self, payload: Any) -> None:
        """Remember vehicle details (`vInfo`)."""
        if isinstance(payload, Mapping) and payload.get("issi"):
            self._vehicles[str(payload["issi"])] = payload
            self._schedule_publish()

    @callback
    def feed_info(self, payload: Any) -> None:
        """Store the info / disruption texts (`iText`, a full replacement)."""
        self._awaiting_info = False
        self._cancel_info_timeout()
        self._set_info(parse_info_texts(payload))

    @callback
    def _async_info_timeout(self, _now: datetime) -> None:
        """No info texts after a (re)connect: imhd.sk has none for the stop."""
        self._unsub_info = None
        if self._awaiting_info:
            self._awaiting_info = False
            self._set_info([])

    @callback
    def _cancel_info_timeout(self) -> None:
        if self._unsub_info is not None:
            self._unsub_info()
            self._unsub_info = None

    @callback
    def _set_info(self, info: list[str]) -> None:
        if info != self._info or not self._info_known:
            _LOGGER.debug("%s: info texts %s", self.name, info)
            self._info = info
            self._info_known = True
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
    def feed_admitted(self) -> None:
        """Handle an admitted connection: a previous refusal is over."""
        self._clear_rejection()

    @callback
    def _clear_rejection(self) -> None:
        """Forget a refusal (and its repairs issue); entities may become available."""
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_REJECTED}_{self.config_entry.entry_id}")
        if self.rejected is not None:
            _LOGGER.info("%s: imhd.sk accepts the connection again", self.name)
            self.rejected = None
            self.async_update_listeners()
