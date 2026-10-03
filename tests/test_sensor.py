"""Tests for the entities, countdown tick and availability."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.const import (
    CONF_DEPARTURE_SENSORS,
    CONF_LEAVE_WINDOW,
    CONF_MAX_DEPARTURES,
    CONF_WALKING_TIME,
    DOMAIN,
    DOMAIN as IMHD_DOMAIN,
)
from custom_components.imhd.coordinator import INFO_GRACE
from custom_components.imhd.diagnostics import async_get_config_entry_diagnostics

from .conftest import LABELS, FakeFeed, load_json, row, sample_tabs, setup_entry, tabs

MAIN = "sensor.hodzovo_departures"


async def test_entities_and_attributes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """All entities exist with the documented states and attributes."""
    await setup_entry(hass, config_entry)

    main = hass.states.get(MAIN)
    assert main.state == "1"
    attrs = main.attributes
    assert attrs[ATTR_UNIT_OF_MEASUREMENT] == "min"
    assert attrs["friendly_name"] == "Hodzovo Departures"
    assert attrs["attribution"] == "Data: imhd.sk"
    assert attrs["stop_name"] == "Hodžovo nám."
    assert attrs["stop_id"] == 83
    assert attrs["city"] == "Bratislava"
    assert attrs["section"] == "ba"
    assert attrs["platforms"] == ["A", "B", "C", "D"]
    assert attrs["next_line"] == "4"
    assert attrs["next_destination"] == "Dúbravka"
    assert attrs["next_time"] == "21:01"
    assert attrs["next_departure"] == "2026-10-03T21:01:00+02:00"
    assert attrs["next_minutes"] == 1
    assert attrs["next_delay"] == 0
    assert attrs["next_realtime"] is True
    assert attrs["next_platform"] == "B"
    assert attrs["lines"] == ["4", "9", "N33", "X13"]
    assert attrs["departure_count"] == 4
    assert attrs["info"] == []
    assert attrs["connected"] is True
    assert attrs["last_update"] == "2026-10-03T21:00:00+02:00"
    assert [d["line"] for d in attrs["departures"]] == ["4", "9", "X13", "N33"]
    nine = attrs["departures"][1]
    assert nine == {
        "line": "9",
        "destination": "Karlova Ves",
        "destination_city": "Bratislava",
        "departure": "2026-10-03T21:04:00+02:00",
        "scheduled": "2026-10-03T21:02:00+02:00",
        "time": "21:04",
        "scheduled_time": "21:02",
        "minutes": 4,
        "leave_in": 4,
        "delay": 2,
        "realtime": True,
        "platform": "A",
        "vehicle": "4401",
        "low_floor": None,
        "air_conditioning": None,
        "stuck": False,
        "text": "4 min",
        "trip_id": 1,
        "platform_id": "213",
        "terminal": "Karlova Ves",
        "previous_stop": None,
        "stops_away": None,
        "vehicle_type": None,
    }

    assert hass.states.get("sensor.hodzovo_next_departure").state == "2026-10-03T19:01:00+00:00"
    next_line = hass.states.get("sensor.hodzovo_next_line")
    assert next_line.state == "4"
    assert next_line.attributes["destination"] == "Dúbravka"
    assert hass.states.get("sensor.hodzovo_delay").state == "0"
    assert hass.states.get("sensor.hodzovo_departure_1").state == "1"
    second = hass.states.get("sensor.hodzovo_departure_2")
    assert second.state == "4"
    assert second.attributes["friendly_name"] == "Hodzovo Departure 2"
    assert second.attributes["line"] == "9"
    assert hass.states.get("sensor.hodzovo_departure_3").state == "12"
    assert hass.states.get("sensor.hodzovo_departure_4") is None

    leave = hass.states.get("binary_sensor.hodzovo_time_to_leave")
    assert leave.state == STATE_ON
    assert leave.attributes["line"] == "4"
    assert leave.attributes["leave_in"] == 1
    connected = hass.states.get("binary_sensor.hodzovo_realtime_connected")
    assert connected.state == STATE_ON
    assert connected.attributes["reconnects"] == 0
    assert connected.attributes["friendly_name"] == "Hodzovo Realtime connection"
    # Unknown until imhd.sk sends info texts or INFO_GRACE confirms there are none.
    assert hass.states.get("binary_sensor.hodzovo_disruption").state == STATE_UNKNOWN

    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    assert device.identifiers == {(DOMAIN, config_entry.entry_id)}
    assert device.name == "Hodzovo"
    assert device.manufacturer == "imhd.sk"
    assert device.model == "Bratislava"
    assert device.configuration_url == "https://imhd.sk/ba/online-zastavkova-tabula?st=83"
    entity = er.async_get(hass).async_get(MAIN)
    assert entity.unique_id == "ba_83_hodzovo_departures"
    connected_entry = er.async_get(hass).async_get("binary_sensor.hodzovo_realtime_connected")
    assert connected_entry.entity_category is er.EntityCategory.DIAGNOSTIC

    for seconds in (INFO_GRACE, 2):  # the grace period, then the coalesced publish
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    alert = hass.states.get("binary_sensor.hodzovo_disruption")
    assert (alert.state, alert.attributes["messages"]) == (STATE_OFF, [])


async def test_entity_ids_are_language_independent(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Entity ids are <domain>.<slug(name)>_<key> whatever the language."""
    await hass.config.async_update(language="sk")
    await setup_entry(hass, config_entry)
    ids = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    }
    assert ids == {
        "sensor.hodzovo_departures",
        "sensor.hodzovo_next_departure",
        "sensor.hodzovo_next_line",
        "sensor.hodzovo_delay",
        "sensor.hodzovo_departure_1",
        "sensor.hodzovo_departure_2",
        "sensor.hodzovo_departure_3",
        "binary_sensor.hodzovo_time_to_leave",
        "binary_sensor.hodzovo_realtime_connected",
        "binary_sensor.hodzovo_disruption",
    }
    assert hass.states.get("sensor.hodzovo_departures").attributes["friendly_name"] == (
        "Hodzovo Odchody"
    )


