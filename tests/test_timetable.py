"""Tests for the scheduled departures (imhd.sk "all departures from the stop" page)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
import json
import logging
import math
from random import Random
import re
import threading
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.const import CONF_NAME, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.api import (
    PARSE_IN_EXECUTOR,
    ImhdApi,
    ImhdConnectionError,
    ImhdStopNotFoundError,
    _decode_stop_token,
    encode_stop_token,
    parse_tabs,
    parse_timetable_page,
    timetable_url,
)
from custom_components.imhd.const import (
    BASE_URL,
    CONF_DEPARTURE_SENSORS,
    CONF_DIRECTION,
    CONF_MAX_DEPARTURES,
    CONF_PLATFORM_LABELS,
    CONF_PLATFORMS,
    CONF_SECTION,
    CONF_STOP_CITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TIMETABLE,
    DEFAULT_OPTIONS,
    DOMAIN,
    TICK_INTERVAL,
    TIMETABLE_BACKOFF_MAX,
    TIMETABLE_JITTER,
    TIMETABLE_MIN_INTERVAL,
    TIMETABLE_NOT_FOUND_RETRY,
    TIMETABLE_REFRESH,
    TIMETABLE_START_DELAY,
)
from custom_components.imhd.coordinator import DepartureFilter, ImhdCoordinator
from custom_components.imhd.diagnostics import async_get_config_entry_diagnostics
from custom_components.imhd.models import Departure, StopInfo, TimetablePage
from custom_components.imhd.timetable import (
    DATA_TIMETABLES,
    Timetable,
    TimetableCache,
    destination_matches,
)

from .conftest import (
    LABELS,
    NOW,
    STOP_PAGE_URL,
    FakeFeed,
    TimetableServer,
    entry_data,
    load_fixture,
    load_json,
    row,
    sample_tabs,
    setup_entry,
    tabs,
    timetable_page,
)

TZ = ZoneInfo("Europe/Bratislava")
SATURDAY, SUNDAY, MONDAY = date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5)
# The end of summer time: 03:00 CEST is 02:00 CET.
DST_DAY = date(2026, 10, 25)
SN_LABELS = {"11476": "11", "11484": "10", "11521": "12", "11528": "9"}
MAIN = "sensor.hodzovo_departures"
# The first fetch with `midpoint_jitter`: halfway through TIMETABLE_START_DELAY
# after setup, then every TIMETABLE_REFRESH.
START = timedelta(seconds=sum(TIMETABLE_START_DELAY) / 2)
REFRESH = TIMETABLE_REFRESH
# A missing page, or the refresh resumed, with `midpoint_jitter`: half of
# TIMETABLE_JITTER after the tick.
SOON = TIMETABLE_JITTER / 2
# Tests whose timeline spans several refreshes start early on Saturday, so
# that it stays within the day.
MORNING = datetime(2026, 10, 3, 6, 0, tzinfo=TZ)

# Scheduled departures of stop 83 on Saturday from 21:00 (NOW). The realtime
# `sample_tabs` cover 4 21:01, 9 21:02 (+2, expected 21:04), X13 21:12 and N33 21:25.
SATURDAY_ROWS = [
    ("9", "20:58", "Karlova Ves", "A"),
    ("4", "21:01", "Dúbravka", "B"),
    ("9", "21:02", "Karlova Ves", "A"),
    ("X13", "21:12", "Petržalka, Jungmannova", "A"),
    ("4", "21:16", "Dúbravka", "B"),
    ("9", "21:17", "Karlova Ves", "A"),
    ("50", "21:20", "Aupark", "C"),
    ("N33", "21:25", "Hlavná stanica", "B"),
    ("4", "21:31", "Dúbravka", "B"),
    ("9", "21:32", "Karlova Ves", "A"),
    ("X13", "21:42", "Petržalka, Jungmannova", "A"),
    ("4", "21:46", "Dúbravka", "B"),
    ("9", "21:47", "Karlova Ves", "A"),
    ("N33", "21:55", "Hlavná stanica", "B"),
]
# Saturday's departures all day, enough that the list runs short only after 23:30.
EVERY_3_MIN = [
    ("9", f"{hour}:{minute:02}", "Karlova Ves", "A")
    for hour in range(24)
    for minute in range(5, 60, 3)
]


def page(name: str, day: date = SUNDAY) -> list[Departure]:
    """Parse a page fixture."""
    return parse_timetable_page(load_fixture(f"{name}.html"), day, TZ)


def make_timetable(*pages: list[Departure]) -> Timetable:
    """Return a Timetable of the given (parsed) pages."""
    timetable = Timetable()
    timetable.update(
        TimetablePage(day=rows[0].departure.date(), departures=tuple(rows)) for rows in pages
    )
    return timetable


def snapshot(name: str, labels: dict[str, str]) -> tuple[datetime, list[Departure]]:
    """Return the capture time and the realtime departures of a `tabs` snapshot."""
    payload = load_json(f"{name}.json")
    now = datetime.fromtimestamp(min(p["timestamp"] for p in payload) / 1000, UTC).astimezone(TZ)
    return now, parse_tabs(payload, labels, now, stop_id=payload[0]["zastavka"])


def clock(departures: list[Departure]) -> list[tuple[str, str]]:
    """Return (line, HH:MM shown) of departures."""
    return [(dep.line, dep.expected.strftime("%H:%M")) for dep in departures]


async def settle(hass: HomeAssistant) -> None:
    """Run what is due, and the scheduled departures updates it started."""
    await hass.async_block_till_done()
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        task = getattr(coordinator, "_timetable_task", None)
        if task is not None and not task.done():
            await task
    await hass.async_block_till_done()


async def jump(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Move the clock and run what became due."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await settle(hass)


@pytest.fixture(autouse=True)
def no_timetable_fetch() -> None:
    """Fetch the scheduled departures (overrides the conftest fixture)."""


@pytest.fixture
def midpoint_jitter() -> Generator[None]:
    """Use the middle of every random range (start after START, refresh every REFRESH)."""
    with patch("custom_components.imhd.coordinator.random") as mock_random:
        mock_random.uniform.side_effect = lambda low, high: (low + high) / 2
        yield


def age(cache: TimetableCache, delta: timedelta) -> None:
    """Make the last fetch `delta` older (as if it was that long ago)."""
    cache.attempt -= delta
    cache.refreshed = {day: when - delta for day, when in cache.refreshed.items()}


def departures_attr(hass: HomeAssistant, entity_id: str = MAIN) -> list[dict[str, Any]]:
    """Return the `departures` attribute of the main sensor."""
    return hass.states.get(entity_id).attributes["departures"]


def second_entry(hass: HomeAssistant, config_entry: MockConfigEntry) -> MockConfigEntry:
    """Add another entry of stop 83, named Center."""
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Center",
        unique_id="ba_83_center",
        data={**config_entry.data, CONF_NAME: "Center"},
        options=dict(DEFAULT_OPTIONS),
    )
    other.add_to_hass(hass)
    return other


class SlowFetch:
    """Page requests held until released (imhd.sk renders a busy stop's page in seconds)."""

    def __init__(self) -> None:
        """Let requests through."""
        self.released = asyncio.Event()
        self.released.set()
        # When each request started, and its day.
        self.started: list[tuple[datetime, date]] = []

    async def async_wait_started(self, hass: HomeAssistant, count: int) -> None:
        """Wait until `count` requests started (the time zone loads in the executor first)."""
        for _ in range(50):
            if len(self.started) >= count:
                return
            await hass.async_add_executor_job(int)
            await hass.async_block_till_done()


@pytest.fixture
def slow_fetch() -> Generator[SlowFetch]:
    """Record page requests as they start; hold them while `released` is clear."""
    slow = SlowFetch()
    fetch = ImhdApi.async_get_timetable

    async def held(api: ImhdApi, stop: StopInfo, day: date, *args: Any) -> TimetablePage:
        slow.started.append((dt_util.utcnow(), day))
        await slow.released.wait()
        return await fetch(api, stop, day, *args)

    with patch.object(ImhdApi, "async_get_timetable", held):
        yield slow


# --------------------------------------------------------------------- URL


def test_timetable_url() -> None:
    """The URL is the one imhd.sk links to: its date picker's token of each day."""
    assert timetable_url("ba", 83, SUNDAY, "Hodžovo nám.") == (
        "https://imhd.sk/ba/vsetky-odchody-zo-zastavky/Hod%C5%BEovo-n%C3%A1m/"
        "ca71b67189718782717b71b3718971817f81857c807f7c7f8371cc"
    )
    dates = json.loads(
        re.search(r"var dates = (\{.*?\});", load_fixture("departures_ba_83.html")).group(1)
    )
    assert len(dates) == 22
    for day, suffix in dates.items():
        url = timetable_url("ba", 83, date.fromisoformat(day), "Hodžovo nám.")
        assert unquote(url.removeprefix(f"{BASE_URL}/ba/vsetky-odchody-zo-zastavky/")) == suffix
        assert _decode_stop_token(suffix.rsplit("/", 1)[1]) == 83
    assert encode_stop_token({"g": "83"}) == "ca71b6718971878271cc"
    # Any slug works; one is required.
    assert timetable_url("sn", 1560, MONDAY, "...").split("/")[5] == "zastavka"


# ------------------------------------------------------------------ parser


def test_parse_page() -> None:
    """Bratislava: every platform, sorted by time, with low floor and via destinations."""
    rows = page("departures_ba_83")
    assert len(rows) == 65
    first = rows[0]
    assert (first.line, first.destination, first.platform, first.low_floor) == (
        "N31",
        "Cintorín Slávičie",
        "D",
        True,
    )
    assert first.departure == first.scheduled == datetime(2026, 10, 4, 0, 2, tzinfo=TZ)
    assert (first.realtime, first.delay, first.source, first.platform_id) == (
        False,
        None,
        "timetable",
        None,
    )
    assert [dep.departure for dep in rows] == sorted(dep.departure for dep in rows)
    assert [(d.line, d.expected.strftime("%H:%M")) for d in rows if d.low_floor is False] == [
        ("598", "00:40"),
        ("699", "00:40"),
        ("798", "00:40"),
    ]
    via = next(dep for dep in rows if dep.destination == "Červený most cez Kramáre")
    assert via.terminal == "Červený most"
    assert "►" in {dep.line for dep in rows}
    assert {dep.platform for dep in rows} == {"A", "B", "C", "D"}
    assert first.with_countdown(datetime(2026, 10, 3, 23, 0, tzinfo=TZ)).text == "~00:02"


def test_parse_small_town_and_unlabeled_pages() -> None:
    """The page's own platform labels; no low-floor data means unknown, not False."""
    rows = page("departures_sn_1560")
    assert len(rows) == 43
    assert {dep.platform for dep in rows} == {"2", "3", "4"}
    assert {dep.low_floor for dep in rows} == {None}
    monday = page("departures_sn_1560_next_day", MONDAY)
    assert monday[0].departure == datetime(2026, 10, 5, 4, 8, tzinfo=TZ)
    zilina = page("departures_za_1831")
    assert {dep.platform for dep in zilina} == {""}
    assert zilina[0].departure == datetime(2026, 10, 4, 10, 35, tzinfo=TZ)


def test_parse_errors() -> None:
    """imhd.sk's error page (HTTP 200) is a missing stop; another day is an error."""
    with pytest.raises(ImhdStopNotFoundError):
        parse_timetable_page(load_fixture("departures_not_found.html"), SUNDAY, TZ)
    with pytest.raises(ImhdStopNotFoundError):
        parse_timetable_page("<html>Chyba</html>", SUNDAY, TZ)
    with pytest.raises(ImhdConnectionError):
        parse_timetable_page(load_fixture("departures_ba_83.html"), MONDAY, TZ)


def test_parse_malformed_rows() -> None:
    """Rows that can't be read are skipped; the others are kept."""
    html = timetable_page(
        SUNDAY,
        [
            ("4", "10:44", "Dúbravka", "B"),
            ("5", "25:99", "Bad time", "A"),
            ("6", "soon", "No time", "A"),
            ("", "10:50", "No line", "A"),
            ("7", "24:10", "After midnight", ""),
        ],
    )
    html = html.replace("</tbody>", "<tr><td>1</td><td>2</td></tr><tr><td>x</td></tbody>")
    html = html.replace('<span class=" Linka ba l6">6</span>', "6")
    rows = parse_timetable_page(html, SUNDAY, TZ)
    assert [(dep.line, dep.departure.isoformat(), dep.platform) for dep in rows] == [
        ("4", "2026-10-04T10:44:00+02:00", "B"),
        ("7", "2026-10-05T00:10:00+02:00", ""),
    ]


# --------------------------------------------------------------- HTTP client


async def test_get_timetable(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """One request per page, with cache validators when imhd.sk sends them."""
    stop = StopInfo(stop_id=83, name="Hodžovo nám.", section="ba", platform_labels=LABELS)
    url = timetable_url("ba", 83, SUNDAY, stop.name)
    api = ImhdApi(async_get_clientsession(hass))

    aioclient_mock.get(url, text=load_fixture("departures_ba_83.html"))
    first = await api.async_get_timetable(stop, SUNDAY, TZ)
    assert (first.day, len(first.departures), first.etag) == (SUNDAY, 65, None)
    _method, _url, _data, headers = aioclient_mock.mock_calls[-1]
    assert "HomeAssistant" in headers["User-Agent"]
    assert headers["Referer"] == "https://imhd.sk/ba/online-zastavkova-tabula?st=83"
    assert "If-None-Match" not in headers
    assert "If-Modified-Since" not in headers

    aioclient_mock.clear_requests()
    validators = {"ETag": '"abc"', "Last-Modified": "Sun, 04 Oct 2026 08:00:00 GMT"}
    aioclient_mock.get(url, text=load_fixture("departures_ba_83.html"), headers=validators)
    second = await api.async_get_timetable(stop, SUNDAY, TZ, first)
    assert (second.etag, second.last_modified) == ('"abc"', "Sun, 04 Oct 2026 08:00:00 GMT")
    assert second.departures == first.departures

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, status=HTTPStatus.NOT_MODIFIED)
    assert await api.async_get_timetable(stop, SUNDAY, TZ, second) is second
    _method, _url, _data, headers = aioclient_mock.mock_calls[-1]
    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"] == "Sun, 04 Oct 2026 08:00:00 GMT"

    # A busy stop's page is parsed in the executor, a small one in the event loop.
    busy = [("9", f"{n // 60}:{n % 60:02}", "Karlova Ves", "A") for n in range(1, 1440, 2)]
    html = timetable_page(SUNDAY, busy)
    assert len(html) > PARSE_IN_EXECUTOR
    in_loop: list[bool] = []

    def parse(*args: Any) -> list[Departure]:
        in_loop.append(threading.current_thread() is threading.main_thread())
        return parse_timetable_page(*args)

    aioclient_mock.clear_requests()
    aioclient_mock.get(url, text=html)
    with patch("custom_components.imhd.api.parse_timetable_page", side_effect=parse):
        big = await api.async_get_timetable(stop, SUNDAY, TZ)
        await api.async_get_timetable(stop, SUNDAY, TZ, first)
    assert len(big.departures) == len(busy)
    assert big.departures[-1].departure == datetime(2026, 10, 4, 23, 59, tzinfo=TZ)
    assert in_loop == [False, False]
    aioclient_mock.clear_requests()
    aioclient_mock.get(url, text=load_fixture("departures_ba_83.html"))
    with patch("custom_components.imhd.api.parse_timetable_page", side_effect=parse):
        await api.async_get_timetable(stop, SUNDAY, TZ)
    assert in_loop == [False, False, True]

    for kwargs, error in (
        ({"status": HTTPStatus.NOT_FOUND}, ImhdStopNotFoundError),
        ({"text": load_fixture("departures_not_found.html")}, ImhdStopNotFoundError),
        ({"status": HTTPStatus.INTERNAL_SERVER_ERROR}, ImhdConnectionError),
        ({"exc": TimeoutError()}, ImhdConnectionError),
    ):
        aioclient_mock.clear_requests()
        aioclient_mock.get(url, **kwargs)
        with pytest.raises(error):
            await api.async_get_timetable(stop, SUNDAY, TZ)


# --------------------------------------------------------------------- merge


@pytest.mark.parametrize(
    ("tabs_name", "page_name", "labels", "learned", "left"),
    [
        (
            "tabs_ba_83_snapshot",
            "departures_ba_83",
            LABELS,
            {"A": "213", "B": "214", "C": "215", "D": "216"},
            9,
        ),
        # The page numbers the platforms 1-4, the board 9-12.
        (
            "tabs_sn_1560_snapshot",
            "departures_sn_1560",
            SN_LABELS,
            {"2": "11484", "3": "11476", "4": "11521"},
            35,
        ),
        ("tabs_za_1831_snapshot", "departures_za_1831", {}, {}, 42),
    ],
)
def test_realtime_departures_cover_their_scheduled_ones(
    tabs_name: str, page_name: str, labels: dict[str, str], learned: dict[str, str], left: int
) -> None:
    """Captured realtime departures (delayed ones too) hide their scheduled ones 1:1."""
    now, realtime = snapshot(tabs_name, labels)
    rows = page(page_name)
    timetable = make_timetable(rows)
    supplement = list(timetable.supplement(realtime, now, labels))
    future = [dep for dep in rows if dep.departure >= now - timedelta(seconds=30)]
    assert len(supplement) == len(future) - len(realtime) == left
    scheduled = {(dep.line, dep.scheduled.replace(second=0)) for dep in realtime}
    assert len(scheduled) == len(realtime)
    assert not scheduled & {(dep.line, dep.departure) for dep in supplement}
    assert timetable.learned_platforms == learned
    assert {dep.source for dep in supplement} == {"timetable"}


def test_delayed_departure_covers_its_scheduled_one() -> None:
    """Bratislava's 44 scheduled 10:30:00, expected 10:32: its 10:30 row is hidden."""
    now, realtime = snapshot("tabs_ba_83_snapshot", LABELS)
    late = [dep for dep in realtime if dep.delay]
    assert clock(late) == [("44", "10:32"), ("47", "10:33"), ("42", "10:40"), ("44", "10:46")]
    # 42 is scheduled at 10:37:30: the page's 10:37 (rounded down, not up).
    assert late[2].scheduled.strftime("%H:%M:%S") == "10:37:30"
    supplement = list(make_timetable(page("departures_ba_83")).supplement(realtime, now, LABELS))
    assert not {("44", "10:30"), ("42", "10:37"), ("44", "10:45")} & set(clock(supplement))


def test_small_town_board() -> None:
    """Spišská Nová Ves at 10:33: 8 realtime departures, the page adds the rest of the day."""
    now, realtime = snapshot("tabs_sn_1560_snapshot", SN_LABELS)
    timetable = make_timetable(page("departures_sn_1560"))
    every = DepartureFilter()
    merged = sorted(
        every.apply(realtime, now)
        + every.apply(timetable.supplement(realtime, now, SN_LABELS), now),
        key=lambda dep: dep.minutes,
    )
    assert len(merged) == 8 + 35
    assert [
        (*row, dep.source, dep.platform) for row, dep in zip(clock(merged), merged, strict=True)
    ][:10] == [
        ("4", "10:44", "realtime", "10"),
        ("12", "11:00", "realtime", "11"),
        ("4", "11:15", "realtime", "12"),
        ("2", "11:30", "realtime", "11"),
        ("5", "11:37", "realtime", "10"),
        ("4", "11:44", "realtime", "10"),
        ("12", "12:08", "realtime", "11"),
        # The page's labels 4 and 2 are the board's 12 and 10.
        ("4", "12:18", "timetable", "12"),
        ("15", "12:31", "realtime", "12"),
        ("4", "12:44", "timetable", "10"),
    ]
    assert merged[7].text == "~12:18"
    assert (merged[7].platform_id, merged[7].destination_city) == ("11521", "Spišská Nová Ves")


def test_unlabeled_page_learns_platforms_by_route() -> None:
    """Žilina's page has no platforms: the line and destination tell them."""
    now, realtime = snapshot("tabs_za_1831_snapshot", {})
    supplement = make_timetable(page("departures_za_1831")).supplement(realtime, now, {})
    first = next(supplement)
    assert (first.line, first.expected.strftime("%H:%M"), first.destination) == (
        "3",
        "11:20",
        "Jaseňová",
    )
    assert (first.platform, first.platform_id) == ("3439", "3439")


def test_rows_after_midnight_are_on_the_next_days_page() -> None:
    """Saturday 21:34: the 8 night departures after midnight are on Sunday's page."""
    now, realtime = snapshot("tabs_ba_83_evening", LABELS)
    assert now.date() == SATURDAY
    after_midnight = [dep for dep in realtime if dep.scheduled.date() == SUNDAY]
    assert len(after_midnight) == 8
    rows = page("departures_ba_83")
    supplement = list(make_timetable(rows).supplement(realtime, now, LABELS))
    assert len(supplement) == len(rows) - 8
    assert not set(clock(after_midnight)) & set(clock(supplement))


@pytest.mark.parametrize(
    ("labels", "swap"),
    [(LABELS, True), ({}, False)],
    ids=["platform first", "destination"],
)
def test_same_line_twice_in_a_minute(labels: dict[str, str], swap: bool) -> None:
    """93 leaves platforms C and D at 12:20: the platform decides, then the destination.

    Real 93 departures of the snapshot moved to 12:20. With the board's labels
    the platform decides even when the destination texts would not.
    """
    at_1220 = int(datetime(2026, 10, 4, 12, 20, tzinfo=TZ).timestamp() * 1000)
    elements = [
        {**element, "tab": [{**row, "casCP": at_1220, "cas": at_1220} for row in element["tab"]]}
        for element in load_json("tabs_ba_83_snapshot.json")
        if element["nastupiste"] in (215, 216)
    ]
    for element in elements:
        element["tab"] = [row for row in element["tab"] if row["linka"] == "93"][:1]
    if swap:
        first, second = elements[0]["tab"][0], elements[1]["tab"][0]
        for key in ("konecnaZstr", "cielStr"):
            first[key], second[key] = second[key], first[key]
    now = datetime(2026, 10, 4, 12, 10, tzinfo=TZ)
    realtime = parse_tabs(elements, labels, now)
    assert {(dep.line, dep.platform_id) for dep in realtime} == {("93", "215"), ("93", "216")}
    timetable = make_timetable(page("departures_ba_83"))
    supplement = list(timetable.supplement(realtime, now, labels))
    assert not [dep for dep in supplement if dep.line == "93"]
    assert ("42", "12:20") in clock(supplement)


def test_ambiguous_departures_are_both_kept() -> None:
    """A realtime departure that fits neither of two scheduled ones hides none."""
    rows = parse_timetable_page(
        timetable_page(SUNDAY, [("9", "12:20", "Karlova Ves", "A"), ("9", "12:20", "Kolíba", "B")]),
        SUNDAY,
        TZ,
    )
    realtime = Departure(
        line="9",
        destination="Somewhere",
        departure=datetime(2026, 10, 4, 12, 20, tzinfo=TZ),
        scheduled=datetime(2026, 10, 4, 12, 20, tzinfo=TZ),
        platform_id="999",
    )
    now = datetime(2026, 10, 4, 12, 0, tzinfo=TZ)
    assert len(list(make_timetable(rows).supplement([realtime], now, {}))) == 2


def test_covered_departure_stays_hidden() -> None:
    """A scheduled departure stays hidden once covered, also after its realtime one went."""
    now, realtime = snapshot("tabs_sn_1560_snapshot", SN_LABELS)
    timetable = make_timetable(page("departures_sn_1560"))
    assert len(list(timetable.supplement(realtime, now, SN_LABELS))) == 35
    # Left a minute early, or cancelled: imhd.sk no longer lists it.
    assert len(list(timetable.supplement(realtime[1:], now, SN_LABELS))) == 35
    # Re-fetched pages (new objects) keep it hidden.
    timetable.update([TimetablePage(day=SUNDAY, departures=tuple(page("departures_sn_1560")))])
    assert len(list(timetable.supplement([], now, SN_LABELS))) == 35
    # Departures go 30 s after their time.
    later = datetime(2026, 10, 4, 12, 18, 30, tzinfo=TZ)
    assert clock(list(timetable.supplement([], later, SN_LABELS)))[0] == ("4", "12:18")
    # (15 at 12:31 is covered.)
    assert clock(list(timetable.supplement([], later + timedelta(seconds=1), SN_LABELS)))[0] == (
        "4",
        "12:44",
    )


def test_platform_filter_needs_a_known_platform() -> None:
    """With a platform filter, departures of a platform not known yet are left out."""
    now, realtime = snapshot("tabs_sn_1560_snapshot", SN_LABELS)
    platform_10 = DepartureFilter.from_options({CONF_PLATFORMS: ["10"]})
    fresh = make_timetable(page("departures_sn_1560"))
    # The page's "2" isn't the board's "2" (there is none): nothing passes yet.
    assert platform_10.apply(fresh.supplement([], now, SN_LABELS), now) == []
    supplement = platform_10.apply(fresh.supplement(realtime, now, SN_LABELS), now)
    assert clock(supplement)[:3] == [("4", "12:44"), ("4", "13:46"), ("5", "14:09")]
    assert {(dep.platform, dep.platform_id) for dep in supplement} == {("10", "11484")}
    # By id too.
    by_id = DepartureFilter.from_options({CONF_PLATFORMS: ["11484"]})
    assert by_id.apply(fresh.supplement(realtime, now, SN_LABELS), now) == supplement
    # Bratislava's page uses the board's labels: known before any realtime departure.
    platform_a = DepartureFilter.from_options({CONF_PLATFORMS: ["a"]})
    ba_now = datetime(2026, 10, 4, 10, 29, tzinfo=TZ)
    ba = platform_a.apply(
        make_timetable(page("departures_ba_83")).supplement([], ba_now, LABELS), ba_now
    )
    assert ba
    assert {(dep.platform, dep.platform_id) for dep in ba} == {("A", "213")}


def test_other_filters_apply_to_scheduled_departures() -> None:
    """Lines, excluded lines, direction (via stops too) and walking time."""
    now = datetime(2026, 10, 4, 10, 29, tzinfo=TZ)
    rows = page("departures_ba_83")

    def lines(**options: Any) -> list[tuple[str, str]]:
        timetable = make_timetable(rows)
        return clock(
            DepartureFilter.from_options(options).apply(timetable.supplement([], now, LABELS), now)
        )

    assert {line for line, _ in lines(lines=["93"])} == {"93"}
    assert "93" not in {line for line, _ in lines(exclude_lines=["93"])}
    # "Červený most cez Kramáre"
    assert {line for line, _ in lines(direction="kramare")} == {"42"}
    assert lines()[0] == ("44", "10:30")
    assert lines(walking_time=5)[0] == ("93", "10:34")


def test_filter_skips_whole_routes() -> None:
    """`keep` is asked once per route; the departures of a rejected one aren't resolved."""
    now = datetime(2026, 10, 4, 10, 29, tzinfo=TZ)
    rows = page("departures_ba_83")
    routes = {
        (dep.line, dep.destination, dep.platform)
        for dep in rows
        if dep.departure >= now - timedelta(seconds=30)
    }
    asked: list[tuple[str, str, str]] = []

    def keep(dep: Departure) -> bool:
        asked.append((dep.line, dep.terminal or "", dep.platform))
        return dep.line == "93"

    timetable = make_timetable(rows)
    with patch.object(
        Timetable, "_resolve", autospec=True, side_effect=Timetable._resolve
    ) as resolve:
        kept = list(timetable.supplement([], now, LABELS, keep))
    assert len(asked) == len(set(asked)) == len(routes)
    assert kept == [dep for dep in timetable.supplement([], now, LABELS) if dep.line == "93"]
    assert resolve.call_count == len(routes) - len({dep.destination for dep in kept}) + len(kept)


def night_trip(line: str) -> list[dict[str, Any]]:
    """Return the evening capture's departure of `line` from platform D, moved to Sunday 00:02."""
    element = next(el for el in load_json("tabs_ba_83_evening.json") if el["nastupiste"] == 216)
    trip = next(item for item in element["tab"] if item["linka"] == line)
    at = int(datetime(2026, 10, 4, 0, 2, tzinfo=TZ).timestamp() * 1000)
    return [{**element, "tab": [{**trip, "cas": at - 15000, "casCP": at}]}]


@pytest.mark.parametrize(
    ("direction", "line", "before"),
    [("Petržalka", "N80", ["00:02", "00:32"]), ("Pri kríži", "N34", [])],
)
def test_direction_filter_keeps_the_page_text(direction: str, line: str, before: list[str]) -> None:
    """A direction matches the page's or the feed's text: what the feed lists only adds.

    N80 goes to "Petržalka, Kapitulský dvor" on the page, "Kapitulský dvor" in
    the feed; N34 to "Dúbravka" on the page, to the terminal "Pri kríži" in the feed.
    """
    now = datetime(2026, 10, 3, 23, 40, tzinfo=TZ)
    towards = DepartureFilter.from_options({CONF_DIRECTION: [direction]})
    timetable = make_timetable(page("departures_ba_83"))

    def shown(listed: list[Departure]) -> list[tuple[str, str]]:
        scheduled = timetable.supplement(listed, now, LABELS, towards.matches)
        departures = towards.apply(timetable.with_page_destinations(listed), now)
        departures += towards.apply(scheduled, now)
        return [
            (dep.source, dep.scheduled.astimezone(TZ).strftime("%H:%M"))
            for dep in departures
            if dep.line == line
        ]

    assert shown([]) == [("timetable", clock) for clock in before]
    realtime = parse_tabs(night_trip(line), LABELS, now)
    assert shown(realtime) == [("realtime", "00:02"), ("timetable", "00:32")]
    # Gone from the feed (it left early): its page row stays hidden, the next one stays.
    assert shown([]) == [("timetable", "00:32")]


def test_destination_matches() -> None:
    """The page's terminal, town prefix and via against the feed's texts."""

    def realtime(destination: str, terminal: str, city: str = "Bratislava") -> Departure:
        when = datetime(2026, 10, 4, 12, 0, tzinfo=TZ)
        return Departure(
            line="1",
            destination=destination,
            terminal=terminal,
            destination_city=city,
            departure=when,
        )

    assert destination_matches(
        "Červený most cez Kramáre", realtime("Kramáre ► Červený most", "Červený most")
    )
    assert destination_matches(
        "Petržalka, Kapitulský dvor", realtime("Kapitulský dvor", "Kapitulský dvor")
    )
    assert destination_matches("Dúbravka", realtime("Dúbravka", "Pri kríži"))
    assert destination_matches(
        "Senec, Žel. stanica", realtime("Žel. stanica", "Žel. stanica", "Senec")
    )
    assert not destination_matches(
        "Hlavná stanica", realtime("Petržalka, Vyšehradská", "Vyšehradská")
    )
    assert not destination_matches("", realtime("Dúbravka", "Pri kríži"))


def test_schedule_key() -> None:
    """The scheduled minute rounded down, as an instant: the two 02:17 of the DST change differ."""
    assert Timetable.key("42", datetime(2026, 10, 4, 10, 37, 30, tzinfo=TZ)) == (
        "42",
        datetime(2026, 10, 4, 8, 37, tzinfo=UTC),
    )
    summer = datetime.fromisoformat("2026-10-25T02:17:00+02:00")
    winter = datetime.fromisoformat("2026-10-25T02:17:00+01:00")
    assert Timetable.key("N31", summer) != Timetable.key("N31", winter)
    assert Timetable.key("N31", datetime(2026, 10, 25, 2, 17, tzinfo=TZ)) == Timetable.key(
        "N31", summer
    )
    assert Timetable.key("N31", datetime(2026, 10, 25, 2, 17, fold=1, tzinfo=TZ)) == (
        Timetable.key("N31", winter)
    )


def test_parse_dst_page() -> None:
    """The autumn DST day: imhd.sk lists 02:17-02:48 twice, the second pass is winter time."""
    rows = page("departures_ba_83_dst", DST_DAY)
    assert len(rows) == 10 + 2 * 20 + 8
    assert [dep.departure.timestamp() for dep in rows] == sorted(
        dep.departure.timestamp() for dep in rows
    )
    assert len({(d.line, d.departure.timestamp(), d.platform, d.destination) for d in rows}) == len(
        rows
    )
    n31 = [dep.departure.isoformat() for dep in rows if dep.line == "N31"]
    assert n31 == [
        "2026-10-25T01:32:00+02:00",
        "2026-10-25T02:18:00+02:00",
        "2026-10-25T02:32:00+02:00",
        "2026-10-25T02:18:00+01:00",
        "2026-10-25T02:32:00+01:00",
        "2026-10-25T03:18:00+01:00",
    ]
    assert all(dep.scheduled == dep.departure for dep in rows)


@pytest.mark.parametrize("second_pass", [False, True], ids=["02:10 CEST", "02:10 CET"])
def test_dst_night(second_pass: bool) -> None:
    """Each pass of 02:xx once; past rows of the first pass don't come back in the second."""
    at = datetime(2026, 10, 25, 1 if second_pass else 0, 10, tzinfo=UTC)
    # The same time zone object as the page's: same-zone comparisons ignore the fold.
    now = at.astimezone(TZ)
    assert now.strftime("%H:%M") == "02:10"
    # N31 to Hlavná stanica (C), scheduled 02:18 of this pass.
    realtime = parse_tabs(
        [tabs(215, [row("N31", 8, "Hlavná stanica", now=at, trip=7)])], LABELS, now
    )
    timetable = make_timetable(page("departures_ba_83_dst", DST_DAY))
    every = DepartureFilter()
    shown = every.apply(realtime, now) + every.apply(
        timetable.supplement(realtime, now, LABELS), now
    )
    trips = [(dep.line, dep.departure.timestamp(), dep.platform) for dep in shown]
    # 20 rows a pass and 8 after 03:00, less the covered N31.
    assert len(set(trips)) == len(trips) == 1 + (27 if second_pass else 47)
    assert all(dep.departure > now and dep.text != "*" for dep in shown)
    n31 = [
        (dep.departure.astimezone(TZ).isoformat(timespec="minutes"), dep.source)
        for dep in shown
        if dep.line == "N31"
    ]
    assert n31 == [
        *(
            []
            if second_pass
            else [("2026-10-25T02:18+02:00", "realtime"), ("2026-10-25T02:32+02:00", "timetable")]
        ),
        ("2026-10-25T02:18+01:00", "realtime" if second_pass else "timetable"),
        ("2026-10-25T02:32+01:00", "timetable"),
        ("2026-10-25T03:18+01:00", "timetable"),
    ]


def dst_page(keep: Callable[[str], bool], copies: int = 1) -> str:
    """Return the DST day's page with only the rows (HTML) `keep` takes, each `copies` times."""
    head, *rows = re.split(r"(?=<tr[\s>])", load_fixture("departures_ba_83_dst.html"))
    return head + "".join(row * copies for row in rows if keep(row))


def test_dst_page_of_a_line_leaving_once_an_hour() -> None:
    """The repeated hour has one row of a line: its second pass is still winter time.

    imhd.sk marks the two passes "(letný čas)" and "(zimný čas)"; an unmarked
    row of the repeated hour is in the second pass once it comes again.
    """
    marked = parse_timetable_page(dst_page(lambda row: ">N44</span>" in row), DST_DAY, TZ)
    hourly = [("N44", f"{hour}:48", "Koliba", "A") for hour in (1, 2, 2, 3)]
    unmarked = parse_timetable_page(timetable_page(DST_DAY, hourly), DST_DAY, TZ)
    n44 = [
        "2026-10-25T01:48+02:00",
        "2026-10-25T02:48+02:00",
        "2026-10-25T02:48+01:00",
        "2026-10-25T03:48+01:00",
    ]
    assert [dep.departure.isoformat(timespec="minutes") for dep in marked] == n44[:3]
    assert [dep.departure.isoformat(timespec="minutes") for dep in unmarked] == n44
    # Marked: two trips in a minute of the first pass stay in summer time.
    twice = dst_page(lambda row: ">N44</span>" in row and "letný" in row, copies=2)
    assert [
        dep.departure.isoformat(timespec="minutes")
        for dep in parse_timetable_page(twice, DST_DAY, TZ)
    ] == [n44[1]] * 2
    # Two lines in the same minute (platform A of the real page: N53 and N55 at :33).
    a33 = parse_timetable_page(
        timetable_page(
            DST_DAY,
            [
                (line, f"{hour}:33", destination, "A")
                for hour in (1, 2, 2, 3)
                for line, destination in (("N53", "Vajnory"), ("N55", "Rača"))
            ],
        ),
        DST_DAY,
        TZ,
    )
    assert [dep.departure.astimezone(UTC).strftime("%H:%M") for dep in a33] == [
        time for time in ("23:33", "00:33", "01:33", "02:33") for _line in (1, 2)
    ]

    # 02:28 summer time: the feed lists the N44 of 02:48 summer time.
    at = datetime(2026, 10, 25, 0, 28, tzinfo=UTC)
    now = at.astimezone(TZ)
    realtime = parse_tabs([tabs(213, [row("N44", 20, "Koliba", now=at, delay=0)])], LABELS, now)
    supplement = make_timetable(unmarked).supplement(realtime, now, LABELS)
    assert [
        (dep.departure.isoformat(timespec="minutes"), dep.text)
        for dep in DepartureFilter().apply(supplement, now)
    ] == [("2026-10-25T02:48+01:00", "~02:48"), ("2026-10-25T03:48+01:00", "~03:48")]


def test_identical_rows_one_per_realtime_departure() -> None:
    """A line twice in a minute to one place (a reinforcement trip): each trip hides one row."""
    rows = parse_timetable_page(
        timetable_page(
            SUNDAY,
            [("93", "7:20", "Hlavná stanica", "C")] * 2 + [("93", "7:35", "Hlavná stanica", "C")],
        ),
        SUNDAY,
        TZ,
    )
    at = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    now = at.astimezone(TZ)
    # Both scheduled 07:20, the second 2 minutes late.
    first, second = (
        parse_tabs(
            [tabs(215, [row("93", 20 + delay, "Hlavná stanica", now=at, delay=delay, trip=trip)])],
            LABELS,
            now,
        )
        for trip, delay in ((1, 0), (2, 2))
    )

    def scheduled(timetable: Timetable, realtime: list[Departure]) -> list[str]:
        return [
            dep.departure.strftime("%H:%M") for dep in timetable.supplement(realtime, now, LABELS)
        ]

    assert scheduled(make_timetable(rows), first + second) == ["07:35"]
    timetable = make_timetable(rows)
    for _ in range(2):
        assert scheduled(timetable, first) == ["07:20", "07:35"]
    # It left early: its row stays hidden; the other trip, listed later, hides the other.
    assert scheduled(timetable, []) == ["07:20", "07:35"]
    assert scheduled(timetable, second) == ["07:35"]


def test_known_platform_before_an_unknown_one() -> None:
    """Of two rows in the minute, the one on the departure's platform, not one not known yet.

    Spišská Nová Ves: the page's "2" is learned to be the board's 10 (11484)
    from the 10:44; its "1" isn't known.
    """
    rows = parse_timetable_page(
        timetable_page(
            SUNDAY,
            [
                ("4", "10:44", "Sídlisko Východ", "2"),
                ("4", "11:15", "Sídlisko Východ", "2"),
                ("4", "11:15", "Sídlisko Východ", "1"),
            ],
        ),
        SUNDAY,
        TZ,
    )
    at = datetime(2026, 10, 4, 8, 40, tzinfo=UTC)
    now = at.astimezone(TZ)
    trips = [row("4", minutes, "Sídlisko Východ", now=at, trip=minutes) for minutes in (4, 35)]
    realtime = parse_tabs([tabs(11484, trips, stop=1560)], SN_LABELS, now)
    timetable = make_timetable(rows)
    # The 11:15 alone: neither platform is known, it hides neither row.
    assert len(list(timetable.supplement(realtime[1:], now, SN_LABELS))) == 3
    # Then with the 10:44, which teaches the page's "2" (also listed after the 11:15).
    supplement = list(timetable.supplement(realtime, now, SN_LABELS))
    assert timetable.learned_platforms == {"2": "11484"}
    assert [(*clock([dep])[0], dep.platform, dep.platform_id) for dep in supplement] == [
        ("4", "11:15", "1", None)
    ]


def test_listed_before_its_page_came() -> None:
    """A departure the feed listed and dropped before its page came stays hidden."""
    now, realtime = snapshot("tabs_sn_1560_snapshot", SN_LABELS)
    timetable = Timetable()
    assert list(timetable.supplement(realtime, now, SN_LABELS)) == []
    # The 10:44 left (early) or was cancelled; then the page comes.
    assert clock(realtime[:1]) == [("4", "10:44")]
    timetable.update([TimetablePage(day=SUNDAY, departures=tuple(page("departures_sn_1560")))])
    supplement = list(timetable.supplement(realtime[1:], now, SN_LABELS))
    assert len(supplement) == 35
    assert ("4", "10:44") not in clock(supplement)
    # Forgotten once past (with the rows they covered).
    later = now + timedelta(hours=3)
    assert len(list(timetable.supplement([], later, SN_LABELS))) == len(
        [
            dep
            for dep in page("departures_sn_1560")
            if dep.departure >= later - timedelta(seconds=30)
        ]
    )
    assert timetable._listed == {}


async def test_times_in_home_assistant_time_zone(hass: HomeAssistant) -> None:
    """The pages are read in Bratislava time, the departures are in HA's like the feed's."""
    await hass.config.async_set_time_zone("UTC")
    now, realtime = snapshot("tabs_sn_1560_snapshot", SN_LABELS)
    every = DepartureFilter()
    merged = sorted(
        every.apply(realtime, now)
        + every.apply(
            make_timetable(page("departures_sn_1560")).supplement(realtime, now, SN_LABELS), now
        ),
        key=lambda dep: dep.departure,
    )
    assert {dep.departure.tzinfo for dep in merged} == {dt_util.get_default_time_zone()}
    times = [dep.as_dict()["time"] for dep in merged]
    assert times == sorted(times)
    # 12:18 in Bratislava.
    assert (merged[7].source, times[7], merged[7].text) == ("timetable", "10:18", "~10:18")
    # The passes of the DST day are still told apart.
    n31 = [dep for dep in page("departures_ba_83_dst", DST_DAY) if dep.line == "N31"]
    assert [dep.departure.isoformat(timespec="minutes") for dep in n31] == [
        "2026-10-24T23:32+00:00",
        "2026-10-25T00:18+00:00",
        "2026-10-25T00:32+00:00",
        "2026-10-25T01:18+00:00",
        "2026-10-25T01:32+00:00",
        "2026-10-25T02:18+00:00",
    ]


def test_backoff() -> None:
    """Errors back off from 5 to 60 minutes; a missing page waits a day."""
    cache = TimetableCache()
    delays = []
    for failures in range(1, 7):
        cache.failures = failures
        delays.append(cache.refresh_after())
    assert (
        delays == [TIMETABLE_MIN_INTERVAL * n for n in (1, 2, 4, 8)] + [TIMETABLE_BACKOFF_MAX] * 2
    )
    cache.not_found = True
    assert cache.refresh_after() == TIMETABLE_NOT_FOUND_RETRY


# ----------------------------------------------------------- coordinator


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_scheduled_departures_fill_the_board(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Realtime first; the page fills in what the feed doesn't list."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, SATURDAY_ROWS)
    await setup_entry(hass, config_entry)
    assert [d["line"] for d in departures_attr(hass)] == ["4", "9", "X13", "N33"]
    last_update = hass.states.get(MAIN).attributes["last_update"]

    await jump(hass, freezer, START)
    assert timetable_server.requests == [("ba", 83, SATURDAY)]
    departures = departures_attr(hass)
    assert [(d["line"], d["time"], d["source"]) for d in departures] == [
        ("4", "21:01", "realtime"),
        # Expected 21:04: its scheduled 21:02 isn't listed again.
        ("9", "21:04", "realtime"),
        ("X13", "21:12", "realtime"),
        ("4", "21:16", "timetable"),
        ("9", "21:17", "timetable"),
        ("50", "21:20", "timetable"),
        ("N33", "21:25", "realtime"),
        ("4", "21:31", "timetable"),
        ("9", "21:32", "timetable"),
        ("X13", "21:42", "timetable"),
    ]
    assert departures[3] == {
        "line": "4",
        "destination": "Dúbravka",
        "destination_city": "Bratislava",
        "departure": "2026-10-03T21:16:00+02:00",
        "scheduled": "2026-10-03T21:16:00+02:00",
        "time": "21:16",
        "scheduled_time": "21:16",
        "minutes": 15,
        "leave_in": 15,
        "delay": None,
        "realtime": False,
        "source": "timetable",
        "platform": "B",
        "vehicle": None,
        "low_floor": None,
        "air_conditioning": None,
        "stuck": False,
        "text": "~15 min",
        "trip_id": None,
        "platform_id": "214",
        "terminal": "Dúbravka",
        "previous_stop": None,
        "stops_away": None,
        "vehicle_type": None,
    }
    # Aupark (C): no realtime departure there; the page uses the board's labels.
    assert (departures[5]["platform"], departures[5]["platform_id"]) == ("C", "215")
    state = hass.states.get(MAIN)
    assert state.state == "0"
    assert state.attributes["departure_count"] == 10
    assert state.attributes["last_update"] != last_update
    assert hass.states.get("sensor.hodzovo_departure_3").attributes["source"] == "realtime"

    # The countdown tick counts down scheduled departures too.
    await jump(hass, freezer, timedelta(seconds=30))
    assert departures_attr(hass)[3]["minutes"] == 14


@pytest.mark.usefixtures("midpoint_jitter")
async def test_small_town_stop(
    hass: HomeAssistant,
    fake_feed: type[FakeFeed],
    aioclient_mock: AiohttpClientMocker,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Spišská Nová Ves: the feed lists 8 departures, the page the rest of the day."""
    freezer.move_to(datetime(2026, 10, 4, 10, 33, tzinfo=TZ))
    aioclient_mock.get(f"{BASE_URL}/sn/online-zastavkova-tabula?st=1560", exc=TimeoutError())
    aioclient_mock.get(re.compile("vsetky-odchody"), side_effect=timetable_server.respond)
    timetable_server.pages[SUNDAY] = load_fixture("departures_sn_1560.html")
    fake_feed.initial = load_json("tabs_sn_1560_snapshot.json")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Stanica",
        unique_id="sn_1560_stanica",
        data={
            CONF_SECTION: "sn",
            CONF_STOP_ID: 1560,
            CONF_NAME: "Stanica",
            CONF_STOP_NAME: "Autobusová stanica",
            CONF_STOP_CITY: "Spišská Nová Ves",
            CONF_PLATFORM_LABELS: SN_LABELS,
        },
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    await setup_entry(hass, entry)
    assert len(departures_attr(hass, "sensor.stanica_departures")) == 8

    await jump(hass, freezer, START)
    assert timetable_server.requests == [("sn", 1560, SUNDAY)]
    departures = departures_attr(hass, "sensor.stanica_departures")
    assert [(d["line"], d["time"], d["source"][0], d["platform"]) for d in departures] == [
        ("4", "10:44", "r", "10"),
        ("12", "11:00", "r", "11"),
        ("4", "11:15", "r", "12"),
        ("2", "11:30", "r", "11"),
        ("5", "11:37", "r", "10"),
        ("4", "11:44", "r", "10"),
        ("12", "12:08", "r", "11"),
        ("4", "12:18", "t", "12"),
        ("15", "12:31", "r", "12"),
        ("4", "12:44", "t", "10"),
    ]


@pytest.mark.usefixtures("fake_feed", "mock_http")
async def test_option_off_fetches_nothing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without the option only the realtime departures are listed."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, SATURDAY_ROWS)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_TIMETABLE: False}
    )
    await setup_entry(hass, config_entry)
    # Past the first fetch and a refresh, had the option been on.
    while dt_util.utcnow() < NOW + START + REFRESH + TIMETABLE_JITTER:
        await jump(hass, freezer, timedelta(minutes=30))
    assert timetable_server.requests == []
    assert config_entry.runtime_data.timetable is None
    assert hass.states.get(MAIN).attributes["departure_count"] == 0
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["timetable"] == {"enabled": False}


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_refresh_and_backoff(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every REFRESH; on errors the rows are kept and retries back off.

    From TIMETABLE_MIN_INTERVAL, doubled after each failed attempt.
    """
    freezer.move_to(MORNING)
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START - timedelta(seconds=1))
    assert timetable_server.requests == []
    await jump(hass, freezer, timedelta(seconds=1))
    assert len(timetable_server.requests) == 1

    await jump(hass, freezer, REFRESH - timedelta(seconds=1))
    assert len(timetable_server.requests) == 1
    await jump(hass, freezer, timedelta(seconds=1))
    assert timetable_server.days == [SATURDAY, SATURDAY]

    timetable_server.status = HTTPStatus.SERVICE_UNAVAILABLE
    await jump(hass, freezer, REFRESH)
    assert len(timetable_server.requests) == 3
    # The realtime departures are hours away: the list is the page's.
    assert [d["source"] for d in departures_attr(hass)] == ["timetable"] * 10
    for retry in (TIMETABLE_MIN_INTERVAL, 2 * TIMETABLE_MIN_INTERVAL):
        await jump(hass, freezer, retry - timedelta(seconds=1))
        count = len(timetable_server.requests)
        await jump(hass, freezer, timedelta(seconds=1))
        assert len(timetable_server.requests) == count + 1
    assert "Cannot fetch the scheduled departures of stop Hodžovo nám." in caplog.text
    # The page fetched before is still used.
    assert hass.states.get(MAIN).attributes["departure_count"] == 10
    diagnostics = (await async_get_config_entry_diagnostics(hass, config_entry))["timetable"]
    assert diagnostics["failures"] == 3
    assert "503" in diagnostics["last_error"]
    assert diagnostics["rows"] == {"2026-10-03": len(EVERY_3_MIN)}

    timetable_server.status = HTTPStatus.OK
    await jump(hass, freezer, 4 * TIMETABLE_MIN_INTERVAL)
    assert len(timetable_server.requests) == 6
    diagnostics = (await async_get_config_entry_diagnostics(hass, config_entry))["timetable"]
    assert (diagnostics["failures"], diagnostics["last_error"]) == (0, None)
    assert diagnostics["last_fetch"] == diagnostics["last_attempt"]
    assert diagnostics["learned_platforms"] == {}
    await jump(hass, freezer, REFRESH - timedelta(seconds=1))
    assert len(timetable_server.requests) == 6
    await jump(hass, freezer, timedelta(seconds=1))
    assert len(timetable_server.requests) == 7


@pytest.mark.usefixtures("fake_feed", "mock_http")
async def test_refresh_interval_has_jitter(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The first fetch within TIMETABLE_START_DELAY, then every REFRESH ± TIMETABLE_JITTER."""
    freezer.move_to(MORNING)
    earliest, latest = (timedelta(seconds=delay) for delay in TIMETABLE_START_DELAY)
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, earliest - timedelta(seconds=1))
    assert timetable_server.requests == []
    await jump(hass, freezer, latest - earliest + timedelta(seconds=1))
    assert len(timetable_server.requests) == 1
    # REFRESH - TIMETABLE_JITTER after setup: not yet, even after the earliest first fetch.
    await jump(hass, freezer, REFRESH - TIMETABLE_JITTER - latest)
    assert len(timetable_server.requests) == 1
    # REFRESH + TIMETABLE_JITTER after the latest first fetch: fetched again.
    await jump(hass, freezer, 2 * TIMETABLE_JITTER + latest)
    assert len(timetable_server.requests) == 2


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_running_short_fetches_tomorrow(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Fewer departures than the list holds: tomorrow's page, once, soon after a fetch.

    Checked with the countdown, every 30 s, from TIMETABLE_MIN_INTERVAL after
    the last fetch, and fetched within TIMETABLE_JITTER after (at random: the
    ticks of all stops are in step), long before the regular refresh.
    """
    timetable_server.pages[SATURDAY] = timetable_page(
        SATURDAY, [("9", "23:40", "Karlova Ves", "A")]
    )
    timetable_server.pages[SUNDAY] = timetable_page(
        SUNDAY, [("N31", "0:02", "Cintorín Slávičie", "D")]
    )
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    assert timetable_server.days == [SATURDAY]
    assert [d["time"] for d in departures_attr(hass)][-1] == "23:40"

    await jump(hass, freezer, TIMETABLE_MIN_INTERVAL - timedelta(seconds=10))
    assert timetable_server.days == [SATURDAY]
    # The first tick TIMETABLE_MIN_INTERVAL after the fetch finds it missing.
    await jump(hass, freezer, timedelta(seconds=40))
    assert timetable_server.days == [SATURDAY]
    await jump(hass, freezer, SOON - timedelta(seconds=1))
    assert timetable_server.days == [SATURDAY]
    await jump(hass, freezer, timedelta(seconds=1))
    assert timetable_server.days == [SATURDAY, SUNDAY]
    early = TIMETABLE_MIN_INTERVAL + timedelta(seconds=30) + SOON
    assert timetable_server.times[1] - timetable_server.times[0] == early
    assert early < REFRESH - TIMETABLE_JITTER
    last = departures_attr(hass)[-1]
    assert (last["line"], last["time"], last["text"]) == ("N31", "00:02", "~00:02")
    # Not again: the next refresh fetches today's page only.
    await jump(hass, freezer, REFRESH)
    assert timetable_server.days == [SATURDAY, SUNDAY, SATURDAY]

    # After midnight Sunday's page is today's; Monday's is fetched as the list is
    # short, soon after the first tick (long before the next refresh). Sunday's
    # page, fetched more than REFRESH - TIMETABLE_JITTER before, is refreshed with it.
    freezer.move_to(datetime(2026, 10, 4, 0, 1, tzinfo=TZ))
    async_fire_time_changed(hass)
    await settle(hass)
    assert timetable_server.days[3:] == []
    assert [d["line"] for d in departures_attr(hass)] == ["N31"]
    await jump(hass, freezer, SOON)
    assert timetable_server.days[3:] == [SUNDAY, MONDAY]


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_tomorrows_page_does_not_put_off_todays_refresh(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Today's page is refreshed REFRESH after its own last fetch.

    Tomorrow's page, fetched in between as the list runs short, doesn't move
    it (that would put today's refresh off by up to another REFRESH).
    """
    # Today's departures until 22:00: the list runs short at about 21:30.
    timetable_server.pages[SATURDAY] = timetable_page(
        SATURDAY, [("9", f"21:{minute:02}", "Karlova Ves", "A") for minute in range(5, 60, 3)]
    )
    timetable_server.pages[SUNDAY] = timetable_page(
        SUNDAY, [("N31", f"{hour}:02", "Cintorín Slávičie", "D") for hour in range(10)]
    )
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    first = timetable_server.times[0]

    async def next_attempt() -> str | None:
        diagnostics = (await async_get_config_entry_diagnostics(hass, config_entry))["timetable"]
        return diagnostics["next_attempt"]

    assert await next_attempt() == (first + REFRESH).isoformat()
    while timetable_server.days == [SATURDAY] and dt_util.utcnow() < first + REFRESH:
        await jump(hass, freezer, TICK_INTERVAL)
    assert timetable_server.days == [SATURDAY, SUNDAY]
    # Well before today's refresh: early, not with it.
    assert timetable_server.times[1] - first < REFRESH - TIMETABLE_JITTER
    assert await next_attempt() == (first + REFRESH).isoformat()

    await jump(hass, freezer, first + REFRESH - dt_util.utcnow() - timedelta(seconds=1))
    assert timetable_server.days == [SATURDAY, SUNDAY]
    await jump(hass, freezer, timedelta(seconds=1))
    assert timetable_server.days == [SATURDAY, SUNDAY, SATURDAY]
    assert timetable_server.times[2] - first == REFRESH
    assert await next_attempt() == (first + 2 * REFRESH).isoformat()


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_identical_refetch_does_not_publish(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The same page again writes no state; a changed one does (and moves last_update)."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    coordinator: ImhdCoordinator = config_entry.runtime_data
    changes: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        changes.append(event)

    async def refetch() -> None:
        """Fetch again now (as if the last fetch was a refresh interval ago)."""
        age(coordinator.timetable_cache, REFRESH)
        await coordinator._async_update_timetable(scheduled=True)
        await hass.async_block_till_done()

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    data = coordinator.data
    await refetch()
    assert len(timetable_server.requests) == 2
    assert coordinator.data is data
    assert changes == []

    timetable_server.pages[SATURDAY] = timetable_page(
        SATURDAY, [*EVERY_3_MIN, ("70", "21:10", "Nivy", "C")]
    )
    freezer.tick(timedelta(seconds=5))
    await refetch()
    assert len(timetable_server.requests) == 3
    assert coordinator.data is not data
    assert "70" in [d["line"] for d in departures_attr(hass)]
    assert coordinator.data.last_update == datetime.now(TZ)
    assert MAIN in {event.data["entity_id"] for event in changes}


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_entries_of_a_stop_share_the_pages(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two entries of one stop fetch once; a reload uses the pages at once."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, SATURDAY_ROWS)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Center",
        unique_id="ba_83_center",
        data={**config_entry.data, CONF_NAME: "Center"},
        options=dict(DEFAULT_OPTIONS),
    )
    other.add_to_hass(hass)
    # Sets up both entries.
    await setup_entry(hass, config_entry)
    assert len(fake_feed.instances) == 2
    await jump(hass, freezer, START)
    assert len(timetable_server.requests) == 1
    for entity_id in (MAIN, "sensor.center_departures"):
        assert hass.states.get(entity_id).attributes["departure_count"] == 10

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_MAX_DEPARTURES: 12}
    )
    await settle(hass)
    assert len(fake_feed.instances) == 3
    assert hass.states.get(MAIN).attributes["departure_count"] == 12
    assert len(timetable_server.requests) == 1


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_stop_without_a_page(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """imhd.sk's error page: one warning, the next try a day later, realtime as usual."""
    for day in (SATURDAY, SUNDAY, MONDAY):
        timetable_server.pages[day] = load_fixture("departures_not_found.html")
    await setup_entry(hass, config_entry)
    with caplog.at_level(logging.WARNING):
        await jump(hass, freezer, START)
    assert len(timetable_server.requests) == 1
    assert caplog.text.count("has no scheduled departures for stop Hodžovo nám.") == 1
    assert hass.states.get(MAIN).attributes["departure_count"] == 4
    await jump(hass, freezer, TIMETABLE_NOT_FOUND_RETRY - timedelta(minutes=1))
    assert len(timetable_server.requests) == 1
    await jump(hass, freezer, timedelta(minutes=1))
    assert len(timetable_server.requests) == 2


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_get_departures_lists_all_scheduled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Entities get as many departures as they show; `imhd.get_departures` all of them."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, SATURDAY_ROWS)
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, CONF_MAX_DEPARTURES: 2, CONF_DEPARTURE_SENSORS: 0},
    )
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    # Published as far as entities show them (no new state for the others).
    assert len(config_entry.runtime_data.build().matching) == 4 + 2
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["departures"]["total_after_filters"] == 4 + 9
    assert len(diagnostics["departures"]["published"]) == 2
    response = await hass.services.async_call(
        DOMAIN,
        "get_departures",
        {"config_entry_id": config_entry.entry_id, "limit": 30, "line": ["9"]},
        blocking=True,
        return_response=True,
    )
    assert [(d["time"], d["source"]) for d in response["departures"]] == [
        ("21:04", "realtime"),
        ("21:17", "timetable"),
        ("21:32", "timetable"),
        ("21:47", "timetable"),
    ]


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_get_departures_direction_matches_the_page_text(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The feed's "Kapitulský dvor" is the page's "Petržalka, Kapitulský dvor"."""
    freezer.move_to(datetime(2026, 10, 4, 0, 0, tzinfo=TZ))
    timetable_server.pages[SUNDAY] = load_fixture("departures_ba_83.html")
    fake_feed.initial = night_trip("N80")
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    response = await hass.services.async_call(
        DOMAIN,
        "get_departures",
        {"config_entry_id": config_entry.entry_id, "line": ["N80"], "direction": "petrzalka"},
        blocking=True,
        return_response=True,
    )
    assert [(d["line"], d["time"], d["source"]) for d in response["departures"]] == [
        ("N80", "00:02", "realtime"),
        ("N80", "00:32", "timetable"),
    ]
    assert response["departures"][0]["destination"] == "Kapitulský dvor"


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_reload_keeps_the_departures_the_feed_listed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A departure the feed listed, then dropped (cancelled), stays out after a reload."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, SATURDAY_ROWS)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    assert ("X13", "21:12", "realtime") in [
        (d["line"], d["time"], d["source"]) for d in departures_attr(hass)
    ]
    without_x13 = [tabs(213, [row("9", 4, "Karlova Ves", delay=2, trip=1)]), sample_tabs()[1]]
    fake_feed.instances[-1].listener.feed_tabs(without_x13)
    await settle(hass)
    assert [d["time"] for d in departures_attr(hass) if d["line"] == "X13"] == ["21:42"]

    # An options change reloads the entry; its new session lists the same board.
    fake_feed.initial = without_x13
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_MAX_DEPARTURES: 11}
    )
    await settle(hass)
    assert len(fake_feed.instances) == 2
    assert [d["time"] for d in departures_attr(hass) if d["line"] == "X13"] == ["21:42"]
    assert len(departures_attr(hass)) == 11
    assert len(timetable_server.requests) == 1


@pytest.mark.parametrize("refused", [True, False], ids=["refused", "never connects"])
@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_no_requests_while_unavailable(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    *,
    refused: bool,
) -> None:
    """No page while the entities are unavailable; soon (at random) once the feed is back."""
    freezer.move_to(MORNING)
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    if refused:
        fake_feed.reject = "Too many connections"
    else:

        async def never_connects(self: FakeFeed) -> None:
            self.sessions += 1
            self.listener.feed_disconnected()
            await self.stopped.wait()

        fake_feed.run = never_connects  # type: ignore[method-assign]
    await setup_entry(hass, config_entry)
    # Past the first fetch and a refresh.
    while dt_util.utcnow() < MORNING + START + REFRESH + TIMETABLE_JITTER:
        await jump(hass, freezer, timedelta(minutes=10))
    assert hass.states.get(MAIN).state == "unavailable"
    assert timetable_server.requests == []

    listener = fake_feed.instances[-1].listener
    listener.feed_connected()
    listener.feed_tabs(sample_tabs())
    # The tick resumes the refresh: within TIMETABLE_JITTER, not at the tick.
    await jump(hass, freezer, TICK_INTERVAL)
    assert timetable_server.requests == []
    await jump(hass, freezer, SOON)
    assert len(timetable_server.requests) == 1
    assert hass.states.get(MAIN).attributes["departure_count"] == 10
    # The refresh runs as usual again.
    await jump(hass, freezer, REFRESH)
    assert len(timetable_server.requests) == 2


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_not_found_is_retried_like_an_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a page of the stop, "not found" is retried like an error; three in a row: a day."""
    freezer.move_to(MORNING)
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    timetable_server.status = HTTPStatus.NOT_FOUND
    await jump(hass, freezer, REFRESH)
    assert len(timetable_server.requests) == 2
    diagnostics = (await async_get_config_entry_diagnostics(hass, config_entry))["timetable"]
    assert (diagnostics["failures"], diagnostics["not_found"]) == (1, False)
    assert diagnostics["next_attempt"] == (dt_util.utcnow() + TIMETABLE_MIN_INTERVAL).isoformat()

    timetable_server.status = HTTPStatus.OK
    await jump(hass, freezer, TIMETABLE_MIN_INTERVAL)
    assert len(timetable_server.requests) == 3

    timetable_server.status = HTTPStatus.NOT_FOUND
    with caplog.at_level(logging.WARNING):
        for delay in (REFRESH, TIMETABLE_MIN_INTERVAL, 2 * TIMETABLE_MIN_INTERVAL):
            await jump(hass, freezer, delay)
    assert len(timetable_server.requests) == 6
    assert caplog.text.count("has no scheduled departures for stop Hodžovo nám.") == 1
    diagnostics = (await async_get_config_entry_diagnostics(hass, config_entry))["timetable"]
    assert diagnostics["not_found"] is True
    assert diagnostics["next_attempt"] == (dt_util.utcnow() + TIMETABLE_NOT_FOUND_RETRY).isoformat()
    # The page fetched before is still used.
    assert hass.states.get(MAIN).attributes["departure_count"] == 10


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_missing_tomorrow_keeps_today_refreshed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only tomorrow's page not found: retried with the errors' back-off, never a day.

    Today's page is fetched again only when its own refresh is due, and no
    warning is logged: today's departures are all shown.
    """
    timetable_server.pages[SATURDAY] = timetable_page(
        SATURDAY, [("9", "23:40", "Karlova Ves", "A")]
    )
    timetable_server.pages[SUNDAY] = load_fixture("departures_not_found.html")
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    first = timetable_server.times[0]
    minute = timedelta(minutes=1)
    # Today's refresh is due within this (at most a retry later), before midnight.
    end = REFRESH - TIMETABLE_JITTER + TIMETABLE_BACKOFF_MAX
    with caplog.at_level(logging.DEBUG, "custom_components.imhd"):
        while dt_util.utcnow() < first + end:
            await jump(hass, freezer, minute)
    # Tomorrow's page soon after the tick TIMETABLE_MIN_INTERVAL after the first
    # request (the list is short; on the next minute), then TIMETABLE_MIN_INTERVAL
    # after the previous attempt, doubled after each one up to
    # TIMETABLE_BACKOFF_MAX; today's with the first of these once its own refresh
    # is due (REFRESH - TIMETABLE_JITTER after its last fetch).
    sunday = [math.ceil((TIMETABLE_MIN_INTERVAL + SOON) / minute) * minute]
    while (
        retry := sunday[-1]
        + min(TIMETABLE_MIN_INTERVAL * 2 ** (len(sunday) - 1), TIMETABLE_BACKOFF_MAX)
    ) <= end:
        sunday.append(retry)
    refresh = next(when for when in sunday if when >= REFRESH - TIMETABLE_JITTER)
    assert [
        (day, when - first)
        for day, when in zip(timetable_server.days, timetable_server.times, strict=True)
    ] == [
        (SATURDAY, timedelta(0)),
        *(
            (day, when)
            for when in sunday
            for day in ((SATURDAY, SUNDAY) if when == refresh else (SUNDAY,))
        ),
    ]
    assert config_entry.runtime_data.timetable_cache.not_found is False
    failed = [r for r in caplog.records if r.getMessage().startswith("Cannot fetch")]
    assert len(failed) == len(sunday)
    assert {r.levelno for r in failed} == {logging.DEBUG}
    assert "Hodžovo nám. (2026-10-04)" in failed[0].getMessage()


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_reload_during_a_request_does_not_request_again(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    slow_fetch: SlowFetch,
) -> None:
    """A request cancelled by a reload counts: the next one 5 minutes later at the earliest."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    slow_fetch.released.clear()
    await setup_entry(hass, config_entry)
    for _ in range(3):
        freezer.tick(START)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        await slow_fetch.async_wait_started(hass, 1)
        freezer.tick(timedelta(seconds=3))
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
    assert len(slow_fetch.started) == 1
    first = slow_fetch.started[0][0]
    slow_fetch.released.set()
    while dt_util.utcnow() < first + TIMETABLE_MIN_INTERVAL + TICK_INTERVAL + SOON:
        await jump(hass, freezer, TICK_INTERVAL)
    assert len(slow_fetch.started) == 2
    assert slow_fetch.started[1][0] - first >= TIMETABLE_MIN_INTERVAL
    assert hass.states.get(MAIN).attributes["departure_count"] == 10

    # A refresh in flight: the reloaded entry uses the pages and waits for the next one.
    slow_fetch.released.clear()
    freezer.tick(REFRESH)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await slow_fetch.async_wait_started(hass, 3)
    assert len(slow_fetch.started) == 3
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    slow_fetch.released.set()
    for _ in range(10):
        await jump(hass, freezer, TICK_INTERVAL)
    assert len(slow_fetch.started) == 3
    assert hass.states.get(MAIN).attributes["departure_count"] == 10


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_entries_of_a_stop_share_a_fetched_page(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A page fetched for one entry of a stop is the other's with its next tick."""
    timetable_server.pages[SATURDAY] = timetable_page(
        SATURDAY, [("9", "23:40", "Karlova Ves", "A")]
    )
    timetable_server.pages[SUNDAY] = timetable_page(
        SUNDAY, [("9", f"0:{minute:02}", "Karlova Ves", "A") for minute in range(5, 60, 5)]
    )
    second_entry(hass, config_entry)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    assert len(departures_attr(hass, "sensor.center_departures")) == 5
    # The first entry's refresh, with tomorrow's page as the list is short.
    coordinator: ImhdCoordinator = config_entry.runtime_data
    age(coordinator.timetable_cache, REFRESH)
    await coordinator._async_update_timetable(scheduled=True)
    assert timetable_server.days == [SATURDAY, SATURDAY, SUNDAY]
    await jump(hass, freezer, TICK_INTERVAL)
    assert len(departures_attr(hass)) == 10
    assert departures_attr(hass, "sensor.center_departures") == departures_attr(hass)
    assert len(timetable_server.requests) == 3


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_pages_no_entry_shows_are_released(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The pages go with the last entry showing them; the request rate is kept."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    other = second_entry(hass, config_entry)
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    cache = hass.data[DATA_TIMETABLES][("ba", 83)]
    assert cache.pages
    await hass.config_entries.async_reload(config_entry.entry_id)
    await settle(hass)
    assert await hass.config_entries.async_remove(other.entry_id)
    await settle(hass)
    assert cache.pages
    assert cache.timetable is not None

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_TIMETABLE: False}
    )
    await settle(hass)
    assert (cache.pages, cache.timetable) == ({}, None)
    assert hass.data[DATA_TIMETABLES][("ba", 83)] is cache
    # On again: not before 5 minutes after the last request.
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_TIMETABLE: True}
    )
    await settle(hass)
    await jump(hass, freezer, START)
    assert len(timetable_server.requests) == 1
    await jump(hass, freezer, TIMETABLE_MIN_INTERVAL - START)
    assert len(timetable_server.requests) == 1
    await jump(hass, freezer, SOON)
    assert len(timetable_server.requests) == 2
    assert cache.pages

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await settle(hass)
    assert (cache.pages, cache.timetable) == ({}, None)


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_clock_order_within_a_countdown_minute(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Both leave in 23 minutes: the scheduled 21:24 before the realtime 21:25.

    The realtime one is expected 21:24:50, which imhd.sk shows as 21:25.
    """
    timetable_server.pages[SATURDAY] = timetable_page(
        SATURDAY, [("9", "21:24", "Karlova Ves", "A")]
    )
    fake_feed.initial = [tabs(214, [row("44", 24 + 50 / 60, "Koliba", delay=0)])]
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    data = config_entry.runtime_data.build(NOW + timedelta(seconds=55))
    assert [(dep.line, dep.minutes, dep.as_dict()["time"]) for dep in data.matching] == [
        ("9", 23, "21:24"),
        ("44", 23, "21:25"),
    ]


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_dropped_before_its_page_came(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A trip the feed listed and dropped (cancelled) before its page came stays out.

    The feed lists N31 00:17 at 23:00 and drops it at 23:30; Sunday's page comes later.
    """
    evening = datetime(2026, 10, 3, 23, 0, tzinfo=TZ)
    freezer.move_to(evening)
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    timetable_server.pages[SUNDAY] = timetable_page(
        SUNDAY, [("N31", f"0:{minute}", "Cintorín Slávičie", "D") for minute in (17, 47)]
    )
    fake_feed.initial = [
        tabs(216, [row("N31", 77, "Cintorín Slávičie", now=evening, delay=0, trip=7)])
    ]
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    assert timetable_server.days == [SATURDAY]
    freezer.move_to(datetime(2026, 10, 3, 23, 30, tzinfo=TZ))
    fake_feed.instances[-1].listener.feed_tabs([tabs(216, [])])
    while dt_util.now() < datetime(2026, 10, 4, 0, 8, tzinfo=TZ):
        await jump(hass, freezer, TICK_INTERVAL)
    assert SUNDAY in timetable_server.days
    assert [(d["time"], d["source"]) for d in departures_attr(hass) if d["line"] == "N31"] == [
        ("00:47", "timetable")
    ]


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_dropped_before_the_first_page(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A trip dropped in the seconds before the first page was fetched stays out."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, SATURDAY_ROWS)
    await setup_entry(hass, config_entry)
    without_x13 = [tabs(213, [row("9", 4, "Karlova Ves", delay=2, trip=1)]), sample_tabs()[1]]
    fake_feed.instances[-1].listener.feed_tabs(without_x13)
    await jump(hass, freezer, START)
    assert timetable_server.requests == [("ba", 83, SATURDAY)]
    assert [d["time"] for d in departures_attr(hass) if d["line"] == "X13"] == ["21:42"]


@pytest.mark.usefixtures("mock_http")
async def test_stops_back_from_an_outage_fetch_apart(
    hass: HomeAssistant,
    fake_feed: type[FakeFeed],
    aioclient_mock: AiohttpClientMocker,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Stops whose feeds come back together fetch their pages at random within TIMETABLE_JITTER.

    Not all in the tick they are back with: imhd.sk has just recovered.
    """
    freezer.move_to(MORNING)
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    entries = []
    for stop in range(83, 88):
        if stop != 83:
            aioclient_mock.get(f"{STOP_PAGE_URL}?st={stop}", exc=TimeoutError())
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=f"Stop {stop}",
            unique_id=f"ba_{stop}",
            data={**entry_data(f"Stop {stop}"), CONF_STOP_ID: stop},
            options=dict(DEFAULT_OPTIONS),
        )
        entry.add_to_hass(hass)
        entries.append(entry)
    with patch("custom_components.imhd.coordinator.random", Random(83)):  # noqa: S311
        # Sets up all of them.
        await setup_entry(hass, entries[0])
        await jump(hass, freezer, timedelta(minutes=1))
        assert sorted(stop for _section, stop, _day in timetable_server.requests) == [
            83,
            84,
            85,
            86,
            87,
        ]
        # imhd.sk is down until every stop's refresh came due: the refresh pauses.
        for feed in fake_feed.instances:
            feed.disconnect()
        while dt_util.utcnow() < MORNING + REFRESH + 2 * TIMETABLE_JITTER:
            await jump(hass, freezer, timedelta(minutes=1))
        assert len(timetable_server.requests) == 5
        back = dt_util.utcnow()
        for feed in fake_feed.instances:
            feed.listener.feed_connected()
            feed.listener.feed_tabs(sample_tabs())
        while dt_util.utcnow() < back + TICK_INTERVAL + TIMETABLE_JITTER:
            await jump(hass, freezer, timedelta(seconds=1))
    resumed = timetable_server.times[5:]
    assert len(resumed) == len(set(resumed)) == 5
    assert max(resumed) - min(resumed) > 4 * TICK_INTERVAL


@pytest.mark.parametrize("share", [0.1, 0.5, 0.9])
@pytest.mark.usefixtures("fake_feed", "mock_http")
async def test_page_missing_after_midnight_is_fetched_at_random(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
    share: float,
) -> None:
    """At midnight a short list wants the new tomorrow's page: within TIMETABLE_JITTER, at random.

    Not at the first tick after midnight, when every stop of every installation
    would ask for it.
    """
    for day, clock_time in ((SATURDAY, "23:40"), (SUNDAY, "0:30"), (MONDAY, "0:30")):
        timetable_server.pages[day] = timetable_page(
            day, [("N31", clock_time, "Cintorín Slávičie", "D")]
        )

    def uniform(low: float, high: float) -> float:
        """Pick `share` of the range when fetching soon, the middle of other ranges."""
        return low + (high - low) * (share if high == TIMETABLE_JITTER.total_seconds() else 0.5)

    freezer.move_to(datetime(2026, 10, 3, 23, 0, tzinfo=TZ))
    midnight = datetime(2026, 10, 4, 0, 0, tzinfo=TZ)
    with patch("custom_components.imhd.coordinator.random") as mock_random:
        mock_random.uniform.side_effect = uniform
        await setup_entry(hass, config_entry)
        while dt_util.now() < midnight + TIMETABLE_JITTER + TICK_INTERVAL:
            await jump(hass, freezer, TICK_INTERVAL)
    # Only the missing pages: each page's refresh is due REFRESH after its fetch.
    assert timetable_server.days == [SATURDAY, SUNDAY, MONDAY]
    assert timetable_server.times[-1] - midnight == share * TIMETABLE_JITTER


@pytest.mark.usefixtures("mock_http", "midpoint_jitter")
async def test_diagnostics_show_the_next_fetch(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The next fetch as scheduled, with its jitter; none while the refresh is paused."""
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, EVERY_3_MIN)
    await setup_entry(hass, config_entry)

    async def next_fetch() -> tuple[str | None, bool]:
        diagnostics = (await async_get_config_entry_diagnostics(hass, config_entry))["timetable"]
        return diagnostics["next_attempt"], diagnostics["paused"]

    assert await next_fetch() == ((dt_util.utcnow() + START).isoformat(), False)
    await jump(hass, freezer, START)
    assert await next_fetch() == ((dt_util.utcnow() + REFRESH).isoformat(), False)
    # Unavailable 5 minutes after the feed went: the refresh due then pauses.
    fake_feed.instances[-1].disconnect()
    await jump(hass, freezer, REFRESH)
    assert len(timetable_server.requests) == 1
    assert await next_fetch() == (None, True)
    listener = fake_feed.instances[-1].listener
    listener.feed_connected()
    listener.feed_tabs(sample_tabs())
    await jump(hass, freezer, TICK_INTERVAL)
    assert await next_fetch() == ((dt_util.utcnow() + SOON).isoformat(), False)


@pytest.mark.usefixtures("fake_feed", "mock_http", "midpoint_jitter")
async def test_get_departures_looks_at_what_it_returns(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    timetable_server: TimetableServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A busy page: `imhd.get_departures` resolves only the scheduled departures it needs."""
    every_minute = [
        ("50", f"{hour}:{minute:02}", "Aupark", "C")
        for hour in (21, 22, 23)
        for minute in range(60)
    ]
    timetable_server.pages[SATURDAY] = timetable_page(SATURDAY, [*every_minute, *EVERY_3_MIN])
    await setup_entry(hass, config_entry)
    await jump(hass, freezer, START)
    with patch.object(
        Timetable, "_resolve", autospec=True, side_effect=Timetable._resolve
    ) as resolve:
        response = await hass.services.async_call(
            DOMAIN,
            "get_departures",
            {
                "config_entry_id": config_entry.entry_id,
                "line": ["9"],
                "limit": 2,
                "min_minutes": 10,
            },
            blocking=True,
            return_response=True,
        )
    assert [(d["time"], d["minutes"], d["source"]) for d in response["departures"]] == [
        ("21:11", 10, "timetable"),
        ("21:14", 13, "timetable"),
    ]
    # Line 50 once (then its route is skipped); line 9 until two leave in 10 minutes or more.
    assert resolve.call_count == 1 + 4
