"""Tests for the HTTP client and payload parsers."""

from __future__ import annotations

from datetime import datetime, timedelta

import aiohttp
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.api import (
    ImhdApi,
    ImhdConnectionError,
    ImhdInvalidStopError,
    ImhdStopNotFoundError,
    parse_info_texts,
    parse_nearest,
    parse_search,
    parse_stop_input,
    parse_stop_page,
    parse_tabs,
    resolve_section,
)
from custom_components.imhd.models import countdown_text, minutes_until

from .conftest import (
    LABELS,
    NEAREST_URL,
    NOW,
    STOP_PAGE_URL,
    load_fixture,
    load_json,
    row,
    sample_tabs,
    tabs,
)


def test_parse_stop_page_unlabeled_platforms() -> None:
    """Košice: `platformsLabels` is [] - platform ids come from the picker."""
    stop = parse_stop_page(load_fixture("stop_page_ke_1130.html"), "ke")
    assert stop.stop_id == 1130
    assert stop.name == "Nám. osloboditeľov"
    assert stop.city == "Košice"
    assert stop.platforms == [str(pid) for pid in range(2474, 2484)]


def test_parse_stop_page() -> None:
    """The board page options describe the stop and its platforms."""
    stop = parse_stop_page(load_fixture("stop_page_ba_83.html"), "ba")
    assert stop.stop_id == 83
    assert stop.name == "Hodžovo nám."
    assert stop.city == "Bratislava"
    assert stop.section == "ba"
    assert stop.platform_labels == LABELS
    assert stop.platforms == ["A", "B", "C", "D"]
    assert stop.board_url == "https://imhd.sk/ba/online-zastavkova-tabula?st=83"


@pytest.mark.parametrize("html", ["stop_page_missing.html", None])
def test_parse_stop_page_missing(html: str | None) -> None:
    """Pages without stop options mean the stop does not exist."""
    text = load_fixture(html) if html else "<html>$.extend( options, {broken</html>"
    with pytest.raises(ImhdStopNotFoundError):
        parse_stop_page(text, "ba")


def test_parse_nearest() -> None:
    """Nearest stops are parsed with distances, closest first."""
    stops = parse_nearest(load_json("nearest_ba.json"), "ba", 48.1486, 17.1077)
    assert [stop.stop_id for stop in stops] == [83, 4077, 270, 136, 222]
    first = stops[0]
    assert first.name == "Hodžovo nám."
    assert first.name_long == "Hodžovo námestie"
    assert first.platforms == ["A", "B", "C", "D"]
    assert first.distance_m == 55
    assert first.describe() == "Hodžovo nám. (Bratislava) · A, B, C, D · 55 m"
    assert first.as_dict()["lat"] == pytest.approx(48.14896)


def test_parse_search() -> None:
    """Only stop results are kept and mapped to stop ids."""
    stops = parse_search(load_json("search_ba_hodzovo.json"), "ba")
    assert [(stop.stop_id, stop.name) for stop in stops] == [(83, "Hodžovo nám.")]
    assert parse_search({}, "ba") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("83", (None, 83)),
        (83, (None, 83)),
        (" 1830 ", (None, 1830)),
        ("https://imhd.sk/ba/online-zastavkova-tabula?st=83", ("ba", 83)),
        ("imhd.sk/za/online-zastavkova-tabula?st=1831&n=12", ("za", 1831)),
        ("https://example.com/board?st=7", (None, 7)),
        ("https://imhd.sk/ba/online-zastavkova-tabula?st=83;84&pfm=213", ("ba", 83)),
        ("https://imhd.sk/ba/zastavka/Hod%C5%BEovo-n%C3%A1m/ca71b6718971878271cc", ("ba", 83)),
        ("https://imhd.sk/za/zastavka/Hurbanova/ca71b67189718087828071cc", ("za", 1831)),
    ],
)
def test_parse_stop_input(text: str | int, expected: tuple[str | None, int]) -> None:
    """Stop ids and URLs are understood."""
    assert parse_stop_input(text) == expected


@pytest.mark.parametrize(
    "text", ["Hodžovo nám.", "https://imhd.sk/ba/zastavka/x", "st=abc", "/ba/zastavka/a/zz"]
)
def test_parse_stop_input_invalid(text: str) -> None:
    """Everything else is rejected."""
    with pytest.raises(ImhdInvalidStopError):
        parse_stop_input(text)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ba", "ba"),
        ("BA", "ba"),
        ("Bratislava", "ba"),
        ("zilina", "za"),
        ("Žilina", "za"),
        ("KOŠICE", "ke"),
        ("banska bystrica", "bb"),
        ("Poprad", "tatry"),
        ("tatry", "tatry"),
        ("Atlantis", None),
        ("", None),
    ],
)
def test_resolve_section(value: str, expected: str | None) -> None:
    """Section codes and city names resolve case/diacritics-insensitively."""
    assert resolve_section(value) == expected


