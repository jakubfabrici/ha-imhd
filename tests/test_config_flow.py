"""Tests for the config, options and reconfigure flows."""

from __future__ import annotations

from collections.abc import Generator
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.imhd.const import (
    BASE_URL,
    CONF_DEPARTURE_SENSORS,
    CONF_DIRECTION,
    CONF_EXCLUDE_LINES,
    CONF_LEAVE_WINDOW,
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_PLATFORMS,
    CONF_SECTION,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_WALKING_TIME,
    DOMAIN,
    SECTIONS,
)

from .conftest import LABELS, NEAREST_URL, SEARCH_URL, load_fixture, load_json

COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN

SETTINGS: dict[str, Any] = {
    CONF_NAME: "Hodzovo",
    CONF_PLATFORMS: ["A"],
    CONF_LINES: ["9", "X13"],
    CONF_DIRECTION: ["Petržalka"],
    CONF_MAX_DEPARTURES: 5,
    CONF_WALKING_TIME: 4,
    CONF_LEAVE_WINDOW: 2,
    CONF_DEPARTURE_SENSORS: 2,
}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Do not set entries up in flow tests."""
    with patch("custom_components.imhd.async_setup_entry", return_value=True) as mock:
        yield mock


async def start(hass: HomeAssistant, method: str, city: str = "ba") -> dict[str, Any]:
    """Start the user flow and choose a city and lookup method."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"city": city, "method": method}
    )


async def finish(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Complete the settings step."""
    assert result["step_id"] == "settings"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result


async def test_nearest_flow(hass: HomeAssistant, mock_http: AiohttpClientMocker) -> None:
    """Nearest to home -> pick -> settings creates the entry."""
    result = await start(hass, "nearest")
    assert result["step_id"] == "pick_stop"
    options = result["data_schema"].schema["stop_choice"].config["options"]
    assert options[0] == {"value": "83", "label": "Hodžovo nám. (Bratislava) · A, B, C, D · 55 m"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop_choice": "83"}
    )
    platforms = result["data_schema"].schema[CONF_PLATFORMS].config["options"]
    assert platforms == ["A", "B", "C", "D"]
    result = await finish(hass, result)

    assert result["title"] == "Hodzovo"
    assert result["data"] == {
        CONF_SECTION: "ba",
        CONF_STOP_ID: 83,
        CONF_NAME: "Hodzovo",
        CONF_STOP_NAME: "Hodžovo nám.",
        "stop_city": "Bratislava",
        "platform_labels": LABELS,
    }
    assert result["options"] == {
        CONF_PLATFORMS: ["A"],
        CONF_LINES: ["9", "X13"],
        CONF_EXCLUDE_LINES: [],
        CONF_DIRECTION: ["Petržalka"],
        CONF_MAX_DEPARTURES: 5,
        CONF_WALKING_TIME: 4,
        CONF_LEAVE_WINDOW: 2,
        CONF_DEPARTURE_SENSORS: 2,
    }
    assert result["result"].unique_id == "ba_83_hodzovo"


@pytest.mark.parametrize(
    ("city", "stop"),
    [("ba", "83"), ("za", "https://imhd.sk/ba/online-zastavkova-tabula?st=83")],
)
async def test_stop_id_flow(
    hass: HomeAssistant, mock_http: AiohttpClientMocker, city: str, stop: str
) -> None:
    """A stop id or board URL (its section wins) leads straight to the settings."""
    result = await start(hass, "stop_id", city=city)
    assert result["step_id"] == "stop_id"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": stop})
    result = await finish(hass, result)
    assert result["data"][CONF_SECTION] == "ba"


async def test_search_flow(hass: HomeAssistant, mock_http: AiohttpClientMocker) -> None:
    """Search by name -> pick -> settings."""
    result = await start(hass, "search")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"query": "hodzovo"})
    assert result["step_id"] == "pick_stop"
    # A single hit is still confirmed; the search API has no platform labels.
    options = result["data_schema"].schema["stop_choice"].config["options"]
    assert options == [{"value": "83", "label": "Hodžovo nám. (Bratislava)"}]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop_choice": "83"}
    )
    await finish(hass, result)


async def test_location_flow(hass: HomeAssistant, mock_http: AiohttpClientMocker) -> None:
    """Nearest to a point on the map."""
    result = await start(hass, "location")
    assert result["step_id"] == "location"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"location": {"latitude": 48.149, "longitude": 17.107}}
    )
    assert result["step_id"] == "pick_stop"
    _, url, _, _ = mock_http.mock_calls[-1]
    assert url.query["lat"] == "48.149000"


async def test_flow_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Connection problems, unknown stops and empty results are reported."""
    aioclient_mock.get(NEAREST_URL, exc=TimeoutError())
    aioclient_mock.get(SEARCH_URL, json={"results": []})
    aioclient_mock.get(
        f"{BASE_URL}/ba/online-zastavkova-tabula?st=9999999",
        text=load_fixture("stop_page_missing.html"),
    )

    result = await start(hass, "nearest")
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}

    result = await start(hass, "search")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"query": "xyz"})
    assert result["errors"] == {"base": "no_stops_found"}

    result = await start(hass, "stop_id")
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {"stop": "Hodžovo"})
    assert result["errors"] == {"base": "invalid_stop"}
    result = await hass.config_entries.flow.async_configure(flow_id, {"stop": "9999999"})
    assert result["errors"] == {"base": "stop_not_found"}


