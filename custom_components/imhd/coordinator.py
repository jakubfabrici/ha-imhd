"""Push-driven coordinator for one imhd.sk stop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
import heapq
from itertools import islice
import logging
import math
import random
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
    CONF_SECTION,
    CONF_STOP_ID,
    CONF_TIMETABLE,
    CONF_WALKING_TIME,
    DEFAULT_DEPARTURE_SENSORS,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_TIMETABLE,
    DOMAIN,
    EMPTY_BOARD_AFTER,
    IMHD_TIME_ZONE,
    ISSUE_REJECTED,
    REALTIME_DEPARTED_GRACE,
    REJECT_BACKOFF,
    SOURCE_TIMETABLE,
    TICK_INTERVAL,
    TIMETABLE_JITTER,
    TIMETABLE_MIN_INTERVAL,
    TIMETABLE_START_DELAY,
    UNAVAILABLE_AFTER,
)
from .models import Departure, StopData, StopInfo, normalize_text
from .timetable import DATA_TIMETABLES, Timetable, TimetableCache

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


def timetable_key(entry: ConfigEntry) -> tuple[str, int]:
    """Return the key of the entry's stop in the shared scheduled departures."""
    return entry.data[CONF_SECTION], int(entry.data[CONF_STOP_ID])


@callback
def async_release_timetables(hass: HomeAssistant) -> None:
    """Drop the pages of stops no (enabled) entry shows scheduled departures for."""
    wanted = {
        timetable_key(entry)
        for entry in hass.config_entries.async_entries(
            DOMAIN, include_ignore=False, include_disabled=False
        )
        if entry.options.get(CONF_TIMETABLE, DEFAULT_TIMETABLE)
    }
    for key, cache in hass.data.get(DATA_TIMETABLES, {}).items():
        if key not in wanted:
            cache.release()


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
        """Return True when a departure passes the platform/line/direction filters.

        A scheduled departure whose platform id isn't known yet carries the
        page's own label, which isn't always the board's: it doesn't pass a
        platform filter.
        """
        if self.platforms and not (
            (departure.platform_id or "") in self.platforms
            or (
                normalize_text(departure.platform) in self.platforms
                and (departure.platform_id is not None or departure.source != SOURCE_TIMETABLE)
            )
        ):
            return False
        line = departure.line.casefold()
        if self.lines and line not in self.lines:
            return False
        if line in self.exclude_lines:
            return False
        if self.directions:
            targets = departure.direction_text
            if not any(direction in targets for direction in self.directions):
                return False
        return True

    def apply(
        self,
        departures: Iterable[Departure],
        now: datetime,
        limit: int | None = None,
        *,
        keep: Callable[[Departure], bool] | None = None,
        min_minutes: int = 0,
    ) -> list[Departure]:
        """Filter departures and compute countdowns (the first `limit` that pass).

        `keep` and `min_minutes` narrow them further (imhd.get_departures). No
        departure after the last one returned is looked at.
        """
        return list(islice(self._passing(departures, now, keep, min_minutes), limit))

    def _passing(
        self,
        departures: Iterable[Departure],
        now: datetime,
        keep: Callable[[Departure], bool] | None,
        min_minutes: int,
    ) -> Iterator[Departure]:
        for candidate in departures:
            if not self.matches(candidate) or (keep is not None and not keep(candidate)):
                continue
            departure = candidate.with_countdown(now, self.walking_time)
            if (self.walking_time > 0 and departure.leave_in < 0) or (
                departure.minutes < min_minutes
            ):
                continue
            yield departure


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
        # Scheduled departures: the pages and their merge, shared by the entries of
        # the stop (and kept across reloads).
        self.timetable_cache: TimetableCache | None = None
        if entry.options.get(CONF_TIMETABLE, DEFAULT_TIMETABLE):
            self.timetable_cache = hass.data.setdefault(DATA_TIMETABLES, {}).setdefault(
                timetable_key(entry), TimetableCache()
            )
        self._timetable_task: asyncio.Task[None] | None = None
        self._unsub_timetable: CALLBACK_TYPE | None = None
        # When the timer fetches the pages next (none while the refresh is paused).
        self.timetable_due: datetime | None = None
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
        """Start the realtime feed, the countdown tick and the scheduled departures."""
        if (cache := self.timetable_cache) is not None and cache.timetable is None:
            # Before the first page too: it remembers the departures the feed lists.
            cache.timetable = Timetable()
        self._start_feed_task()
        if self._unsub_tick is None:
            self._unsub_tick = async_track_time_interval(
                self.hass, self._async_tick, TICK_INTERVAL, name=f"{self.name} tick"
            )
        if cache is not None and self._unsub_timetable is None:
            if not cache.pages:
                # The realtime feed first.
                self._schedule_timetable(random.uniform(*TIMETABLE_START_DELAY))  # noqa: S311
            else:
                # Used before (by another entry of the stop, or before a reload).
                self._schedule_timetable(self._timetable_delay())

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
        """Stop the feed, the tick and the scheduled departures (entry unload / HA stop)."""
        for unsub in (
            self._unsub_tick,
            self._unsub_publish,
            self._unsub_empty,
            self._unsub_info,
            self._unsub_timetable,
        ):
            if unsub is not None:
                unsub()
        self._unsub_tick = self._unsub_publish = self._unsub_empty = self._unsub_info = None
        self._unsub_timetable, self.timetable_due = None, None
        if self._timetable_task is not None:
            self._timetable_task.cancel()
        # The option turned off, the stop changed: no fetch left to write them back.
        async_release_timetables(self.hass)
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
    def timetable(self) -> Timetable | None:
        """Return the merge of the scheduled departures (none before the first update)."""
        return self.timetable_cache.timetable if self.timetable_cache is not None else None

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

    def build(
        self,
        now: datetime | None = None,
        *,
        keep: Callable[[Departure], bool] | None = None,
        min_minutes: int = 0,
        limit: int | None = None,
        all_scheduled: bool = False,
    ) -> StopData:
        """Build a fresh snapshot (filters applied, countdowns for `now`).

        Scheduled departures not covered by realtime ones fill in as many rows as
        the entities show, `limit` or all of them with `all_scheduled` (a busy
        stop has hundreds left in the day). `keep` (by line, destination and
        platform) and `min_minutes` narrow the departures further.
        """
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
        apply = partial(self.filter.apply, now=now, keep=keep, min_minutes=min_minutes)
        if (timetable := self.timetable) is None:
            matching = apply(departures)
        else:
            supplement = timetable.supplement(
                departures,
                now,
                self.stop.platform_labels,
                lambda dep: self.filter.matches(dep) and (keep is None or keep(dep)),
            )
            if limit is None and not all_scheduled:
                limit = self._shown
            # Both sorted by countdown and the minute shown, on a tie the realtime one first.
            matching = list(
                heapq.merge(
                    apply(timetable.with_page_destinations(departures)),
                    apply(supplement, limit=limit),
                    key=lambda dep: (dep.minutes, dep.expected),
                )
            )
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
        self._check_timetable()

    # ------------------------------------------------- scheduled departures

    @property
    def timetable_paused(self) -> bool:
        """Return True while the refresh waits for the departures to be shown again."""
        running = self._timetable_task is not None and not self._timetable_task.done()
        return self.timetable_due is None and not running

    @callback
    def _schedule_timetable(self, delay: float) -> None:
        if self._unsub_timetable is not None:
            self._unsub_timetable()
        self.timetable_due = dt_util.utcnow() + timedelta(seconds=delay)
        self._unsub_timetable = async_call_later(self.hass, delay, self._async_timetable_due)

    @callback
    def _schedule_timetable_soon(self) -> None:
        """Fetch within TIMETABLE_JITTER, at random.

        The ticks of an installation's stops are in step, and every installation
        would fetch in the same minute: after midnight, or when imhd.sk is back.
        """
        self._schedule_timetable(random.uniform(0, TIMETABLE_JITTER.total_seconds()))  # noqa: S311

    @callback
    def _async_timetable_due(self, _now: datetime) -> None:
        self._unsub_timetable, self.timetable_due = None, None
        self._start_timetable_update(scheduled=True)

    @callback
    def _start_timetable_update(self, *, scheduled: bool) -> None:
        if self._timetable_task is None or self._timetable_task.done():
            self._timetable_task = self.config_entry.async_create_background_task(
                self.hass,
                self._async_update_timetable(scheduled=scheduled),
                name=f"{self.name} scheduled departures",
            )

    @callback
    def _check_timetable(self) -> None:
        """Resume a paused refresh; fetch a missing page early (soon, not at the tick).

        Today's page after midnight, tomorrow's when the list runs short.
        """
        if (
            (cache := self.timetable_cache) is None
            or cache.timetable is None
            # Nothing fetched yet.
            or (time_zone := cache.time_zone) is None
            or (self._timetable_task is not None and not self._timetable_task.done())
        ):
            return
        if (due := self.timetable_due) is None:
            # Paused while the departures couldn't be shown.
            if self.available:
                self._schedule_timetable_soon()
        elif due - dt_util.utcnow() > TIMETABLE_JITTER and self._timetable_days(
            dt_util.now(time_zone), scheduled=False
        ):
            self._schedule_timetable_soon()

    async def _async_update_timetable(self, *, scheduled: bool) -> None:
        """Fetch the pages that are due and merge them in (best effort)."""
        if (cache := self.timetable_cache) is None:
            return
        async with cache.lock:
            if (time_zone := cache.time_zone) is None:
                time_zone = await dt_util.async_get_time_zone(IMHD_TIME_ZONE)
                time_zone = cache.time_zone = time_zone or dt_util.get_default_time_zone()
            if (timetable := cache.timetable) is None:
                timetable = cache.timetable = Timetable()
            now = dt_util.now(time_zone)
            cache.forget_before(now.date())
            if days := self._timetable_days(now, scheduled=scheduled):
                await cache.async_fetch(self.api, self.stop, days, time_zone)
            timetable.update(cache.pages.values())
        if self.available:
            self._schedule_timetable(self._timetable_delay())
        # Otherwise paused: the tick resumes it once the departures can be shown.
        if self.has_data:
            # Publishes (and moves last_update) only when the content changed.
            self._publish(from_feed=True)

    def _timetable_days(self, now: datetime, *, scheduled: bool) -> list[date]:
        """Return the days whose pages to fetch now (none: nothing is due).

        The timer refreshes today's page (and fetches missing ones) when its own
        refresh is due, counted from its last fetch: tomorrow's page fetched in
        between doesn't put it off. A retry after an error refetches today's
        only when its own refresh is due (tomorrow's failing). Missing pages,
        today's after midnight and tomorrow's when the list runs short, are
        fetched earlier, but not more often than every TIMETABLE_MIN_INTERVAL
        and not while backing off after errors. Nothing is fetched while the
        departures can't be shown (imhd.sk refused the realtime connection, or
        it is down).
        """
        cache = self.timetable_cache
        if cache is None or not self.available:
            return []
        today = now.date()
        if cache.attempt is None:
            return [today] if scheduled else []
        wanted = [today]
        # Short with today's departures (without them it is short anyway).
        if today in cache.pages and len(self.data.matching) < self.filter.max_departures:
            wanted.append(today + timedelta(days=1))
        missing = [day for day in wanted if day not in cache.pages]
        since = now - cache.attempt
        if cache.failures:
            # Backing off: only the retry, once it is due.
            if not scheduled or since < cache.refresh_after():
                return []
            if missing and not cache.refresh_due(today, now):
                return missing
            # Also when nothing is missing: today's page failed.
            return list(dict.fromkeys([today, *missing]))
        if since < TIMETABLE_MIN_INTERVAL:
            return []
        if scheduled and cache.refresh_due(today, now):
            return list(dict.fromkeys([today, *missing]))
        return missing

    def _timetable_delay(self) -> float:
        """Return the seconds until the next refresh is due (with jitter when all is well).

        When all is well, today's page is refreshed TIMETABLE_REFRESH (moved by
        up to TIMETABLE_JITTER either way) after its own last fetch, not after
        the last attempt: tomorrow's page, fetched since as the list ran short,
        doesn't put it off. After errors the retry backs off from the last
        attempt.
        """
        cache = self.timetable_cache
        if cache is None or cache.attempt is None:
            return TIMETABLE_MIN_INTERVAL.total_seconds()
        due = cache.attempt + cache.refresh_after()
        if not cache.failures:
            jitter = random.uniform(0, 2 * TIMETABLE_JITTER.total_seconds())  # noqa: S311
            today = dt_util.now(cache.time_zone).date()
            last = cache.refreshed.get(today, cache.attempt)
            due = max(
                last + cache.refresh_after() + timedelta(seconds=jitter),
                cache.attempt + TIMETABLE_MIN_INTERVAL,
            )
        return max(0.0, (due - dt_util.utcnow()).total_seconds())

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
