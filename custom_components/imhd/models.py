"""Data models for the IMHD.sk Departures integration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
import math
from typing import Any
import unicodedata

from .const import BASE_URL, SECTIONS, SOURCE_REALTIME

# imhd.sk shows "N min" up to this many minutes, then the clock time.
COUNTDOWN_TEXT_MAX = 60
# imhd.sk's expected departure (`cas`) runs this much ahead of its displayed time.
PREDICTION_LEAD = timedelta(seconds=15)


@dataclass(slots=True, kw_only=True)
class StopInfo:
    """A stop (with all its platforms) as known by imhd.sk."""

    stop_id: int
    name: str
    section: str
    city: str | None = None
    name_long: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    platform_labels: dict[str, str] = field(default_factory=dict)
    distance_m: int | None = None

    @property
    def board_url(self) -> str:
        """Return the URL of the imhd.sk online departure board."""
        return board_url(self.section, self.stop_id)

    @property
    def city_name(self) -> str:
        """Return the city of the stop, falling back to the section name."""
        return self.city or SECTIONS.get(self.section, self.section)

    @property
    def platforms(self) -> list[str]:
        """Return the platform labels sorted naturally."""
        return sorted(
            {self.platform_label(pid) for pid in self.platform_labels},
            key=natural_key,
        )

    def platform_label(self, platform_id: str | int | None) -> str:
        """Return the label of a platform id (fallback: the id itself)."""
        if platform_id is None:
            return ""
        key = str(platform_id)
        return self.platform_labels.get(key) or key

    def describe(self) -> str:
        """Return a one-line human readable description for selectors."""
        text = f"{self.name} ({self.city_name})"
        if platforms := self.platforms:
            text += f" · {', '.join(platforms)}"
        if self.distance_m is not None:
            text += f" · {self.distance_m} m"
        return text

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation."""
        return {
            "id": self.stop_id,
            "name": self.name,
            "name_long": self.name_long,
            "city": self.city_name,
            "section": self.section,
            "lat": self.latitude,
            "lon": self.longitude,
            "platforms": self.platforms,
            "platform_labels": dict(self.platform_labels),
            "distance_m": self.distance_m,
            "url": self.board_url,
        }


@dataclass(slots=True, kw_only=True)
class Departure:
    """One departure from a stop."""

    line: str
    destination: str
    destination_city: str | None = None
    departure: datetime
    scheduled: datetime | None = None
    delay: int | None = None
    realtime: bool = False
    platform: str = ""
    platform_id: str | None = None
    vehicle: str | None = None
    low_floor: bool | None = None
    air_conditioning: bool | None = None
    stuck: bool = False
    text: str | None = None
    trip_id: int | None = None
    terminal: str | None = None
    previous_stop: str | None = None
    stops_away: int | None = None
    vehicle_type: str | None = None
    # SOURCE_REALTIME (the realtime feed) or SOURCE_TIMETABLE (scheduled departures page).
    source: str = SOURCE_REALTIME
    # The scheduled departures page's destination, where the feed's text took its
    # place or was matched to it ("Petržalka, Kapitulský dvor" for "Kapitulský dvor").
    timetable_destination: str | None = None
    minutes: int = 0
    leave_in: int = 0

    def with_countdown(self, now: datetime, walking_time: int = 0) -> Departure:
        """Return a copy with `minutes`, `leave_in` and `text` computed for `now`."""
        minutes = minutes_until(self.departure, now)
        return replace(
            self,
            minutes=minutes,
            leave_in=minutes - walking_time,
            text=countdown_text(self.departure, now, realtime=self.realtime),
        )

    @property
    def direction_text(self) -> str:
        """Return what a direction filter looks in: headsign, terminal and page destination."""
        texts = (self.destination, self.terminal, self.timetable_destination)
        return normalize_text("\n".join(text for text in texts if text))

    @property
    def expected(self) -> datetime:
        """Return the departure minute imhd.sk shows (the same as `time`)."""
        return expected_departure(self.departure)

    @property
    def shown_order(self) -> tuple[Any, ...]:
        """Return a sort key: the minute shown, then line and trip.

        Unlike the prediction to the second, it does not change when busy boards
        move predictions by a few seconds.
        """
        return (self.expected.timestamp(), natural_key(self.line), self.trip_id or 0)

    def as_dict(self) -> dict[str, Any]:
        """Return the public representation (service responses, to the second)."""
        return {
            "line": self.line,
            "destination": self.destination,
            "destination_city": self.destination_city,
            "departure": self.departure.isoformat(),
            "scheduled": self.scheduled.isoformat() if self.scheduled else None,
            "time": expected_clock_time(self.departure),
            "scheduled_time": clock_time(self.scheduled) if self.scheduled else None,
            "minutes": self.minutes,
            "leave_in": self.leave_in,
            "delay": self.delay,
            "realtime": self.realtime,
            "source": self.source,
            "platform": self.platform,
            "vehicle": self.vehicle,
            "low_floor": self.low_floor,
            "air_conditioning": self.air_conditioning,
            "stuck": self.stuck,
            "text": self.text,
            "trip_id": self.trip_id,
            # Extras beyond the documented core fields.
            "platform_id": self.platform_id,
            "terminal": self.terminal,
            "previous_stop": self.previous_stop,
            "stops_away": self.stops_away,
            "vehicle_type": self.vehicle_type,
        }

    def as_attributes(self) -> dict[str, Any]:
        """Return the entity attribute representation (minute precision).

        `departure` is the minute shown by imhd.sk (as `time`): busy boards tweak
        predictions by seconds every few seconds, which must not write new states.
        """
        return {**self.as_dict(), "departure": self.expected.isoformat()}