async def test_parse_tabs_synthetic(hass: HomeAssistant) -> None:
    """Rows are parsed, labelled and sorted by expected departure."""
    deps = parse_tabs(sample_tabs(), LABELS, NOW, stop_id=83)
    assert [dep.line for dep in deps] == ["4", "9", "X13", "N33"]
    nine = deps[1]
    assert nine.destination == "Karlova Ves"
    assert nine.platform == "A"
    assert nine.platform_id == "213"
    assert nine.realtime is True
    assert nine.delay == 2
    assert nine.minutes == 4
    assert nine.vehicle == "4401"
    assert nine.departure.tzinfo is not None
    assert nine.as_dict()["departure"] == "2026-10-03T21:04:00+02:00"
    assert nine.as_dict()["scheduled_time"] == "21:02"
    assert nine.text == "4 min"
    timetable = deps[2]
    assert timetable.realtime is False
    assert timetable.delay is None
    assert timetable.vehicle is None
    assert timetable.text == "~12 min"
    assert deps[0].text == "1 min"


async def test_parse_tabs_real_capture(hass: HomeAssistant) -> None:
    """A real Bratislava capture (online rows + vInfo) parses completely."""
    event = load_json("tabs_ba_sequence.json")[0]
    payload = event["payload"]
    now = dt_util.utc_from_timestamp(payload[0]["timestamp"] / 1000)
    vehicles = {v["issi"]: v for v in load_json("vinfo_ba.json")}
    deps = parse_tabs(payload, LABELS, now, stop_id=83, vehicles=vehicles)

    assert len(deps) == sum(len(el["tab"]) for el in payload) == 56
    assert deps == sorted(deps, key=lambda dep: dep.departure)
    assert {dep.platform for dep in deps} == {"A", "B", "C", "D"}
    assert all(dep.line and dep.destination for dep in deps)
    assert all(dep.low_floor is not None for dep in deps if dep.realtime)

    first = deps[0].as_dict()
    assert first["line"] == "44"
    assert first["destination"] == "Koliba"
    assert first["departure"] == "2026-10-03T21:32:36+02:00"
    assert first["scheduled_time"] == "21:28"
    assert first["minutes"] == 0
    assert first["text"] == "*"
    assert first["delay"] == 4
    assert first["realtime"] is True
    assert first["vehicle"] == "6126"
    assert first["vehicle_type"] == "SOR TNS 12"
    assert first["low_floor"] is True
    assert first["air_conditioning"] is True
    assert first["previous_stop"] == "Kozia"
    assert first["stops_away"] == 1
    assert first["trip_id"] == -18185594

    service_trip = next(dep for dep in deps if dep.trip_id == -18473158)
    assert service_trip.line == "►"
    assert service_trip.realtime is False
    assert service_trip.text == "~23:10"
    assert service_trip.delay is None

    # The local countdown text reproduces imhd's own texts.
    for dep, row_text in _server_texts(payload, deps):
        assert dep.text.lstrip("~") == row_text


def _server_texts(payload: list, deps: list) -> list:
    """Pair departures with the server rendered `odjazd` text of their row."""
    texts = {row["i"]: row["odjazd"] for el in payload for row in el["tab"]}
    return [
        (dep, texts[dep.trip_id])
        for dep in deps
        if not texts[dep.trip_id][0].isdigit() or texts[dep.trip_id].endswith("min")
    ]


async def test_parse_tabs_drops_departed_and_foreign(hass: HomeAssistant) -> None:
    """Departed rows (>30 s) and other stops are dropped; minutes never negative."""
    payload = [
        tabs(213, [row("1", -0.4, "Just left"), row("2", -1, "Gone"), row("3", 0.5, "Now")]),
        tabs(999, [row("4", 3, "Other stop")], stop=1),
    ]
    deps = parse_tabs(payload, LABELS, NOW, stop_id=83)
    assert [(dep.line, dep.minutes) for dep in deps] == [("1", 0), ("3", 0)]


@pytest.mark.parametrize("payload", [[], {}, None, [{"tab": []}], [{"tab": [{"linka": "1"}]}]])
async def test_parse_tabs_empty(hass: HomeAssistant, payload: object) -> None:
    """Empty / malformed payloads give no departures."""
    assert parse_tabs(payload, LABELS, NOW) == []