async def test_already_configured(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_http: AiohttpClientMocker
) -> None:
    """The same stop with the same name cannot be added twice."""
    result = await start(hass, "stop_id")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "83"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The options flow edits the filters."""
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"].schema[CONF_PLATFORMS].config["options"] == ["A", "B", "C", "D"]
    settings = {k: v for k, v in SETTINGS.items() if k != CONF_NAME}
    settings[CONF_EXCLUDE_LINES] = ["N33"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], settings)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_EXCLUDE_LINES] == ["N33"]
    assert config_entry.options[CONF_LINES] == ["9", "X13"]
    assert config_entry.options[CONF_MAX_DEPARTURES] == 5


async def test_reconfigure_changes_stop(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_http: AiohttpClientMocker
) -> None:
    """Reconfigure switches the stop and keeps the entity ids."""
    mock_http.get(
        f"{BASE_URL}/za/online-zastavkova-tabula?st=1831",
        text=load_fixture("stop_page_za_1831.html"),
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "ba_83_hodzovo_departures",
        config_entry=config_entry,
        suggested_object_id="hodzovo_departures",
    )
    config_entry.mock_state(hass, ConfigEntryState.LOADED)

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"city": "za", "method": "stop_id"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "https://imhd.sk/za/online-zastavkova-tabula?st=1831"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    assert config_entry.unique_id == "za_1831_hodzovo"
    assert config_entry.data[CONF_SECTION] == "za"
    assert config_entry.data[CONF_STOP_ID] == 1831
    assert config_entry.data[CONF_STOP_NAME] == "Hurbanova"
    assert config_entry.data["platform_labels"] == {}
    assert config_entry.options[CONF_PLATFORMS] == []
    entity = registry.async_get("sensor.hodzovo_departures")
    assert entity.unique_id == "za_1831_hodzovo_departures"


async def test_reconfigure_conflict(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_http: AiohttpClientMocker
) -> None:
    """Reconfiguring onto a stop configured by another entry is refused."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="ba_83_center", data={CONF_NAME: "Center"}
    ).add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ba_1_center",
        title="Center",
        data={CONF_SECTION: "ba", CONF_STOP_ID: 1, CONF_NAME: "Center"},
    )
    other.add_to_hass(hass)
    result = await other.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"city": "ba", "method": "stop_id"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "83"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_stop_id_unicode_digits(hass: HomeAssistant, mock_http: AiohttpClientMocker) -> None:
    """Unicode digits give a form error, not "Unknown error" (finding 8)."""
    result = await start(hass, "stop_id")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "²"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_stop"}


def _yaml_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IMPORT,
        title="Hodzovo",
        unique_id="ba_83_hodzovo",
        data={CONF_SECTION: "ba", CONF_STOP_ID: 83, CONF_NAME: "Hodzovo"},
        options={CONF_LINES: ["9"]},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_yaml_entry_aborts(hass: HomeAssistant) -> None:
    """Entries from YAML are changed in YAML (finding 9)."""
    entry = _yaml_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "yaml_managed"


async def test_options_flow_yaml_entry_notes_override(hass: HomeAssistant) -> None:
    """The options form of a YAML entry explains that YAML wins on restart."""
    entry = _yaml_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "yaml"
    settings = {k: v for k, v in SETTINGS.items() if k != CONF_NAME}
    result = await hass.config_entries.options.async_configure(result["flow_id"], settings)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LINES] == ["9", "X13"]