async def test_vehicle_details(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """vInfo updates (coalesced) fill low floor / air conditioning."""
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_vehicle({"issi": "1:4403", "lf": 1, "ac": 0, "type": "Škoda 30T"})
    listener.feed_vehicle({"issi": "1:4401", "lf": 0, "ac": 1})
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    first = hass.states.get("sensor.hodzovo_departure_1").attributes
    assert (first["vehicle"], first["low_floor"], first["air_conditioning"]) == (
        "4403",
        True,
        False,
    )
    second = hass.states.get("sensor.hodzovo_departure_2").attributes
    assert (second["low_floor"], second["air_conditioning"]) == (False, True)


async def test_quiet_stop_is_an_empty_board(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A connected stop that sends no `tabs` shows no departures, not unavailable."""
    fake_feed.initial = None
    with patch("custom_components.imhd.FIRST_DATA_TIMEOUT", 0):
        await setup_entry(hass, config_entry)
    assert hass.states.get(MAIN).state == "unavailable"
    freezer.tick(timedelta(seconds=7))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).state == STATE_UNKNOWN
    assert hass.states.get(MAIN).attributes["departure_count"] == 0


async def test_partial_updates_are_merged(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """`tabs` events only carry changed platforms; the others are kept."""
    sequence = load_json("tabs_ba_sequence.json")
    freezer.move_to(dt_util.utc_from_timestamp(sequence[0]["received_ms"] / 1000))
    fake_feed.initial = sequence[0]["payload"]
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_MAX_DEPARTURES: 30}
    )
    await setup_entry(hass, config_entry)
    coordinator = config_entry.runtime_data

    def trips(platform: str) -> set[int]:
        return {d.trip_id for d in coordinator.build().matching if d.platform_id == platform}

    assert all(trips(p) for p in LABELS)
    for event in sequence[1:]:
        freezer.move_to(dt_util.utc_from_timestamp(event["received_ms"] / 1000))
        untouched = {p: trips(p) for p in LABELS}
        coordinator.feed_tabs(event["payload"])
        for element in event["payload"]:
            platform = str(element["nastupiste"])
            untouched.pop(platform)
            assert trips(platform) == {row["i"] for row in element["tab"]} & trips(platform)
            assert trips(platform)
        for platform, before in untouched.items():
            assert trips(platform) == before
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).attributes["departure_count"] == 30


async def test_countdown_tick(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Countdowns are recomputed every 30 s and departed rows dropped."""
    await setup_entry(hass, config_entry)
    assert hass.states.get(MAIN).state == "1"

    freezer.tick(timedelta(seconds=80))  # 21:01:20, line 4 left 20 s ago
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(MAIN)
    assert state.state == "0"
    assert state.attributes["next_line"] == "4"

    freezer.tick(timedelta(seconds=40))  # 21:02:00, left 60 s ago -> dropped
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(MAIN)
    assert state.state == "2"
    assert state.attributes["next_line"] == "9"
    assert state.attributes["departure_count"] == 3


@pytest.mark.parametrize(
    ("now", "minutes", "state", "next_departure"),
    [
        # 02:50 CEST: the N33 at 02:05:30 CET is 15.5 minutes away, not departed.
        ("2026-10-25T00:50:00+00:00", 15.5, "15", "2026-10-25T01:05:00+00:00"),
        # 02:10 CET (second pass): the 44 at 02:20:30 CET is shown 02:20 CET.
        ("2026-10-25T01:10:00+00:00", 10.5, "10", "2026-10-25T01:20:00+00:00"),
    ],
)
async def test_departures_in_the_repeated_hour(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    *,
    now: str,
    minutes: float,
    state: str,
    next_departure: str,
) -> None:
    """At the end of summer time 02:00-03:00 comes twice: entities use real time."""
    utc_now = datetime.fromisoformat(now)
    freezer.move_to(utc_now)
    fake_feed.initial = [tabs(213, [row("44", minutes, "Koliba", now=utc_now, delay=0)])]
    await setup_entry(hass, config_entry)
    assert hass.states.get(MAIN).state == state
    assert hass.states.get("sensor.hodzovo_next_departure").state == next_departure
    shown = dt_util.as_local(datetime.fromisoformat(next_departure)).isoformat()
    assert hass.states.get(MAIN).attributes["next_departure"] == shown


async def test_empty_board_and_disruption(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No departures -> unknown (still available); info texts -> service alert."""
    fake_feed.initial = [{"zastavka": 83, "nastupiste": 213, "tab": []}]
    await setup_entry(hass, config_entry)
    state = hass.states.get(MAIN)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["departures"] == []
    assert state.attributes["next_line"] is None
    assert hass.states.get("sensor.hodzovo_next_departure").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.hodzovo_time_to_leave").state == STATE_OFF

    feed = fake_feed.instances[0]
    feed.listener.feed_info(["", "Linka 9: výluka.               Line 9: closure.", ""])
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    alert = hass.states.get("binary_sensor.hodzovo_disruption")
    assert alert.state == STATE_ON
    assert alert.attributes["messages"] == ["Linka 9: výluka.\nLine 9: closure."]
    assert hass.states.get(MAIN).attributes["info"] == alert.attributes["messages"]


async def test_availability_on_disconnect(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Short blips keep entities available; long outages do not."""
    await setup_entry(hass, config_entry)
    fake_feed.instances[0].disconnect()
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).state == "1"
    assert hass.states.get("binary_sensor.hodzovo_realtime_connected").state == STATE_OFF

    freezer.tick(timedelta(seconds=310))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).state == "unavailable"
    assert hass.states.get("binary_sensor.hodzovo_realtime_connected").state == STATE_OFF


async def test_walking_time_and_sensor_count(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Uncatchable departures are hidden; departure_sensors controls row sensors."""
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            CONF_WALKING_TIME: 3,
            CONF_LEAVE_WINDOW: 1,
            CONF_DEPARTURE_SENSORS: 1,
        },
    )
    await setup_entry(hass, config_entry)
    state = hass.states.get(MAIN)
    assert state.state == "4"
    assert state.attributes["departures"][0]["leave_in"] == 1
    assert hass.states.get("binary_sensor.hodzovo_time_to_leave").state == STATE_ON
    assert hass.states.get("sensor.hodzovo_departure_1").state == "4"
    assert hass.states.get("sensor.hodzovo_departure_2") is None


async def test_surplus_row_sensors_removed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Lowering departure_sensors removes the now unused entities."""
    await setup_entry(hass, config_entry)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.hodzovo_departure_3") is not None
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_DEPARTURE_SENSORS: 1}
    )
    await hass.async_block_till_done()
    assert registry.async_get("sensor.hodzovo_departure_2") is None
    assert registry.async_get("sensor.hodzovo_departure_3") is None
    assert registry.async_get("sensor.hodzovo_departure_1") is not None


