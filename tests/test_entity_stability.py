"""Service alerts across reconnects / reloads and state churn on busy stops."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.const import EVENT_STATE_CHANGED, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_fire_time_changed_exact,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.const import (
    BASE_URL,
    CONF_LEAVE_WINDOW,
    CONF_MAX_DEPARTURES,
    CONF_WALKING_TIME,
    DOMAIN,
)
from custom_components.imhd.coordinator import EMPTY_PLATFORM_GRACE, INFO_GRACE, PUBLISH_DELAY

from .conftest import NOW, FakeFeed, load_fixture, load_json, row, sample_tabs, setup_entry, tabs

ALERT = "Linka 3: výluka."
ITEXT = ["", "", ALERT + " " * 20, ""]
DISRUPTION = "binary_sensor.hodzovo_disruption"
MAIN = "sensor.hodzovo_departures"
NEXT = "sensor.hodzovo_next_departure"
FIRST = "sensor.hodzovo_departure_1"
LEAVE = "binary_sensor.hodzovo_time_to_leave"
PER_DEPARTURE = (
    NEXT,
    "sensor.hodzovo_next_line",
    "sensor.hodzovo_delay",
    FIRST,
    "sensor.hodzovo_departure_2",
    "sensor.hodzovo_departure_3",
)


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Move the clock in 1 s steps so timers scheduled on the way fire too."""
    for _ in range(int(seconds)):
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def record(hass: HomeAssistant, *entity_ids: str) -> dict[str, list[tuple[State, State]]]:
    """Record (old, new) states of state_changed events per entity."""
    changes: dict[str, list[tuple[State, State]]] = defaultdict(list)

    @callback
    def _record(event: Event) -> None:
        if event.data["entity_id"] in entity_ids:
            changes[event.data["entity_id"]].append(
                (event.data["old_state"], event.data["new_state"])
            )

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    return changes


def states(changes: list[tuple[State, State]]) -> list[str]:
    """Return the sequence of new states (attribute-only changes collapsed)."""
    return [new.state for old, new in changes if old is None or old.state != new.state]


def departure_lines(hass: HomeAssistant) -> list[str]:
    """Return the lines listed by the main sensor."""
    return [dep["line"] for dep in hass.states.get(MAIN).attributes["departures"]]


async def test_alert_survives_reconnect(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Departures arriving before the info texts after a reconnect: no off/on flicker."""
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    feed.listener.feed_info(ITEXT)
    await advance(hass, freezer, 2)
    assert hass.states.get(DISRUPTION).state == STATE_ON
    changes = record(hass, DISRUPTION)

    feed.disconnect()
    await advance(hass, freezer, 30)  # stays on while disconnected
    feed.listener.feed_connected()
    feed.listener.feed_tabs(fake_feed.initial)
    await advance(hass, freezer, 3)
    feed.listener.feed_info(ITEXT)
    await advance(hass, freezer, INFO_GRACE + 5)

    assert changes[DISRUPTION] == []
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_ON, [ALERT])

    feed.listener.feed_info(["", "", ""])  # imhd.sk withdraws the alert
    await advance(hass, freezer, 2)
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_OFF, [])


async def test_alert_cleared_while_disconnected(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No info texts after a reconnect: the alert ends after the grace period."""
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    feed.listener.feed_info(ITEXT)
    await advance(hass, freezer, 2)
    changes = record(hass, DISRUPTION)

    feed.disconnect()
    await advance(hass, freezer, INFO_GRACE + 5)  # the grace period starts on connect
    assert hass.states.get(DISRUPTION).state == STATE_ON
    feed.listener.feed_connected()
    feed.listener.feed_tabs(fake_feed.initial)
    await advance(hass, freezer, INFO_GRACE - 2)
    assert hass.states.get(DISRUPTION).state == STATE_ON
    await advance(hass, freezer, 4)

    assert states(changes[DISRUPTION]) == [STATE_OFF]
    assert hass.states.get(DISRUPTION).attributes["messages"] == []
    assert hass.states.get(MAIN).attributes["info"] == []


async def test_alert_survives_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An entry reload restores the alert instead of reporting off until iText arrives."""
    await setup_entry(hass, config_entry)
    fake_feed.instances[0].listener.feed_info(ITEXT)
    await advance(hass, freezer, 2)
    changes = record(hass, DISRUPTION)

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_ON, [ALERT])
    await advance(hass, freezer, 3)
    fake_feed.instances[1].listener.feed_info(ITEXT)
    await advance(hass, freezer, INFO_GRACE + 5)

    assert STATE_OFF not in states(changes[DISRUPTION])
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_ON, [ALERT])


async def test_alert_ended_during_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A restored alert that imhd.sk no longer sends ends after the grace period."""
    await setup_entry(hass, config_entry)
    fake_feed.instances[0].listener.feed_info(ITEXT)
    await advance(hass, freezer, 2)

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(DISRUPTION).state == STATE_ON
    await advance(hass, freezer, INFO_GRACE + 2)
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_OFF, [])