async def test_import_without_name_skips_lookup_when_known(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An unchanged YAML stop without a name does not fetch the board page again."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IMPORT,
        title="Hodžovo nám.",
        unique_id="ba_83_hodzovo_nam",
        data={
            CONF_SECTION: "ba",
            CONF_STOP_ID: 83,
            CONF_NAME: "Hodžovo nám.",
            CONF_STOP_NAME: "Hodžovo nám.",
        },
        options={},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data={"city": "ba", "stop": 83}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert aioclient_mock.call_count == 0
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize(("stop_city", "section"), [("Bratislava", "ba"), ("Malacky", "ke")])
@pytest.mark.parametrize("method", ["nearest", "location"])
async def test_nearest_saves_stop_city_section(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    method: str,
    stop_city: str,
    section: str,
) -> None:
    """Nearest stops keep the section of their own city, not the chosen one."""
    nearest = load_json("nearest_ba.json")
    for stop in nearest["stops"]:
        stop["city"] = stop_city
    aioclient_mock.get(f"{BASE_URL}/ke/api/cepo", json=nearest)
    page = load_fixture("stop_page_ba_83.html")
    aioclient_mock.get(f"{BASE_URL}/ba/online-zastavkova-tabula?st=83", text=page)
    # Stop ids are global: the Košice board shows the stop with a city prefix.
    aioclient_mock.get(
        f"{BASE_URL}/ke/online-zastavkova-tabula?st=83",
        text=page.replace('"section":"ba"', '"section":"ke"').replace(
            '"stopName":"Hod', '"stopName":"Bratislava, Hod'
        ),
    )

    result = await start(hass, method, city="ke")
    if method == "location":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"location": {"latitude": 48.149, "longitude": 17.107}}
        )
    assert result["step_id"] == "pick_stop"
    options = result["data_schema"].schema["stop_choice"].config["options"]
    assert options[0]["label"].startswith(f"Hodžovo nám. ({stop_city})")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop_choice": "83"}
    )
    url = f"{BASE_URL}/{section}/online-zastavkova-tabula?st=83"
    assert result["description_placeholders"]["url"] == url
    result = await finish(hass, result)
    assert result["data"][CONF_SECTION] == section
    assert result["result"].unique_id == f"{section}_83_hodzovo"


async def test_minute_fields_have_unit(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_http: AiohttpClientMocker
) -> None:
    """Minute fields show their unit in the settings and options forms."""
    result = await start(hass, "stop_id")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"stop": "83"})
    options = await hass.config_entries.options.async_init(config_entry.entry_id)
    for form in (result, options):
        schema = form["data_schema"].schema
        assert schema[CONF_WALKING_TIME].config["unit_of_measurement"] == "min"
        assert schema[CONF_LEAVE_WINDOW].config["unit_of_measurement"] == "min"
        assert "unit_of_measurement" not in schema[CONF_MAX_DEPARTURES].config
        assert "unit_of_measurement" not in schema[CONF_DEPARTURE_SENSORS].config


def _translations(name: str) -> dict[str, Any]:
    path = COMPONENT / name if name == "strings.json" else COMPONENT / "translations" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


async def test_city_selector_uses_translations(hass: HomeAssistant) -> None:
    """City labels come from selector.city, which covers every section in en and sk."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    config = result["data_schema"].schema["city"].config
    assert config["translation_key"] == "city"
    assert {"value": "ke", "label": "Košice"} in config["options"]
    for name in ("en.json", "sk.json"):
        assert set(_translations(name)["selector"]["city"]["options"]) == set(SECTIONS)


async def test_translations_consistent(hass: HomeAssistant) -> None:
    """strings.json equals en.json; sk has the same keys and placeholders."""
    en = _flatten(_translations("en.json"))
    sk = _flatten(_translations("sk.json"))
    assert _flatten(_translations("strings.json")) == en
    assert set(sk) == set(en)
    for key, text in en.items():
        assert set(re.findall(r"{(\w+)}", sk[key])) == set(re.findall(r"{(\w+)}", text)), key

    result = await start(hass, "search")
    hint = "config.step.search.data_description.query"
    for strings in (en, sk):
        assert ".." not in strings[hint].format(**result["description_placeholders"])
