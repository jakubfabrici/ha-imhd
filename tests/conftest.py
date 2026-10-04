"""Shared fixtures for the imhd tests."""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterable
from datetime import date, datetime, timedelta
from http import HTTPStatus
import json
from pathlib import Path
import re
from typing import Any, ClassVar
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from yarl import URL

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.imhd.const import (
    BASE_URL,
    CONF_PLATFORM_LABELS,
    CONF_SECTION,
    CONF_STOP_CITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.fromisoformat("2026-10-03T19:00:00+00:00")  # 21:00 in Bratislava
STOP_PAGE_URL = f"{BASE_URL}/ba/online-zastavkova-tabula"
NEAREST_URL = f"{BASE_URL}/ba/api/cepo"
SEARCH_URL = f"{BASE_URL}/ba/api/sk/vyhladavanie"
TIMETABLE_URL = re.compile(r"^https://imhd\.sk/[a-z]+/vsetky-odchody-zo-zastavky/")
LABELS = {"213": "A", "214": "B", "215": "C", "216": "D"}
# Row fields of a trip whose vehicle is on its way (it passed the previous stop).
ON_THE_WAY = {"tuZidx": 12, "predoslaZidx": 11, "predoslaZstr": "Kozia"}


def load_fixture(name: str) -> str:
    """Return a fixture file as text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str) -> Any:
    """Return a JSON fixture."""
    return json.loads(load_fixture(name))


def row(
    line: str,
    minutes: float,
    destination: str,
    *,
    now: datetime = NOW,
    delay: int | None = None,
    trip: int = 1,
    issi: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one `tabs` row departing `minutes` after `now`."""
    expected = int((now + timedelta(minutes=minutes)).timestamp() * 1000)
    data: dict[str, Any] = {
        "linka": line,
        "i": trip,
        "cas": expected,
        "casCP": expected - (delay or 0) * 60000,
        "typ": "online" if delay is not None else "cp",
        "konecnaZstr": destination,
        "konecnaZobec": "Bratislava",
        "odjazd": f"{int(minutes)} min",
    }
    if delay is not None:
        data["casDelta"] = delay
        data["issi"] = issi or f"1:{4400 + trip}"
    data.update(extra)
    return data


def tabs(platform: int, rows: list[dict[str, Any]], stop: int = 83) -> dict[str, Any]:
    """Build one per-platform `tabs` element."""
    return {
        "zastavka": stop,
        "nastupiste": platform,
        "timestamp": int(NOW.timestamp() * 1000),
        "nextRowAt": None,
        "tab": rows,
    }


def sample_tabs() -> list[dict[str, Any]]:
    """Return a small synthetic `tabs` payload for stop 83."""
    return [
        tabs(
            213,
            [
                row("9", 4, "Karlova Ves", delay=2, trip=1),
                row("X13", 12, "Petržalka, Jungmannova", trip=2),
            ],
        ),
        tabs(
            214,
            [
                row("4", 1, "Dúbravka", delay=0, trip=3),
                row("N33", 25, "Hlavná stanica", trip=4),
            ],
        ),
    ]


class FakeFeed:
    """Stand-in for ImhdRealtimeFeed driven by the tests."""

    instances: ClassVar[list[FakeFeed]] = []
    initial: Any = None
    reject: str | None = None

    def __init__(self, *, section: str, stop_id: int, listener: Any, **_: Any) -> None:
        """Record the listener."""
        self.section = section
        self.stop_id = stop_id
        self.listener = listener
        self.connected = False
        self.sessions = 0
        self.rejected: str | None = None
        self.reconnect_requests = 0
        self.stopped = asyncio.Event()
        type(self).instances.append(self)

    @property
    def reconnects(self) -> int:
        """Mirror the real feed."""
        return max(0, self.sessions - 1)

    async def run(self) -> None:
        """Connect and deliver the initial payload, then idle."""
        self.sessions += 1
        if self.reject is not None:
            self.rejected = self.reject
            self.listener.feed_rejected(self.reject)
            return
        self.connected = True
        self.listener.feed_connected()
        if self.initial is not None:
            self.listener.feed_tabs(self.initial)
        await self.stopped.wait()

    async def async_stop(self) -> None:
        """Stop."""
        self.connected = False
        self.stopped.set()

    def request_reconnect(self) -> None:
        """Count reconnect requests."""
        self.reconnect_requests += 1

    def disconnect(self) -> None:
        """Simulate a lost connection."""
        self.connected = False
        self.listener.feed_disconnected()


def timetable_page(
    day: date, rows: Iterable[tuple[str, str, str, str]] = (), stop: str = "Hodžovo nám."
) -> str:
    """Render a scheduled departures page like imhd.sk's.

    Rows are (line, "H:MM", destination, platform label or "").
    """
    cells = []
    for line, clock, destination, platform in rows:
        label = (
            '<span class="float-right text-muted"><svg class="Icon Icon--opticalAlign">'
            f'<use href="/data/img/icons.svg#stop"></use></svg><small>{platform}</small></span>'
            if platform
            else ""
        )
        cells.append(
            '<tr>\n\t<td class="text-center"><svg class="TimetableHeader-icon Icon">'
            '<use href="/data/img/vehicles.svg#bus"></use></svg></td>\n'
            f'\t<td class="text-center"><span class=" Linka ba l{line}">{line}</span></td>\n'
            f'\t<td class="w-100">\n\t\t{destination} {label}\n\t</td>\n'
            f"\t<td>\n\t\t{clock}\n\t</td>\n\t<td> </td>\n</tr>"
        )
    return (
        '<html><body><section class="Module Box"><div class="ModuleHeader">'
        '<h1 class="ModuleHeader-title h2">Všetky odchody zo zastávky '
        f"{stop} {day.day}.{day.month}.{day.year}</h1></div>"
        '<table class="table table-borderless table-striped"><thead><tr>'
        '<th scope="col" colspan="2">Linka</th><th scope="col">Smer</th>'
        '<th scope="col" colspan="2">Odchod</th></tr></thead><tbody>'
        f"{''.join(cells)}</tbody></table></section></body></html>"
    )


class TimetableServer:
    """imhd.sk's scheduled departures pages (an empty page for days not set)."""

    def __init__(self) -> None:
        """Serve empty pages."""
        self.pages: dict[date, str] = {}
        self.status = HTTPStatus.OK
        self.exc: Exception | None = None
        self.headers: dict[str, str] = {}
        # (section, stop id, day) of each request, and when it was made.
        self.requests: list[tuple[str, int, date]] = []
        self.times: list[datetime] = []

    @property
    def days(self) -> list[date]:
        """Return the days requested, in order."""
        return [day for _section, _stop, day in self.requests]

    async def respond(self, method: str, url: URL, _data: Any) -> AiohttpClientMockResponse:
        """Answer a request for a page (the stop and day are in the token)."""
        token = url.path.rsplit("/", 1)[-1]
        payload = json.loads(bytes((byte - 0x4F) & 0xFF for byte in bytes.fromhex(token)))
        day = date.fromisoformat(payload["d"])
        self.requests.append((url.path.split("/")[1], int(payload["g"]), day))
        self.times.append(dt_util.utcnow())
        return AiohttpClientMockResponse(
            method,
            url,
            status=self.status,
            text=self.pages.get(day) or timetable_page(day),
            headers=self.headers,
            exc=self.exc,
        )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in every test."""


@pytest.fixture(autouse=True)
def no_timetable_fetch() -> Generator[None]:
    """Fetch no scheduled departures (test_timetable.py overrides this).

    The fetch runs as a background task, which hass.async_block_till_done
    doesn't wait for: it could publish a clock step after the one it started on.
    """
    year = timedelta(days=365).total_seconds()
    with patch("custom_components.imhd.coordinator.TIMETABLE_START_DELAY", (year, year)):
        yield


@pytest.fixture(autouse=True)
async def bratislava_time(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Run tests in Bratislava local time at a fixed instant."""
    await hass.config.async_set_time_zone("Europe/Bratislava")
    hass.config.latitude = 48.1486
    hass.config.longitude = 17.1077
    freezer.move_to(NOW)


@pytest.fixture
def fake_feed() -> Generator[type[FakeFeed]]:
    """Replace the realtime feed by FakeFeed (delivering sample_tabs)."""

    class Feed(FakeFeed):
        instances: ClassVar[list[FakeFeed]] = []
        initial = sample_tabs()
        reject = None

    with patch("custom_components.imhd.coordinator.ImhdRealtimeFeed", Feed):
        yield Feed


@pytest.fixture
def timetable_server() -> TimetableServer:
    """Return the scheduled departures pages served by `mock_http`."""
    return TimetableServer()


@pytest.fixture
def mock_http(
    aioclient_mock: AiohttpClientMocker, timetable_server: TimetableServer
) -> AiohttpClientMocker:
    """Mock the imhd.sk HTTP endpoints with recorded responses."""
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=83", text=load_fixture("stop_page_ba_83.html"))
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=9999999", text=load_fixture("stop_page_missing.html"))
    aioclient_mock.get(NEAREST_URL, json=load_json("nearest_ba.json"))
    aioclient_mock.get(SEARCH_URL, json=load_json("search_ba_hodzovo.json"))
    aioclient_mock.get(TIMETABLE_URL, side_effect=timetable_server.respond)
    return aioclient_mock


def entry_data(name: str = "Hodzovo") -> dict[str, Any]:
    """Return config entry data for stop 83."""
    return {
        CONF_SECTION: "ba",
        CONF_STOP_ID: 83,
        CONF_NAME: name,
        CONF_STOP_NAME: "Hodžovo nám.",
        CONF_STOP_CITY: "Bratislava",
        CONF_PLATFORM_LABELS: LABELS,
    }


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a config entry for Bratislava stop 83 named Hodzovo."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hodzovo",
        unique_id="ba_83_hodzovo",
        data=entry_data(),
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up an entry and wait for it."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def local(text: str) -> datetime:
    """Parse a Bratislava local time string of 2026-10-03 (HH:MM[:SS])."""
    return dt_util.as_local(
        datetime.fromisoformat(f"2026-10-03T{text}").replace(
            tzinfo=dt_util.get_time_zone("Europe/Bratislava")
        )
    )
