"""Tests for the HTTP client and payload parsers."""

from __future__ import annotations

from datetime import datetime, timedelta
import json

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
from custom_components.imhd.models import (
    countdown_text,
    expected_clock_time,
    expected_departure,
    minutes_until,
)

from .conftest import (
    LABELS,
    NEAREST_URL,
    NOW,
    ON_THE_WAY,
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
    assert {stop.section for stop in stops} == {"ba"}


@pytest.mark.parametrize(
    ("city", "section", "url"),
    [
        ("Bratislava", "ba", "https://imhd.sk/ba/online-zastavkova-tabula?st=83"),
        ("Malacky", "ke", "https://imhd.sk/ke/online-zastavkova-tabula?st=83"),
        (None, "ke", "https://imhd.sk/ke/online-zastavkova-tabula?st=83"),
    ],
)
def test_parse_nearest_stop_city_section(city: str | None, section: str, url: str) -> None:
    """Stops of another city get that city's section (else the requested one)."""
    payload = load_json("nearest_ba.json")
    for stop in payload["stops"]:
        stop["city"] = city
    stop = parse_nearest(payload, "ke", 48.1486, 17.1077)[0]
    assert (stop.stop_id, stop.section, stop.board_url) == (83, section, url)


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
        ("Slovensko a svet", "transport"),
        ("Slovakia & world", "transport"),
        ("slovakia and world", "transport"),
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
    # Sorted by the countdown, then the minute shown (not by the prediction to the second).
    assert [dep.minutes for dep in deps] == sorted(dep.minutes for dep in deps)
    assert [dep.expected for dep in deps] == sorted(dep.expected for dep in deps)
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


async def test_parse_tabs_keeps_vehicles_on_their_way(hass: HomeAssistant) -> None:
    """Trips listed with their vehicle on its way (at the stop) stay up to 90 s past."""
    payload = [
        tabs(
            213,
            [
                row("1", -85 / 60, "At the stop", delay=0, trip=1, odjazd="*", **ON_THE_WAY),
                row("2", -95 / 60, "Frozen platform", delay=0, trip=2, odjazd="*", **ON_THE_WAY),
                # Vehicle assigned, the trip starts here: listed `*` for 31 min.
                row("3", -40 / 60, "Not on its way", delay=0, trip=3, odjazd="*", tuZidx=0),
                row("4", -40 / 60, "Timetable only", trip=4, odjazd="*", **ON_THE_WAY),
            ],
        ),
        tabs(214, [row("5", -40 / 60, "Other platform", delay=0, trip=1, **ON_THE_WAY)]),
    ]
    deps = parse_tabs(payload, LABELS, NOW, stop_id=83)
    assert [(dep.line, dep.minutes, dep.leave_in, dep.text) for dep in deps] == [
        ("1", 0, 0, "*"),
        ("5", 0, 0, "*"),
    ]
    # Nothing confirms that the vehicles are still there (disconnected).
    assert parse_tabs(payload, LABELS, NOW, stop_id=83, vehicle_grace=False) == []


SAME_MINUTE = [("9", -3), ("9", 2), ("44", 1)]


@pytest.mark.parametrize(
    ("first", "second", "order"),
    [
        (10, 12, SAME_MINUTE),
        (13, 11, SAME_MINUTE),
        # The 44 and the Dúbravka 9 leave in 2 minutes, the Karlova Ves 9 in 3.
        (-14, 44, [("9", -3), ("44", 1), ("9", 2)]),
    ],
)
async def test_parse_tabs_same_minute_order(
    hass: HomeAssistant, first: int, second: int, order: list[tuple[str, int]]
) -> None:
    """Sorted by countdown, then shown minute; ties keep their order when predictions jitter."""
    payload = [
        tabs(213, [row("44", 3 + first / 60, "Koliba", delay=1, trip=1)]),
        tabs(214, [row("9", 3 + second / 60, "Karlova Ves", delay=0, trip=2)]),
        tabs(215, [row("9", 3 + first / 60, "Dúbravka", delay=0, trip=-3)]),
    ]
    deps = parse_tabs(payload, LABELS, NOW, stop_id=83)
    assert [(dep.line, dep.trip_id) for dep in deps] == order
    assert {dep.as_dict()["time"] for dep in deps} == {"21:03"}
    assert [dep.minutes for dep in deps] == sorted(dep.minutes for dep in deps)


@pytest.mark.parametrize(
    ("now", "offsets", "expected"),
    [
        # End of summer time, 02:55 CEST: 02:05, 02:35 and 03:05 CET are 10-70 min away.
        (
            "2026-10-25T00:55:00+00:00",
            [600, 2400, 4200],
            [
                (10, "10 min", "2026-10-25T02:05:00+01:00"),
                (40, "40 min", "2026-10-25T02:35:00+01:00"),
                (70, "03:05", "2026-10-25T03:05:00+01:00"),
            ],
        ),
        # 02:00:10 CET (second pass): 02:59:30 CEST left 40 s ago, 02:59:50 CEST 20 s ago.
        (
            "2026-10-25T01:00:10+00:00",
            [-40, -20, 600],
            [(0, "*", "2026-10-25T02:59:50+02:00"), (10, "10 min", "2026-10-25T02:10:10+01:00")],
        ),
        # Start of summer time, 01:50 CET: 03:05 CEST is 15 min away.
        ("2026-03-29T00:50:00+00:00", [900], [(15, "15 min", "2026-03-29T03:05:00+02:00")]),
        # 03:00:10 CEST: 01:59:50 CET left 20 s ago (kept), 01:59:30 CET 40 s ago.
        ("2026-03-29T01:00:10+00:00", [-40, -20], [(0, "*", "2026-03-29T01:59:50+01:00")]),
    ],
)
async def test_parse_tabs_across_dst_changes(
    hass: HomeAssistant, now: str, offsets: list[int], expected: list[tuple[int, str, str]]
) -> None:
    """Countdowns and departed rows use real time, not the local wall clock."""
    utc_now = datetime.fromisoformat(now)
    payload = [
        tabs(213, [row("9", offset / 60, "X", now=utc_now, delay=0, trip=i)])
        for i, offset in enumerate(offsets)
    ]
    deps = parse_tabs(payload, LABELS, dt_util.as_local(utc_now), stop_id=83)
    assert [(dep.minutes, dep.text, dep.as_dict()["departure"]) for dep in deps] == expected


@pytest.mark.parametrize(
    ("now", "kept"),
    [
        # 02:00:10 CET (second pass): 02:58:30 CEST left 100 s ago, 02:59:30 CEST 40 s ago.
        ("2026-10-25T01:00:10+00:00", "2026-10-25T02:59:30+02:00"),
        # 03:00:10 CEST: 01:58:30 CET left 100 s ago, 01:59:30 CET 40 s ago.
        ("2026-03-29T01:00:10+00:00", "2026-03-29T01:59:30+01:00"),
    ],
)
async def test_parse_tabs_vehicles_on_their_way_across_dst_changes(
    hass: HomeAssistant, now: str, kept: str
) -> None:
    """The longer grace of vehicles at the stop runs in real time too."""
    utc_now = datetime.fromisoformat(now)
    payload = [
        tabs(
            213,
            [
                row("9", -100 / 60, "X", now=utc_now, delay=0, trip=1, **ON_THE_WAY),
                row("9", -40 / 60, "X", now=utc_now, delay=0, trip=2, **ON_THE_WAY),
            ],
        )
    ]
    deps = parse_tabs(payload, LABELS, dt_util.as_local(utc_now), stop_id=83)
    assert [dep.as_dict()["departure"] for dep in deps] == [kept]


@pytest.mark.parametrize(
    ("cas", "shown"),
    [
        ("2026-10-25T01:20:30+00:00", "2026-10-25T02:20:00+01:00"),  # 02:20:30 CET
        ("2026-10-25T00:59:50+00:00", "2026-10-25T02:00:00+01:00"),  # 02:59:50 CEST
        ("2026-10-25T00:20:30+00:00", "2026-10-25T02:20:00+02:00"),  # 02:20:30 CEST
    ],
)
def test_expected_departure_in_the_repeated_hour(hass: HomeAssistant, cas: str, shown: str) -> None:
    """The shown minute keeps the UTC offset: 02:xx comes twice at the end of summer time."""
    when = dt_util.as_local(datetime.fromisoformat(cas))
    assert expected_departure(when).isoformat() == shown
    assert expected_clock_time(when) == shown[11:16]


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
        (3700, False, "~22:01"),
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
        "source",
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


def _token(value: dict) -> str:
    """Encode a stop page token (UTF-8 JSON bytes + 0x4F, hex)."""
    return bytes((b + 0x4F) & 0xFF for b in json.dumps(value).encode()).hex()


@pytest.mark.parametrize(
    "text",
    [
        "²",
        "١٢",
        "https://imhd.sk/ba/online-zastavkova-tabula?st=²",
        f"https://imhd.sk/ba/zastavka/x/{_token({'g': '²'})}",
    ],
)
def test_parse_stop_input_non_ascii_digits(text: str) -> None:
    """Unicode digits are not stop ids (finding 4/8: no ValueError)."""
    with pytest.raises(ImhdInvalidStopError):
        parse_stop_input(text)


def test_parse_stop_page_non_ascii_stop_id() -> None:
    """A weird stopId means "not found", not a crash."""
    html = '<script>$.extend( options, {"stopId": "²", "stopName": "X"} );</script>'
    with pytest.raises(ImhdStopNotFoundError):
        parse_stop_page(html, "ba")


async def test_parse_tabs_skips_malformed_rows(hass: HomeAssistant) -> None:
    """Bad rows are skipped; good rows of the same payload survive (finding 4)."""
    good = row("9", 4, "Karlova Ves", delay=1, trip=1)
    payload = [
        tabs(
            213,
            [
                good,
                row("1", 2, "Huge", trip=2, cas=1e30),
                row("2", 2, "Inf", trip=3, cas=float("inf"), casCP=float("nan")),
                row("3", 2, "Inf index", delay=0, trip=4, tuZidx=float("inf"), predoslaZidx=1),
                row("²", 3, "Unicode line", trip=5),
                {**row("5", 3, "Bad text", trip=6), "odjazd": {"x": 1}, "konecnaZstr": 7},
                "junk",
            ],
        ),
        {"zastavka": 83, "nastupiste": 214, "tab": 5},
        {"zastavka": 83, "nastupiste": 215, "tab": ["junk", None, 3]},
    ]
    vehicles = {"1:4401": {"issi": "1:4401", "lf": "²", "ac": "1", "type": ["x"]}}
    deps = parse_tabs(payload, LABELS, NOW, stop_id=83, vehicles=vehicles)
    lines = [dep.line for dep in deps]
    assert "9" in lines
    assert "1" not in lines
    assert "2" not in lines
    nine = next(dep for dep in deps if dep.line == "9")
    assert nine.air_conditioning is True
    assert nine.low_floor is None
    for dep in deps:
        dep.as_dict()  # serialisable, no exception


def test_parse_lookups_tolerate_odd_json() -> None:
    """Unexpected JSON shapes give no stops instead of exceptions (finding 8)."""
    for payload in ([], [1, 2], "x", None, {"results": "x"}, {"stops": {"a": 1}}):
        assert parse_search(payload, "ba") == []
        assert parse_nearest(payload, "ba", 48.1, 17.1) == []
    search = {
        "results": [
            "junk",
            {"class": "stop", "value": "g5"},
            {"class": "stop", "value": "g6", "name": None},
            {"class": "stop", "value": "g7", "name": "Ok"},
            {"class": "stop", "value": "g²", "name": "Bad"},
        ]
    }
    assert [(s.stop_id, s.name) for s in parse_search(search, "ba")] == [(7, "Ok")]
    nearest = {
        "stops": [
            "junk",
            {"id": "x", "lat": 1, "lng": 2},
            {"id": 9, "lat": "48.1", "lng": "17.1", "name": None, "platform_labels": [1]},
        ]
    }
    [stop] = parse_nearest(nearest, "ba", 48.1, 17.1)
    assert (stop.stop_id, stop.name, stop.platform_labels) == (9, "9", {})


@pytest.mark.parametrize(
    ("cas", "cas_cp", "time", "scheduled"),
    [
        ("21:32:45", "21:33:00", "21:33", "21:33"),  # imhd predicts 15 s early
        ("21:32:40", "21:33:45", "21:32", "21:33"),  # truncated, not rounded
        ("21:33:44", "21:33:30", "21:33", "21:33"),
    ],
)
async def test_clock_times_like_imhd(
    hass: HomeAssistant, cas: str, cas_cp: str, time: str, scheduled: str
) -> None:
    """HH:MM texts follow imhd.sk / stop.js (finding 10)."""
    tz = dt_util.get_time_zone("Europe/Bratislava")
    ms = lambda text: int(  # noqa: E731
        datetime.fromisoformat(f"2026-10-03T{text}").replace(tzinfo=tz).timestamp() * 1000
    )
    raw = {**row("9", 0, "X"), "cas": ms(cas), "casCP": ms(cas_cp)}
    [dep] = parse_tabs([tabs(213, [raw])], LABELS)
    assert dep.as_dict()["time"] == time
    assert dep.as_dict()["scheduled_time"] == scheduled


def test_countdown_text_hhmm_matches_time() -> None:
    """Beyond 60 minutes the text is the same clock time as `time`."""
    tz = dt_util.get_time_zone("Europe/Bratislava")
    when = datetime.fromisoformat("2026-10-03T22:32:45").replace(tzinfo=tz)
    now = when - timedelta(minutes=61, seconds=30)
    assert countdown_text(when, now, realtime=True) == "22:33"
    assert countdown_text(when, when - timedelta(minutes=60, seconds=30), realtime=True) == (
        "60 min"
    )
