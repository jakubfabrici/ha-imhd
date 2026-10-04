"""Scheduled departures of a stop, merged into its realtime departures."""

from __future__ import annotations

import asyncio
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, tzinfo
from itertools import islice
import logging
from operator import itemgetter
import re

from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .api import ImhdApi, ImhdError, ImhdStopNotFoundError
from .const import (
    DEPARTED_GRACE,
    DOMAIN,
    SOURCE_REALTIME,
    TIMETABLE_BACKOFF_MAX,
    TIMETABLE_JITTER,
    TIMETABLE_MIN_INTERVAL,
    TIMETABLE_NOT_FOUND_ATTEMPTS,
    TIMETABLE_NOT_FOUND_RETRY,
    TIMETABLE_REFRESH,
)
from .models import Departure, StopInfo, TimetablePage, normalize_text

_LOGGER = logging.getLogger(__name__)

# Failed fetches in a row before a warning is logged.
FAILURES_BEFORE_WARNING = 3

_NON_WORD_RE = re.compile(r"[\W_]+")

# A scheduled departure as the realtime feed knows it: line and scheduled minute (UTC).
type ScheduleKey = tuple[str, datetime]
# A trip the realtime feed listed: its ScheduleKey, platform id and trip id.
type TripKey = tuple[str, datetime, str | None, int | None]
# A row of a page: line, time (UTC), the page's platform label, destination and
# which of the identical rows it is (a line leaving twice in a minute).
type RowId = tuple[str, datetime, str, str, int]
# What a covering realtime departure tells about a (line, page destination):
# platform id, destination, destination city and terminal.
type Route = tuple[str | None, str, str | None, str | None]


@dataclass(kw_only=True)
class TimetableCache:
    """The fetched pages of a stop, the fetch state and the merge.

    Shared by the entries of the stop, also across reloads, so that the pages
    are fetched at most once per TIMETABLE_MIN_INTERVAL per stop and what the
    merge learned from the realtime departures (which ones it listed) is kept.
    """

    pages: dict[date, TimetablePage] = field(default_factory=dict)
    # The merge of the pages (created when an entry of the stop starts).
    timetable: Timetable | None = None
    # When the last fetch started (also one cancelled), and when a page was last fetched.
    attempt: datetime | None = None
    fetched: datetime | None = None
    # When the attempt that fetched each page started.
    refreshed: dict[date, datetime] = field(default_factory=dict)
    # imhd.sk's time zone (loaded with the first fetch).
    time_zone: tzinfo | None = None
    error: str | None = None
    failures: int = 0
    # A warning was logged for the failures (an info follows when a fetch works).
    warned: bool = False
    # imhd.sk has no page for the stop: retried after TIMETABLE_NOT_FOUND_RETRY.
    not_found: bool = False
    # Attempts in a row that fetched nothing and got a "not found".
    not_found_attempts: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def refresh_after(self) -> timedelta:
        """Return how long the next attempt waits (at the earliest, before the jitter).

        After errors from the last attempt; when all is well from the last
        fetch of today's page (see `refresh_due`).
        """
        if self.not_found:
            return TIMETABLE_NOT_FOUND_RETRY
        if self.failures:
            return min(TIMETABLE_MIN_INTERVAL * 2 ** (self.failures - 1), TIMETABLE_BACKOFF_MAX)
        return TIMETABLE_REFRESH - TIMETABLE_JITTER

    def refresh_due(self, day: date, now: datetime) -> bool:
        """Return True when the page of `day` is missing or its own refresh is due."""
        refreshed = self.refreshed.get(day)
        return refreshed is None or now - refreshed >= TIMETABLE_REFRESH - TIMETABLE_JITTER

    def forget_before(self, day: date) -> None:
        """Drop the pages of days before `day`."""
        self.pages = {page_day: page for page_day, page in self.pages.items() if page_day >= day}
        self.refreshed = {
            page_day: when for page_day, when in self.refreshed.items() if page_day >= day
        }

    def release(self) -> None:
        """Drop the pages and the merge (no entry shows them); keep the fetch state."""
        self.pages, self.refreshed = {}, {}
        self.timetable = None

    async def async_fetch(
        self, api: ImhdApi, stop: StopInfo, days: Iterable[date], time_zone: tzinfo
    ) -> None:
        """Fetch the pages of `days`; on errors the pages fetched before are kept.

        The attempt counts from its start, also when it is cancelled (a reload),
        and so does the refresh of a page whose request it cancelled: imhd.sk
        got the request, the reloaded entry waits for the page's next refresh.
        """
        self.attempt = attempt = dt_util.utcnow()
        fetched = False
        for day in days:
            try:
                page = await api.async_get_timetable(stop, day, time_zone, self.pages.get(day))
            except ImhdError as err:
                self._failed(stop, day, err, fetched=fetched)
                break
            except asyncio.CancelledError:
                if day in self.pages:
                    self.refreshed[day] = attempt
                raise
            self.pages[day], self.refreshed[day] = page, attempt
            fetched = True
        else:
            if self.warned:
                _LOGGER.info("Scheduled departures of stop %s fetched again", stop.name)
            self.failures, self.error, self.not_found = 0, None, False
            self.not_found_attempts, self.warned = 0, False
        if fetched:
            self.fetched = dt_util.utcnow()

    def _failed(self, stop: StopInfo, day: date, err: ImhdError, *, fetched: bool) -> None:
        """Count a failed attempt (the page of `day` failed).

        "Not found" means no page for the stop only when no page of it was ever
        fetched or it repeats: a single one (a hiccup) is retried like any error.
        Only tomorrow's page failing (today's is shown) logs no warning, and its
        "not found" doesn't count.
        """
        self.failures += 1
        self.error = str(err)
        tomorrow = (day - timedelta(days=1)) in self.pages
        missing = isinstance(err, ImhdStopNotFoundError) and not fetched and not tomorrow
        self.not_found_attempts = self.not_found_attempts + 1 if missing else 0
        if missing and (
            self.fetched is None or self.not_found_attempts >= TIMETABLE_NOT_FOUND_ATTEMPTS
        ):
            if not self.not_found:
                _LOGGER.warning(
                    "imhd.sk has no scheduled departures for stop %s (%s/%s): %s. Retrying in "
                    "%.0f hours; turn the timetable option off to stop asking",
                    stop.name,
                    stop.section,
                    stop.stop_id,
                    err,
                    TIMETABLE_NOT_FOUND_RETRY.total_seconds() / 3600,
                )
            self.not_found = True
            return
        self.not_found = False
        if self.failures >= FAILURES_BEFORE_WARNING and not tomorrow and not self.warned:
            self.warned = True
            log = _LOGGER.warning
        else:
            log = _LOGGER.debug
        log(
            "Cannot fetch the scheduled departures of stop %s (%s), retrying: %s",
            stop.name,
            day,
            err,
        )