@dataclass(frozen=True, slots=True, kw_only=True)
class TimetablePage:
    """The scheduled departures of a stop on one day, sorted by time."""

    day: date
    departures: tuple[Departure, ...] = ()
    # HTTP cache validators of the response, sent back with the next request.
    etag: str | None = None
    last_modified: str | None = None


@dataclass(slots=True, kw_only=True)
class StopData:
    """Snapshot of a stop published by the coordinator."""

    stop: StopInfo
    departures: list[Departure] = field(default_factory=list)
    matching: list[Departure] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    # False until imhd.sk sent the info texts (or their absence was confirmed).
    info_known: bool = False
    connected: bool = False
    last_update: datetime | None = None
    reconnects: int = 0

    @property
    def next(self) -> Departure | None:
        """Return the next departure (after filters), if any."""
        return self.departures[0] if self.departures else None

    @property
    def lines(self) -> list[str]:
        """Return the sorted unique lines present in the departures."""
        return sorted({dep.line for dep in self.departures}, key=natural_key)


def board_url(section: str, stop_id: int) -> str:
    """Return the URL of the imhd.sk online departure board of a stop."""
    return f"{BASE_URL}/{section}/online-zastavkova-tabula?st={stop_id}"


def _seconds_until(when: datetime, now: datetime) -> float:
    """Return the real seconds from `now` to `when`.

    Datetimes of one time zone subtract as wall clock times, an hour off across
    a DST change.
    """
    return (when.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


def minutes_until(when: datetime, now: datetime) -> int:
    """Return whole minutes until `when` (rounded down like imhd.sk, never < 0)."""
    return max(0, math.floor(_seconds_until(when, now) / 60))


def countdown_text(when: datetime, now: datetime, *, realtime: bool) -> str:
    """Return the text imhd.sk boards show ("*", "<1 min", "4 min", "22:15").

    Timetable-only departures get the "~" prefix like on imhd.sk.
    """
    seconds = _seconds_until(when, now)
    if seconds <= 0:
        return "*"
    minutes = int(seconds // 60)
    if minutes < 1:
        text = "<1 min"
    elif minutes <= COUNTDOWN_TEXT_MAX:
        text = f"{minutes} min"
    else:
        text = expected_clock_time(when)
    return text if realtime else f"~{text}"


def clock_time(when: datetime) -> str:
    """Return HH:MM, truncated like stop.js (21:33:45 -> "21:33")."""
    return when.strftime("%H:%M")


def expected_departure(when: datetime) -> datetime:
    """Return the minute imhd.sk shows for an expected departure (`cas`).

    imhd.sk predicts departures 15 s early; its board shows cas + 15 s truncated
    (checked against 1,196 server rendered times). Computed in UTC: local
    arithmetic drops the DST fold (02:xx comes twice at the end of summer time).
    """
    shown = (when.astimezone(UTC) + PREDICTION_LEAD).replace(second=0, microsecond=0)
    return shown.astimezone(when.tzinfo)


def expected_clock_time(when: datetime) -> str:
    """Return the HH:MM imhd.sk shows for an expected departure (`cas`)."""
    return clock_time(expected_departure(when))


def natural_key(text: str) -> tuple[Any, ...]:
    """Sort key ordering embedded numbers numerically ("9" < "N21" < "X13")."""
    digits = "".join(ch for ch in text if ch.isascii() and ch.isdigit())
    prefix = "".join(ch for ch in text if not (ch.isascii() and ch.isdigit()))
    return (prefix.casefold(), int(digits) if digits else -1, text)


def normalize_text(text: str) -> str:
    """Return `text` case-folded and stripped of diacritics ("Žilina" -> "zilina")."""
    decomposed = unicodedata.normalize("NFKD", text.strip())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