async def test_alert_restored_after_restart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a restart the last alert is shown until imhd.sk confirms or replaces it."""
    mock_restore_cache_with_extra_data(
        hass, [(State(DISRUPTION, STATE_ON, {"messages": [ALERT]}), {"stop_id": 83})]
    )
    await setup_entry(hass, config_entry)
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_ON, [ALERT])
    changes = record(hass, DISRUPTION)

    fake_feed.instances[0].listener.feed_info(ITEXT)
    await advance(hass, freezer, INFO_GRACE + 2)
    assert changes[DISRUPTION] == []

    fake_feed.instances[0].listener.feed_info(["Linka 4: výluka."])
    await advance(hass, freezer, 2)
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_ON, ["Linka 4: výluka."])


async def test_alert_of_another_stop_not_restored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A saved state of another stop under the same entity id is not restored."""
    mock_restore_cache_with_extra_data(
        hass, [(State(DISRUPTION, STATE_ON, {"messages": [ALERT]}), {"stop_id": 1831})]
    )
    await setup_entry(hass, config_entry)
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_UNKNOWN, [])


async def test_reconfigure_does_not_restore_old_stop_alert(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reconfigure keeps the entity ids: the old stop's alert must not show on the new one."""
    mock_http.get(
        f"{BASE_URL}/za/online-zastavkova-tabula?st=1831",
        text=load_fixture("stop_page_za_1831.html"),
    )
    await setup_entry(hass, config_entry)
    fake_feed.instances[0].listener.feed_info(ITEXT)
    await advance(hass, freezer, 2)
    assert hass.states.get(DISRUPTION).state == STATE_ON
    changes = record(hass, DISRUPTION)

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"city": "za", "method": "stop_id"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "https://imhd.sk/za/online-zastavkova-tabula?st=1831"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert fake_feed.instances[-1].stop_id == 1831

    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_UNKNOWN, [])
    await advance(hass, freezer, INFO_GRACE + 2)
    alert = hass.states.get(DISRUPTION)
    assert (alert.state, alert.attributes["messages"]) == (STATE_OFF, [])
    # No "on" for the new stop at any point (e.g. no "alert cleared" notification).
    assert STATE_ON not in [new.state for _old, new in changes[DISRUPTION]]


def busy_board(jitter: int = 0) -> list[dict[str, Any]]:
    """Return 8 departures at hh:mm:40, with each prediction moved by up to ±5 s.

    X13 and 4 are shown in the same minute; the jitter keeps changing which one
    leaves first, and so their order.
    """
    rows = [
        row(line, minutes + 40 / 60, "Koliba", delay=1, trip=trip)
        for trip, (line, minutes) in enumerate(
            [("44", 3), ("9", 4), ("X13", 6), ("4", 6), ("47", 9), ("42", 10), ("1", 12), ("3", 14)]
        )
    ]
    for index, item in enumerate(rows):
        item["cas"] += ((jitter * 7 + index * 3) % 10 - 5) * 1000
    return [tabs(213, rows[::2]), tabs(214, rows[1::2])]