async def test_parse_tabs_timetable_without_labels(hass: HomeAssistant) -> None:
    """Košice: timetable-only rows and no platform labels (ids are used)."""
    payload = load_json("tabs_ke_timetable.json")
    now = dt_util.utc_from_timestamp(payload[0]["timestamp"] / 1000)
    deps = parse_tabs(payload, {}, now, stop_id=1130)
    assert len(deps) == sum(len(el["tab"]) for el in payload)
    assert not any(dep.realtime for dep in deps)
    assert all(dep.delay is None and dep.vehicle is None for dep in deps)
    assert all(dep.text and dep.text.startswith("~") for dep in deps)
    assert {dep.platform for dep in deps} <= {str(el["nastupiste"]) for el in payload}
    assert deps[0].destination_city == "Košice"
    assert parse_tabs(load_json("tabs_empty.json"), {}, now) == []


@pytest.mark.parametrize(
    ("seconds", "realtime", "text"),
    [
        (-5, True, "*"),
        (30, True, "<1 min"),
        (299, True, "4 min"),
        (299, False, "~4 min"),
        (3600, True, "60 min"),
        (3700, False, "~22:02"),
    ],
)
def test_countdown_text(seconds: int, realtime: bool, text: str) -> None:
    """Board texts are recomputed locally like imhd.sk shows them."""
    when = NOW.astimezone(dt_util.get_time_zone("Europe/Bratislava")) + timedelta(seconds=seconds)
    assert countdown_text(when, NOW, realtime=realtime) == text


def test_minutes_until() -> None:
    """Minutes are rounded down and never negative."""
    assert minutes_until(NOW + timedelta(seconds=299), NOW) == 4
    assert minutes_until(NOW + timedelta(seconds=59), NOW) == 0
    assert minutes_until(NOW - timedelta(seconds=20), NOW) == 0


def test_parse_info_texts() -> None:
    """Empty texts are dropped; language variants stay in one message."""
    texts = ["", "Linka 3: výluka.            Line 3: closure.   ", "", "Linka 3: výluka."]
    assert parse_info_texts(texts) == ["Linka 3: výluka.\nLine 3: closure.", "Linka 3: výluka."]
    assert parse_info_texts(None) == []
    real = parse_info_texts(load_json("itext_ba.json"))
    assert len(real) == 1
    assert real[0].startswith("Linka 3: cez víkend")
    assert real[0].endswith("nie je obsluhovaná.")


async def test_api_get_stop(hass: HomeAssistant, mock_http: AiohttpClientMocker) -> None:
    """The stop description is fetched from the board page."""
    api = ImhdApi(async_get_clientsession(hass))
    stop = await api.async_get_stop("ba", 83)
    assert stop.name == "Hodžovo nám."
    with pytest.raises(ImhdStopNotFoundError):
        await api.async_get_stop("ba", 9999999)


async def test_api_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """HTTP errors map to the client exceptions."""
    api = ImhdApi(async_get_clientsession(hass))
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=1", status=404)
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=2", status=500)
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=3", exc=aiohttp.ClientError("boom"))
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=4", exc=TimeoutError())
    aioclient_mock.get(NEAREST_URL, text="not json")
    with pytest.raises(ImhdStopNotFoundError):
        await api.async_get_stop("ba", 1)
    for stop_id in (2, 3, 4):
        with pytest.raises(ImhdConnectionError):
            await api.async_get_stop("ba", stop_id)
    with pytest.raises(ImhdConnectionError):
        await api.async_nearest_stops("ba", 48.1, 17.1)


async def test_api_lookup(hass: HomeAssistant, mock_http: AiohttpClientMocker) -> None:
    """Nearest stops, search and exact name lookup."""
    api = ImhdApi(async_get_clientsession(hass))
    nearest = await api.async_nearest_stops("ba", 48.1486, 17.1077)
    assert nearest[0].stop_id == 83
    _, url, _, headers = mock_http.mock_calls[-1]
    assert url.query["op"] == "GetNearestStop"
    assert url.query["lat"] == "48.148600"
    assert "HomeAssistant" in headers["User-Agent"]

    assert [stop.stop_id for stop in await api.async_search_stops("ba", "hodzovo")] == [83]
    assert (await api.async_find_stop_by_name("ba", "HODZOVO NAM.")).stop_id == 83
    with pytest.raises(ImhdStopNotFoundError):
        await api.async_find_stop_by_name("ba", "Hodžovo")


def test_departure_dict_keys() -> None:
    """The public departure dict has exactly the documented keys."""
    deps = parse_tabs(sample_tabs(), LABELS)
    assert list(deps[0].as_dict()) == [
        "line",
        "destination",
        "destination_city",
        "departure",
        "scheduled",
        "time",
        "scheduled_time",
        "minutes",
        "leave_in",
        "delay",
        "realtime",
        "platform",
        "vehicle",
        "low_floor",
        "air_conditioning",
        "stuck",
        "text",
        "trip_id",
        "platform_id",
        "terminal",
        "previous_stop",
        "stops_away",
        "vehicle_type",
    ]
    assert isinstance(deps[0].departure, datetime)