async def test_identical_payloads_do_not_churn(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """10 identical `tabs` 3 s apart cause at most one state change (finding 3)."""
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_info([])  # publishes once (the service alert becomes known)
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    entity_ids = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    }
    changes: list[str] = []

    @callback
    def _record(event: Event) -> None:
        if event.data["entity_id"] in entity_ids:
            changes.append(event.data["entity_id"])

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    for _ in range(10):
        freezer.tick(timedelta(seconds=3))
        listener.feed_tabs(sample_tabs())
        await hass.async_block_till_done()
    assert len(changes) <= 1
    attrs = hass.states.get("binary_sensor.hodzovo_realtime_connected").attributes
    assert "last_message" not in attrs
    assert attrs["reconnects"] == 0


async def test_second_level_prediction_jitter_does_not_churn(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Predictions moving by seconds within the same minute publish nothing."""
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    changes: list[str] = []

    @callback
    def _record(event: Event) -> None:
        if event.data["entity_id"].startswith(("sensor.hodzovo", "binary_sensor.hodzovo")):
            changes.append(event.data["entity_id"])

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    for shift_ms in (5000, 10000, -5000):
        payload = sample_tabs()
        for platform in payload:
            for item in platform["tab"]:
                item["cas"] += shift_ms
        listener.feed_tabs(payload)
        await hass.async_block_till_done()
    assert changes == []


async def test_last_update_tracks_content_changes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """`last_update` moves only when the departures change."""
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    first = hass.states.get(MAIN).attributes["last_update"]
    freezer.tick(timedelta(seconds=5))
    listener.feed_tabs(sample_tabs())
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).attributes["last_update"] == first
    changed = sample_tabs()
    changed[0]["tab"][0]["casDelta"] = 3
    listener.feed_tabs(changed)
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).attributes["last_update"] == "2026-10-03T21:00:05+02:00"


async def test_malformed_payload_does_not_freeze_stop(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A broken platform / row is ignored; the stop keeps updating (finding 4)."""
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_tabs(
        [
            {"zastavka": 83, "nastupiste": 215, "tab": 5},
            {"zastavka": 83, "nastupiste": 216, "tab": [{"linka": "7", "cas": 1e30}, "junk"]},
            {"zastavka": 83, "nastupiste": 217, "timestamp": float("inf"), "tab": []},
        ]
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).state == "0"
    response = await hass.services.async_call(
        IMHD_DOMAIN,
        "get_departures",
        {"entity_id": MAIN},
        blocking=True,
        return_response=True,
    )
    assert [d["line"] for d in response["departures"]] == ["4", "9", "X13", "N33"]
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["departures"]["total_after_filters"] == 4


async def test_departure_sensors_beyond_max_departures(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """departure_N reads the full filtered list, not the truncated one (finding 7)."""
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, CONF_MAX_DEPARTURES: 2, CONF_DEPARTURE_SENSORS: 4},
    )
    await setup_entry(hass, config_entry)
    assert hass.states.get(MAIN).attributes["departure_count"] == 2
    assert hass.states.get("sensor.hodzovo_departure_3").state == "12"
    assert hass.states.get("sensor.hodzovo_departure_4").state == "25"
