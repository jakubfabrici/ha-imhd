"""Tests for the actions and diagnostics."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.const import BASE_URL, DOMAIN
from custom_components.imhd.diagnostics import async_get_config_entry_diagnostics

from .conftest import NEAREST_URL, STOP_PAGE_URL, FakeFeed, load_json, setup_entry


async def call(hass: HomeAssistant, service: str, data: dict) -> dict:
    """Call a response-only action."""
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def test_get_departures(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """get_departures returns the stop and filtered departures."""
    await setup_entry(hass, config_entry)

    response = await call(hass, "get_departures", {"entity_id": "sensor.hodzovo_departures"})
    assert response["stop"]["id"] == 83
    assert response["stop"]["name"] == "Hodžovo nám."
    assert response["stop"]["url"] == "https://imhd.sk/ba/online-zastavkova-tabula?st=83"
    assert [d["line"] for d in response["departures"]] == ["4", "9", "X13", "N33"]

    response = await call(
        hass,
        "get_departures",
        {"config_entry_id": config_entry.entry_id, "line": ["x13", "9"], "limit": 1},
    )
    assert [d["line"] for d in response["departures"]] == ["9"]

    response = await call(
        hass,
        "get_departures",
        {"entity_id": "binary_sensor.hodzovo_time_to_leave", "direction": "petrzalka"},
    )
    assert [d["line"] for d in response["departures"]] == ["X13"]

    response = await call(
        hass, "get_departures", {"config_entry_id": config_entry.entry_id, "min_minutes": 5}
    )
    assert [d["minutes"] for d in response["departures"]] == [12, 25]


async def test_get_departures_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Unknown or unloaded targets are validation errors."""
    await setup_entry(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await call(hass, "get_departures", {})
    with pytest.raises(ServiceValidationError):
        await call(hass, "get_departures", {"config_entry_id": "nope"})
    with pytest.raises(ServiceValidationError):
        await call(hass, "get_departures", {"entity_id": "sensor.unknown"})
    await hass.config_entries.async_unload(config_entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await call(hass, "get_departures", {"config_entry_id": config_entry.entry_id})


async def test_find_stops(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """find_stops searches near home, near coordinates or by name."""
    await setup_entry(hass, config_entry)
    response = await call(hass, "find_stops", {"city": "Bratislava"})
    first = response["stops"][0]
    assert first["id"] == 83
    assert first["platforms"] == ["A", "B", "C", "D"]
    assert first["distance_m"] == 55
    assert {"id", "name", "city", "lat", "lon", "platforms", "distance_m"} <= set(first)
    _, url, _, _ = mock_http.mock_calls[-1]
    assert url.query["lat"] == "48.148600"

    await call(hass, "find_stops", {"city": "ba", "latitude": 48.2, "longitude": 17.2})
    _, url, _, _ = mock_http.mock_calls[-1]
    assert url.query["lat"] == "48.200000"

    response = await call(hass, "find_stops", {"city": "ba", "query": "hodzovo"})
    assert [stop["id"] for stop in response["stops"]] == [83]

    with pytest.raises(ServiceValidationError):
        await call(hass, "find_stops", {"city": "Atlantis"})


async def test_find_stops_nearest_other_city(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Nearby stops of another city come with their own section and board link."""
    mock_http.get(f"{BASE_URL}/ke/api/cepo", json=load_json("nearest_ba.json"))
    await setup_entry(hass, config_entry)
    response = await call(
        hass, "find_stops", {"city": "ke", "latitude": 48.149, "longitude": 17.107}
    )
    first = response["stops"][0]
    assert (first["id"], first["city"], first["section"]) == (83, "Bratislava", "ba")
    assert first["url"] == "https://imhd.sk/ba/online-zastavkova-tabula?st=83"


async def test_find_stops_offline(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """HTTP failures surface as HomeAssistantError."""
    aioclient_mock.get(NEAREST_URL, exc=TimeoutError())
    aioclient_mock.get(STOP_PAGE_URL, exc=TimeoutError())
    await setup_entry(hass, config_entry)  # setup works from the cached stop info
    with pytest.raises(HomeAssistantError):
        await call(hass, "find_stops", {"city": "ba"})


async def test_refresh_targets(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """refresh reconnects all or the targeted stops."""
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    await hass.services.async_call(
        DOMAIN, "refresh", {"entity_id": ["sensor.hodzovo_departures"]}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "refresh", {"config_entry_id": config_entry.entry_id}, blocking=True
    )
    assert feed.reconnect_requests == 2


async def test_diagnostics(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Diagnostics contain config, stop, connection and a trimmed payload."""
    await setup_entry(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["entry"]["unique_id"] == "ba_83_hodzovo"
    assert diag["stop"]["id"] == 83
    assert diag["connection"]["connected"] is True
    assert diag["connection"]["reconnects"] == 0
    assert diag["departures"]["total_after_filters"] == 4
    assert len(diag["last_raw_payload"]) == 2
    assert diag["last_raw_payload"][0]["tab_rows_total"] == 2


async def test_find_stops_odd_json(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Unexpected JSON from imhd.sk gives an empty result, not a crash (finding 8)."""
    aioclient_mock.get(NEAREST_URL, json=[1, 2, 3])
    aioclient_mock.get(STOP_PAGE_URL, exc=TimeoutError())
    await setup_entry(hass, config_entry)
    assert await call(hass, "find_stops", {"city": "ba"}) == {"stops": []}
