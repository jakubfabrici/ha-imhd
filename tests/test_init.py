"""Tests for setup, unload, rejection handling and YAML import."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.const import (
    CONF_DIRECTION,
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_PLATFORMS,
    CONF_STOP_NAME,
    CONF_WALKING_TIME,
    DEFAULT_OPTIONS,
    DOMAIN,
)

from .conftest import STOP_PAGE_URL, FakeFeed, entry_data, setup_entry


async def test_setup_and_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """The entry sets up a feed and entities and tears them down again."""
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    feed = fake_feed.instances[0]
    assert (feed.section, feed.stop_id) == ("ba", 83)
    assert hass.states.get("sensor.hodzovo_departures").state == "1"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert feed.stopped.is_set()
    assert hass.states.get("sensor.hodzovo_departures").state == "unavailable"


async def test_setup_uses_cached_stop_when_offline(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """When imhd.sk HTTP is down the cached stop description is used."""
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=83", exc=TimeoutError())
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.stop.name == "Hodžovo nám."


async def test_setup_retry_without_cache(
    hass: HomeAssistant, fake_feed: type[FakeFeed], aioclient_mock: AiohttpClientMocker
) -> None:
    """Without cached stop info a failing fetch retries later."""
    aioclient_mock.get(f"{STOP_PAGE_URL}?st=83", exc=TimeoutError())
    data = entry_data()
    data.pop(CONF_STOP_NAME)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="ba_83_x", data=data, options={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_rejected(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """A refused connection at setup -> loaded but unavailable + repairs issue."""
    fake_feed.reject = "-12 too many connections from this IP address"
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.hodzovo_departures").state == "unavailable"
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"subscription_rejected_{config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["reason"] == (
        "-12 too many connections from this IP address"
    )

    fake_feed.reject = None
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()
    assert len(fake_feed.instances) == 2
    assert hass.states.get("sensor.hodzovo_departures").state == "1"
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"subscription_rejected_{config_entry.entry_id}")
        is None
    )


async def test_runtime_rejection_and_refresh(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """A rejection while running marks entities unavailable until refresh."""
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    issues = ir.async_get(hass)
    issue_id = f"subscription_rejected_{config_entry.entry_id}"

    feed.listener.feed_rejected("[-1]")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hodzovo_departures").state == "unavailable"
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    assert feed.reconnect_requests == 1
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    feed.listener.feed_tabs(fake_feed.initial)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hodzovo_departures").state == "1"


async def test_options_update_reloads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Changing options reloads the entry (new feed, new filters)."""
    await setup_entry(hass, config_entry)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_LINES: ["9"]}
    )
    await hass.async_block_till_done()
    assert len(fake_feed.instances) == 2
    assert hass.states.get("sensor.hodzovo_departures").state == "4"


YAML_STOPS: list[dict[str, Any]] = [
    {
        "name": "Hodzovo",
        "city": "Bratislava",
        "stop": 83,
        "lines": "9, X13",
        "direction": "Petržalka",
        "walking_time": 3,
    },
    {"name": "Center", "city": "ba", "stop": "https://imhd.sk/ba/online-zastavkova-tabula?st=83"},
    {"city": "BA", "stop": "Hodžovo nám.", "platforms": ["A"], "max_departures": 5},
]


async def test_yaml_import(
    hass: HomeAssistant, fake_feed: type[FakeFeed], mock_http: AiohttpClientMocker
) -> None:
    """Every YAML item becomes a config entry (id, URL and exact name)."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: YAML_STOPS})
    await hass.async_block_till_done()

    entries = {e.unique_id: e for e in hass.config_entries.async_entries(DOMAIN)}
    assert set(entries) == {"ba_83_hodzovo", "ba_83_center", "ba_83_hodzovo_nam"}
    first = entries["ba_83_hodzovo"]
    assert first.source == SOURCE_IMPORT
    assert first.title == "Hodzovo"
    assert first.options[CONF_LINES] == ["9", "X13"]
    assert first.options[CONF_DIRECTION] == ["Petržalka"]
    assert first.options[CONF_WALKING_TIME] == 3
    third = entries["ba_83_hodzovo_nam"]
    assert third.title == "Hodžovo nám."
    assert third.options[CONF_PLATFORMS] == ["A"]
    assert third.options[CONF_MAX_DEPARTURES] == 5
    assert all(e.state is ConfigEntryState.LOADED for e in entries.values())
    assert hass.states.get("sensor.hodzovo_departures") is not None


async def test_yaml_import_updates_existing(
    hass: HomeAssistant,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Importing an existing stop updates its options instead of duplicating it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IMPORT,
        title="Hodzovo",
        unique_id="ba_83_hodzovo",
        data=entry_data(),
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={"city": "ba", "stop": 83, CONF_NAME: "Hodzovo", CONF_LINES: ["9"]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.options[CONF_LINES] == ["9"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    # Known stop id + name: resolved without asking imhd.sk.
    assert mock_http.call_count == 0

    # Without a name the stop is looked up (default name = stop name).
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data={"city": "ba", "stop": "83"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "ba_83_hodzovo_nam"
    assert mock_http.call_count >= 1


@pytest.mark.parametrize(
    ("stop", "reason"),
    [("Nowhere street", "stop_not_found"), (9999999, "stop_not_found")],
)
async def test_yaml_import_errors(
    hass: HomeAssistant,
    mock_http: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    stop: str | int,
    reason: str,
) -> None:
    """Unresolvable stops abort the import with a logged reason."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data={"city": "ba", "stop": stop}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert "Cannot import imhd YAML stop" in caplog.text
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_yaml_invalid_city(hass: HomeAssistant) -> None:
    """An unknown city fails YAML validation."""
    assert not await async_setup_component(
        hass, DOMAIN, {DOMAIN: [{"city": "Atlantis", "stop": 1}]}
    )


async def test_yaml_removed_entry_creates_issue(
    hass: HomeAssistant, fake_feed: type[FakeFeed], mock_http: AiohttpClientMocker
) -> None:
    """Imported entries no longer in YAML get a repairs issue."""
    old = MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IMPORT,
        title="Old stop",
        unique_id="ba_83_old_stop",
        data=entry_data("Old stop"),
        options=dict(DEFAULT_OPTIONS),
    )
    old.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [YAML_STOPS[0]]})
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, f"yaml_entry_removed_{old.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"name": "Old stop"}

    assert await hass.config_entries.async_remove(old.entry_id)
    assert issues.async_get_issue(DOMAIN, f"yaml_entry_removed_{old.entry_id}") is None