async def test_prediction_jitter_on_a_busy_stop(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """40 jittery `tabs` in 2 minutes: states change only when the minutes do."""
    fake_feed.initial = busy_board()
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_info([])
    await advance(hass, freezer, 2)
    freezer.move_to("2026-10-03T19:00:15+00:00")  # first tick at 21:00:30
    first_update = hass.states.get(MAIN).attributes["last_update"]
    first_next = hass.states.get(NEXT).state
    changes = record(hass, MAIN, *PER_DEPARTURE)

    for event in range(1, 41):  # 21:00:18 .. 21:02:15, every 3 s
        freezer.tick(timedelta(seconds=3))
        listener.feed_tabs(busy_board(event))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    # No "Next departure changed to ..." in the activity log.
    assert states(changes[NEXT]) == []
    assert hass.states.get(NEXT).state == first_next == "2026-10-03T19:03:00+00:00"
    # The minutes drop at 21:00:40 and 21:01:40 (seen by the ticks at 21:01:00, 21:02:00);
    # nothing else is written, whatever the predictions do within their minute.
    assert len(changes[MAIN]) <= 2
    for entity_id in PER_DEPARTURE:
        assert len(changes[entity_id]) <= 2, entity_id
        assert len({new.attributes["departure"] for _old, new in changes[entity_id]}) == 1
    main = hass.states.get(MAIN)
    assert main.state == "1"
    assert main.attributes["last_update"] == first_update
    assert all(dep["departure"].endswith(":00+02:00") for dep in main.attributes["departures"])


def same_minute_board(first: int, second: int) -> list[dict[str, Any]]:
    """Return a 44 and a 9 shown at 21:03, predicted `first` / `second` s past 21:03."""
    return [
        tabs(213, [row("44", 3 + first / 60, "Koliba", delay=1, trip=1)]),
        tabs(214, [row("9", 3 + second / 60, "Karlova Ves", delay=0, trip=2)]),
    ]


async def test_same_minute_departures_keep_their_order(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two departures shown in the same minute don't swap when predictions jitter."""
    fake_feed.initial = same_minute_board(10, 12)
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_info([])
    await advance(hass, freezer, 2)
    changes = record(hass, MAIN, *PER_DEPARTURE)

    for first, second in ((13, 11), (10, 12), (13, 11)):  # ±2 s, both still 21:03
        freezer.tick(timedelta(seconds=3))
        listener.feed_tabs(same_minute_board(first, second))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert dict(changes) == {}
    assert hass.states.get("sensor.hodzovo_next_line").state == "9"
    assert hass.states.get("sensor.hodzovo_departure_2").attributes["line"] == "44"


async def test_soonest_departure_comes_first(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Departures shown in the same minute are ordered by their countdown.

    The 44 (21:03:50) and the 9 (21:04:40) are both shown at 21:04. At 21:00:20
    the 44 leaves in 3 minutes (leave now) and the 9 in 4: the 44 is next.
    """
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, CONF_WALKING_TIME: 3, CONF_LEAVE_WINDOW: 0},
    )
    fake_feed.initial = [
        tabs(213, [row("44", 3 + 50 / 60, "Koliba", delay=0, trip=1)]),
        tabs(214, [row("9", 4 + 40 / 60, "Karlova Ves", delay=0, trip=2)]),
    ]
    freezer.move_to(NOW + timedelta(seconds=20))
    await setup_entry(hass, config_entry)

    main = hass.states.get(MAIN)
    assert main.state == "3"
    departures = main.attributes["departures"]
    assert [(dep["line"], dep["time"], dep["minutes"]) for dep in departures] == [
        ("44", "21:04", 3),
        ("9", "21:04", 4),
    ]
    assert hass.states.get("sensor.hodzovo_next_line").state == "44"
    assert hass.states.get("sensor.hodzovo_departure_2").attributes["line"] == "9"
    leave = hass.states.get("binary_sensor.hodzovo_time_to_leave")
    assert (leave.state, leave.attributes["line"], leave.attributes["leave_in"]) == (
        STATE_ON,
        "44",
        0,
    )
    response = await hass.services.async_call(
        DOMAIN, "get_departures", {"entity_id": MAIN}, blocking=True, return_response=True
    )
    assert [dep["line"] for dep in response["departures"]] == ["44", "9"]


def boundary_board(jitter: int) -> list[dict[str, Any]]:
    """Return a 44 at 21:02:45 + `jitter` s (shown 21:02 or 21:03) and a 9 at 21:03:30."""
    return [
        tabs(213, [row("44", 2 + (45 + jitter) / 60, "Koliba", delay=0, trip=1)]),
        tabs(214, [row("9", 3 + 30 / 60, "Karlova Ves", delay=0, trip=2)]),
    ]


async def test_jitter_across_a_shown_minute_keeps_the_order(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A prediction moving across a shown minute doesn't swap it with a later one."""
    fake_feed.initial = boundary_board(-1)
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_info([])
    await advance(hass, freezer, 2)
    changes = record(hass, MAIN, "sensor.hodzovo_next_line")

    for jitter in (1, -1, 1, -1, 1, -1):  # 21:00:05 .. 21:00:20, the 44 leaves in 2 min
        freezer.tick(timedelta(seconds=3))
        listener.feed_tabs(boundary_board(jitter))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert states(changes["sensor.hodzovo_next_line"]) == []
    assert states(changes[MAIN]) == []
    assert hass.states.get(MAIN).state == "2"
    assert hass.states.get("sensor.hodzovo_next_line").state == "44"


async def test_changes_no_entity_shows_are_not_published(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Busy boards update rows beyond the shown ones all the time: no state write."""
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_MAX_DEPARTURES: 2}
    )  # shown: 2 in the list, 3 departure_N sensors
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_info([])
    await advance(hass, freezer, 2)
    first_update = hass.states.get(MAIN).attributes["last_update"]
    changes = record(hass, MAIN, *PER_DEPARTURE)

    hidden = sample_tabs()
    hidden[1]["tab"][1].update(casDelta=4, typ="online", issi="1:4404", predoslaZstr="Kozia")
    listener.feed_tabs(hidden)  # the 4th departure (N33) is not shown
    listener.feed_vehicle({"issi": "1:4404", "lf": 1, "ac": 1})
    await advance(hass, freezer, 2)
    assert dict(changes) == {}
    assert hass.states.get(MAIN).attributes["last_update"] == first_update

    shown = sample_tabs()
    shown[0]["tab"][1]["konecnaZstr"] = "Most SNP"  # the 3rd departure: departure_3
    listener.feed_tabs(shown)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hodzovo_departure_3").attributes["destination"] == "Most SNP"
    assert hass.states.get(MAIN).attributes["last_update"] != first_update


async def test_next_departure_is_the_minute_shown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """State and `departure` attributes agree with `time` (cas + 15 s, truncated)."""
    fake_feed.initial = [
        tabs(213, [row("44", 3 + 50 / 60, "Koliba", delay=0, trip=1)]),  # 21:03:50
        tabs(214, [row("9", 5 + 40 / 60, "Karlova Ves", delay=0, trip=2)]),  # 21:05:40
    ]
    await setup_entry(hass, config_entry)

    shown = "2026-10-03T21:04:00+02:00"
    nxt = hass.states.get(NEXT)
    assert nxt.state == "2026-10-03T19:04:00+00:00"
    assert (nxt.attributes["time"], nxt.attributes["departure"]) == ("21:04", shown)
    main = hass.states.get(MAIN).attributes
    assert (main["next_time"], main["next_departure"]) == ("21:04", shown)
    assert main["departures"][1]["departure"] == "2026-10-03T21:05:00+02:00"
    assert main["departures"][1]["time"] == "21:05"
    assert hass.states.get("sensor.hodzovo_departure_1").attributes["departure"] == shown
    assert hass.states.get("sensor.hodzovo_next_line").attributes["departure"] == shown
    assert hass.states.get("binary_sensor.hodzovo_time_to_leave").attributes["departure"] == shown
    # The countdown follows imhd.sk's prediction (3 min 50 s -> 3), like the board.
    assert main["next_minutes"] == 3

    # The service response keeps the prediction to the second.
    response = await hass.services.async_call(
        DOMAIN, "get_departures", {"entity_id": MAIN}, blocking=True, return_response=True
    )
    assert response["departures"][0]["departure"] == "2026-10-03T21:03:50+02:00"
    assert response["departures"][0]["time"] == "21:04"


async def test_frequently_changing_attributes_not_recorded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """`last_update` (and `next_minutes`, a copy of the state) stay out of the recorder."""
    await setup_entry(hass, config_entry)
    unrecorded = hass.states.get(MAIN).state_info["unrecorded_attributes"]
    assert {"departures", "info", "last_update", "next_minutes"} <= unrecorded


async def test_vehicle_moves_are_written_with_the_tick(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Shown vehicles passing stops write nothing until the 30 s tick.

    No state depends on `previous_stop` / `stops_away`: on busy stops they changed
    every few seconds and wrote most of the main sensor's states.
    """
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_info([])
    await advance(hass, freezer, 2)
    first_update = hass.states.get(MAIN).attributes["last_update"]
    changes = record(hass, MAIN, LEAVE, *PER_DEPARTURE)

    for passed, stop in enumerate(("Kollárovo nám.", "Poštová", "Kozia")):  # 21:00:02 .. 21:00:11
        payload = sample_tabs()
        payload[1]["tab"][0].update(tuZidx=12, predoslaZidx=9 + passed, predoslaZstr=stop)
        payload[0]["tab"][0].update(tuZidx=20, predoslaZidx=10 + passed, predoslaZstr="Patrónka")
        listener.feed_tabs(payload)
        await advance(hass, freezer, 3)
    assert dict(changes) == {}
    assert hass.states.get(FIRST).attributes["previous_stop"] is None

    await advance(hass, freezer, 20)  # the tick at 21:00:30
    first = hass.states.get(FIRST).attributes
    assert (first["line"], first["previous_stop"], first["stops_away"]) == ("4", "Kozia", 1)
    main = hass.states.get(MAIN).attributes
    assert [dep["stops_away"] for dep in main["departures"][:2]] == [1, 8]
    assert main["last_update"] == first_update


BURST = (MAIN, NEXT, FIRST, LEAVE)


async def start_burst_board(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    freezer: FrozenDateTimeFactory,
) -> tuple[list[dict[str, Any]], dict[str, list[tuple[State, State]]]]:
    """Set up Hodžovo nám. as captured just before an empty-platform burst.

    Returns the burst (imhd.sk's `tabs` events as received) and the recorded changes.
    """
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_LEAVE_WINDOW: 5}
    )
    sequence = load_json("tabs_ba_empty_burst.json")
    freezer.move_to(dt_util.utc_from_timestamp(sequence[0]["received_ms"] / 1000))
    fake_feed.initial = sequence[0]["payload"]
    await setup_entry(hass, config_entry)
    fake_feed.instances[0].listener.feed_info([])
    freezer.tick(timedelta(seconds=PUBLISH_DELAY))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(MAIN).state == "4"
    assert hass.states.get(FIRST).attributes["line"] == "N93"
    assert hass.states.get(LEAVE).state == STATE_ON
    return sequence[1:], record(hass, *BURST)


async def replay(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, listener: Any, events: list[dict[str, Any]]
) -> None:
    """Deliver `tabs` events at the time they were received."""
    for event in events:
        freezer.move_to(dt_util.utc_from_timestamp(event["received_ms"] / 1000))
        async_fire_time_changed(hass)
        listener.feed_tabs(event["payload"])
        await hass.async_block_till_done()


async def test_empty_platform_burst_does_not_flicker(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """imhd.sk now and then empties every platform for 0.6 s: nothing changes.

    Replays a capture of Hodžovo nám. (01:13:26): platforms A-D are sent empty
    within 12 ms and full again 0.6 s later, with vehicles moved on. Every
    platform used to be cleared in turn: a later bus became the next departure,
    then nothing (unknown), and time to leave went off and on again.
    """
    burst, changes = await start_burst_board(hass, config_entry, fake_feed, freezer)
    listener = fake_feed.instances[0].listener

    await replay(hass, freezer, listener, burst)
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE + 2)

    assert dict(changes) == {}


async def test_platforms_that_stay_empty_clear_after_the_grace(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Service ending for good: the emptied platforms clear once the grace is over."""
    burst, changes = await start_burst_board(hass, config_entry, fake_feed, freezer)
    listener = fake_feed.instances[0].listener

    await replay(hass, freezer, listener, burst[:4])  # A, B, C, D empty; no refill
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE - 1)
    assert dict(changes) == {}
    await advance(hass, freezer, PUBLISH_DELAY + 2)

    # Cleared together in one write, without showing a later bus on the way.
    assert [new.state for _old, new in changes[MAIN]] == [STATE_UNKNOWN]
    assert states(changes[NEXT]) == [STATE_UNKNOWN]
    assert states(changes[FIRST]) == [STATE_UNKNOWN]
    assert states(changes[LEAVE]) == [STATE_OFF]
    assert hass.states.get(MAIN).attributes["departures"] == []


async def test_departed_rows_dropped_while_a_platform_is_emptied(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The countdown tick still drops departed rows of a platform kept for the grace."""
    board = sample_tabs()
    board[1]["tab"][0] = row("4", 88 / 60, "Dúbravka", delay=0, trip=3)  # 21:01:28
    fake_feed.initial = board
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    await advance(hass, freezer, 117)

    listener.feed_tabs([tabs(214, [])])  # B: the 4 (left 29 s ago) and the N33
    await advance(hass, freezer, 3)  # the tick at 21:02:00
    assert departure_lines(hass) == ["9", "X13", "N33"]
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE)
    assert departure_lines(hass) == ["9", "X13"]


async def test_emptied_platform_across_a_reconnect(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A disconnect during the grace leaves the decision to the next session.

    The grace still runs from when the platform came empty: the next session
    sending it empty again clears it once the grace is over.
    """
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    listener = feed.listener

    listener.feed_tabs([tabs(214, [])])
    await advance(hass, freezer, 1)
    feed.disconnect()
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE + 2)
    assert departure_lines(hass) == ["4", "9", "X13", "N33"]

    listener.feed_connected()
    listener.feed_tabs([sample_tabs()[0], tabs(214, [])])  # B is still empty
    await advance(hass, freezer, PUBLISH_DELAY + 1)
    assert departure_lines(hass) == ["9", "X13"]


async def test_short_sessions_do_not_restart_the_grace(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sessions shorter than the grace (connect, data, drop) can't keep emptied rows.

    Every new session sending the platform empty again used to restart the grace:
    the departures imhd.sk withdrew stayed for as long as the connection flapped.
    """
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    feed.listener.feed_tabs([tabs(214, [])])  # 21:00:00, B: the 4 and the N33 are gone
    await advance(hass, freezer, 3)
    feed.disconnect()
    await advance(hass, freezer, 1)
    feed.listener.feed_connected()
    feed.listener.feed_tabs([sample_tabs()[0], tabs(214, [])])  # B is still empty
    await advance(hass, freezer, 1)  # the grace is over at 21:00:05
    assert departure_lines(hass) == ["4", "9", "X13", "N33"]
    await advance(hass, freezer, PUBLISH_DELAY)
    assert departure_lines(hass) == ["9", "X13"]


async def test_every_burst_gets_the_full_grace(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refilled platform's grace starts afresh when it comes empty again."""
    await setup_entry(hass, config_entry)
    listener = fake_feed.instances[0].listener
    listener.feed_tabs([tabs(214, [])])  # an empty burst, refilled at once
    listener.feed_tabs([sample_tabs()[1]])
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE * 2)

    listener.feed_tabs([tabs(214, [])])  # the next burst
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE - 1)
    assert departure_lines(hass) == ["4", "9", "X13", "N33"]


async def test_unload_during_the_grace(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
) -> None:
    """Unloading while a platform is emptied leaves no timer (the test harness checks)."""
    await setup_entry(hass, config_entry)
    fake_feed.instances[0].listener.feed_tabs([tabs(214, [])])
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_http")
@pytest.mark.parametrize("disconnect", [False, True], ids=["tick", "disconnect"])
async def test_platform_clear_moves_last_update(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    freezer: FrozenDateTimeFactory,
    disconnect: bool,
) -> None:
    """A platform cleared after the grace moves `last_update`, whoever publishes it.

    B comes empty at 21:00:24.5 and stays empty: it is cleared at 29.5, for a
    coalesced publish at 30.5. The 30 s tick at 30.0 (or a disconnect) used to
    publish it first, as an update that leaves `last_update` as it was.
    """
    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    feed.listener.feed_info([])
    await advance(hass, freezer, 24)
    first_update = hass.states.get(MAIN).attributes["last_update"]
    freezer.tick(timedelta(seconds=0.5))
    feed.listener.feed_tabs([tabs(214, [])])  # B: the 4 and the N33 are gone for good
    await advance(hass, freezer, EMPTY_PLATFORM_GRACE)  # 21:00:29.5
    if disconnect:
        feed.disconnect()
    else:
        freezer.move_to(NOW + timedelta(seconds=30))  # the tick
        async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert departure_lines(hass) == ["9", "X13"]

    await advance(hass, freezer, PUBLISH_DELAY + 1)
    assert hass.states.get(MAIN).attributes["last_update"] != first_update


@pytest.mark.usefixtures("mock_http")
@pytest.mark.parametrize("disconnect", [False, True], ids=["tick", "disconnect"])
async def test_platforms_emptied_together_clear_together(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    freezer: FrozenDateTimeFactory,
    disconnect: bool,
) -> None:
    """Platforms sent empty a few ms apart clear in one go, whoever publishes it.

    B comes empty at 21:00:24.995 and A at 25.005 (the capture had them 11 ms
    apart), and neither is refilled. Their graces ended 10 ms apart: the 30 s
    tick (or a disconnect, which also stopped A's grace) published B cleared and
    A not, so A's 9 (21:04) became the next departure on the way to unknown.
    """

    async def at(seconds: float) -> None:
        freezer.move_to(NOW + timedelta(seconds=seconds))
        async_fire_time_changed_exact(hass)
        await hass.async_block_till_done()

    await setup_entry(hass, config_entry)
    feed = fake_feed.instances[0]
    feed.listener.feed_info([])
    await at(2)
    changes = record(hass, NEXT)
    await at(24.995)
    feed.listener.feed_tabs([tabs(214, [])])  # B: the 4 (21:01) and the N33
    await at(25.005)
    feed.listener.feed_tabs([tabs(213, [])])  # A: the 9 (21:04) and the X13
    await at(29.996)  # B's grace is over
    if disconnect:
        feed.disconnect()
    await at(30.0005)  # the tick
    await advance(hass, freezer, PUBLISH_DELAY + 2)

    assert states(changes[NEXT]) == [STATE_UNKNOWN]
    assert departure_lines(hass) == []