DATA_TIMETABLES: HassKey[dict[tuple[str, int], TimetableCache]] = HassKey(f"{DOMAIN}_timetables")


def _stop_name(text: str | None) -> str:
    """Return a stop name for comparisons ("Petržalka, Kapitulský" -> "petrzalka kapitulsky")."""
    return " ".join(_NON_WORD_RE.sub(" ", normalize_text(text or "")).split())


def destination_matches(page_destination: str, departure: Departure) -> bool:
    """Return True when a page destination names the terminal of a realtime departure.

    The page writes "Červený most cez Kramáre" and the town in front of stops of
    other towns ("Petržalka, Kapitulský dvor"); the feed "Kramáre ► Červený most"
    with the terminal and its town apart.
    """
    wanted = _stop_name(page_destination.split(" cez ", 1)[0])
    names = {
        _stop_name(departure.terminal),
        _stop_name(f"{departure.destination_city or ''} {departure.terminal or ''}"),
        _stop_name(departure.destination.rsplit("►", 1)[-1]),
    }
    return bool(wanted) and any(
        name and (wanted == name or wanted.endswith(f" {name}") or name.endswith(f" {wanted}"))
        for name in names
    )


class Timetable:
    """The scheduled departures of a stop, merged into its realtime departures.

    The page has no trip ids. A realtime departure covers the scheduled one with
    the same line and scheduled minute (a delay doesn't move it); when a line
    leaves twice in that minute, the one on its platform, then the one with its
    destination; of identical rows, one per realtime departure. A covered
    departure stays hidden after the realtime one went (it left early or was
    cancelled), also when its page came later. The page numbers the platforms
    its own way (1-4 where the board says 9-12): their ids are learned from the
    departures covered, like the feed's destination texts ("Kramáre ► Červený
    most" for the page's "Červený most cez Kramáre"). The page's text stays with
    the departures (also the realtime ones) for direction filters: "Petržalka,
    Kapitulský dvor" is "Kapitulský dvor" in the feed.
    """

    def __init__(self) -> None:
        """Initialize without departures."""
        self._rows: list[Departure] = []
        # Their times in UTC: same-zone comparisons ignore the DST fold.
        self._times: list[datetime] = []
        self._ids: list[RowId] = []
        self._index: dict[ScheduleKey, list[tuple[RowId, Departure]]] = {}
        self._labels: frozenset[str] = frozenset()
        # Learned from covered departures.
        self._label_platforms: dict[str, str] = {}
        self._routes: dict[tuple[str, str], Route] = {}
        # The page's destination of the feed's (line, destination).
        self._page_destinations: dict[tuple[str, str], str] = {}
        # The realtime departures listed (also dropped since) and the rows they
        # cover, until their scheduled time is past.
        self._listed: dict[TripKey, Departure] = {}
        self._covered: set[RowId] = set()

    @property
    def rows(self) -> int:
        """Return the number of scheduled departures (all days)."""
        return len(self._rows)

    @property
    def learned_platforms(self) -> dict[str, str]:
        """Return the platform ids learned for the page's platform labels."""
        return dict(self._label_platforms)

    def update(self, pages: Iterable[TimetablePage]) -> None:
        """Use the departures of these pages."""
        rows = sorted(
            ((dt_util.as_utc(dep.departure), dep) for page in pages for dep in page.departures),
            key=itemgetter(0),
        )
        self._times = [when for when, _dep in rows]
        self._rows = [dep for _when, dep in rows]
        self._ids = []
        self._index = {}
        repeats: Counter[tuple[str, datetime, str, str]] = Counter()
        for when, dep in rows:
            row = dep.line, when, dep.platform, dep.destination
            row_id = (*row, repeats[row])
            repeats[row] += 1
            self._ids.append(row_id)
            self._index.setdefault(self.key(dep.line, when), []).append((row_id, dep))
        self._labels = frozenset(dep.platform for dep in self._rows if dep.platform)

    @staticmethod
    def key(line: str, when: datetime) -> ScheduleKey:
        """Return the line and scheduled minute (UTC).

        Bratislava's scheduled times come in 15 s steps: the page shows the minute
        (rounded down; the offsets are whole hours). The instant tells apart the
        two 02:xx of the autumn DST change, which the page lists one after the other.
        """
        return line.casefold(), dt_util.as_utc(when).replace(second=0, microsecond=0)

    def supplement(
        self,
        realtime: Iterable[Departure],
        now: datetime,
        platform_labels: Mapping[str, str],
        keep: Callable[[Departure], bool] | None = None,
    ) -> Iterator[Departure]:
        """Return the scheduled departures no realtime departure covers, by time.

        Departures are kept until 30 s past their time, like the feed's timetable
        rows. `keep`, a filter by line, destination and platform, is asked once
        per route of the page: the departures of a route it rejects are skipped
        without being resolved.
        """
        board = self._board_platforms(platform_labels)
        self._listed.update(
            ((*self.key(dep.line, dep.scheduled), dep.platform_id, dep.trip_id), dep)
            for dep in realtime
            if dep.scheduled is not None and dep.source == SOURCE_REALTIME
        )
        self._match(board)
        since = dt_util.as_utc(now) - DEPARTED_GRACE
        self._listed = {trip: dep for trip, dep in self._listed.items() if trip[1] >= since}
        self._covered = {row_id for row_id in self._covered if row_id[1] >= since}
        return self._uncovered(bisect_left(self._times, since), board, platform_labels, keep)

    def with_page_destinations(self, realtime: Iterable[Departure]) -> list[Departure]:
        """Return the realtime departures with the page's text of their destination, if known."""
        return [
            replace(dep, timetable_destination=text)
            if (text := self._page_destinations.get((dep.line.casefold(), dep.destination)))
            else dep
            for dep in realtime
        ]

    def _uncovered(
        self,
        start: int,
        board: Mapping[str, str],
        platform_labels: Mapping[str, str],
        keep: Callable[[Departure], bool] | None,
    ) -> Iterator[Departure]:
        # Whether `keep` takes the departures of a (line, destination, platform label).
        kept: dict[tuple[str, str, str], bool] = {}
        for row, row_id in zip(
            islice(self._rows, start, None), islice(self._ids, start, None), strict=True
        ):
            route = row.line, row.destination, row.platform
            verdict = kept.get(route)
            if verdict is False or row_id in self._covered:
                continue
            departure = self._resolve(row, board, platform_labels)
            if verdict is None and keep is not None:
                kept[route] = verdict = keep(departure)
                if not verdict:
                    continue
            yield departure

    def _board_platforms(self, platform_labels: Mapping[str, str]) -> dict[str, str]:
        """Return label -> platform id when the page uses the board's labels (Bratislava)."""
        board = {label: platform_id for platform_id, label in platform_labels.items()}
        return board if self._labels and self._labels <= board.keys() else {}

    def _match(self, board: Mapping[str, str]) -> None:
        """Cover the scheduled departures of the listed ones, learning from the unique ones.

        Identical rows (a line leaving twice in a minute from one platform to one
        destination) are interchangeable: as many are covered as listed
        departures are theirs.
        """
        ties: list[tuple[Departure, list[tuple[RowId, Departure]]]] = []
        for (line, minute, _platform_id, _trip_id), dep in self._listed.items():
            candidates = self._index.get((line, minute))
            if not candidates:
                continue
            if len(candidates) > 1:
                ties.append((dep, candidates))
                continue
            row_id, row = candidates[0]
            if row.platform and dep.platform_id is not None:
                self._label_platforms[row.platform] = dep.platform_id
            self._routes[(row.line.casefold(), row.destination)] = (
                dep.platform_id,
                dep.destination,
                dep.destination_city,
                dep.terminal,
            )
            self._page_destinations[(dep.line.casefold(), dep.destination)] = row.destination
            self._covered.add(row_id)
        claims: Counter[tuple[RowId, ...]] = Counter()
        for dep, candidates in ties:
            rows = [row_id for row_id, _row in self._narrow(dep, candidates, board)]
            if len({row_id[:4] for row_id in rows}) == 1:
                claims[tuple(rows)] += 1
        for rows, count in claims.items():
            uncovered = [row_id for row_id in rows if row_id not in self._covered]
            # Those covered before count (the same departures, in an earlier update).
            covered = len(rows) - len(uncovered)
            self._covered.update(uncovered[: max(0, count - covered)])

    def _narrow(
        self,
        dep: Departure,
        candidates: list[tuple[RowId, Departure]],
        board: Mapping[str, str],
    ) -> list[tuple[RowId, Departure]]:
        """Return those of a line's departures in a minute that `dep` may be.

        Those on its platform (else those whose platform isn't known yet), then
        those to its destination. Where none fits, all stay: a realtime
        departure hides one only when it is clear which.
        """
        by_platform: dict[str | None, list[tuple[RowId, Departure]]] = {}
        for item in candidates:
            by_platform.setdefault(self._platform_id(item[1], board), []).append(item)
        candidates = by_platform.get(dep.platform_id) or by_platform.get(None) or candidates
        if len(candidates) > 1:
            candidates = [
                item for item in candidates if destination_matches(item[1].destination, dep)
            ] or candidates
        return candidates

    def _platform_id(self, row: Departure, board: Mapping[str, str]) -> str | None:
        if row.platform:
            return self._label_platforms.get(row.platform) or board.get(row.platform)
        route = self._routes.get((row.line.casefold(), row.destination))
        return route[0] if route else None

    def _resolve(
        self, row: Departure, board: Mapping[str, str], platform_labels: Mapping[str, str]
    ) -> Departure:
        """Return the departure with the board's platform and the feed's names, when known."""
        platform_id = self._platform_id(row, board)
        route = self._routes.get((row.line.casefold(), row.destination))
        if platform_id is None and route is None:
            return row
        platform = row.platform
        if platform_id is not None:
            platform = platform_labels.get(platform_id) or platform_id
        if route is None:
            return replace(row, platform=platform, platform_id=platform_id)
        _platform_id, destination, destination_city, terminal = route
        return replace(
            row,
            platform=platform,
            platform_id=platform_id,
            destination=destination,
            destination_city=destination_city,
            terminal=terminal,
            timetable_destination=row.destination,
        )
