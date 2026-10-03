"""Tests for the time-to-leave notification blueprint."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.const import CONF_LEAVE_WINDOW, CONF_WALKING_TIME

from .conftest import NOW, FakeFeed, row, setup_entry, tabs

BLUEPRINT = Path(__file__).parents[1] / "blueprints" / "automation" / "imhd" / "time_to_leave.yaml"
LEAVE = "binary_sensor.hodzovo_time_to_leave"
MAIN = "sensor.hodzovo_departures"


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use a temporary config directory that holds the blueprint."""
    target = Path(hass_tmp_config_dir, "blueprints", "automation", "imhd")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, target)
    return hass_tmp_config_dir


async def test_blueprint_variables_match_the_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_feed: type[FakeFeed],
    mock_http: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """`minutes`, `leave_in` and `departure_time` are the values the entities show.

    The 44 is predicted at 21:06:40, so the board shows 21:06 and `departure` is
    21:06:00: a countdown computed from it would be one minute short.
    """
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, CONF_WALKING_TIME: 3, CONF_LEAVE_WINDOW: 2},
    )
    fake_feed.initial = [tabs(213, [row("44", 6 + 40 / 60, "Koliba", delay=0, trip=1)])]
    freezer.move_to(NOW + timedelta(seconds=10))  # 21:00:10, ticks at :10 and :40
    await setup_entry(hass, config_entry)
    assert hass.states.get(LEAVE).state == STATE_OFF  # leave_in 3
    notifications = async_mock_service(hass, "notify", "test")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "imhd/time_to_leave.yaml",
                    "input": {
                        "leave_sensor": LEAVE,
                        "notify_action": "notify.test",
                        "message": "{{ minutes }}|{{ leave_in }}|{{ departure_time }}|{{ line }}",
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()

    for _ in range(60):  # until 21:01:10: 5 min 30 s to go, leave in 2 min
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    leave = hass.states.get(LEAVE)
    assert leave.state == STATE_ON
    assert leave.attributes["departure"] == "2026-10-03T21:06:00+02:00"
    assert (leave.attributes["minutes"], leave.attributes["leave_in"]) == (5, 2)
    assert hass.states.get(MAIN).attributes["next_minutes"] == 5
    assert [call.data["message"] for call in notifications] == ["5|2|21:06|44"]
