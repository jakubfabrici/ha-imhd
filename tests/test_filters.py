"""Tests for the departure filters."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.core import HomeAssistant

from custom_components.imhd.api import parse_tabs
from custom_components.imhd.coordinator import DepartureFilter, as_list
from custom_components.imhd.models import Departure

from .conftest import LABELS, NOW, row, tabs


def departures() -> list[Departure]:
    """Return a varied set of departures (sorted)."""
    payload = [
        tabs(
            213,
            [
                row("9", 2, "Karlova Ves", trip=1),
                row("X13", 5, "Petržalka, Jungmannova", trip=2),
                row("N33", 8, "Hlavná stanica", trip=3),
            ],
        ),
        tabs(
            214,
            [
                row("4", 1, "Dúbravka", trip=4),
                row("93", 6, "Petrzalka, Vysehradska", trip=5),
                row("x13", 9, "Kollárovo nám.", trip=6),
            ],
        ),
    ]
    return parse_tabs(payload, LABELS, NOW, stop_id=83)


def lines(options: dict[str, Any]) -> list[str]:
    """Apply a filter built from options and return the remaining lines."""
    matching = DepartureFilter.from_options(options).apply(departures(), NOW)
    return [dep.line for dep in matching]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ["4", "9", "X13", "93", "N33", "x13"]),
        ({"platforms": ["*"]}, ["4", "9", "X13", "93", "N33", "x13"]),
        ({"platforms": ["a"]}, ["9", "X13", "N33"]),
        ({"platforms": ["214"]}, ["4", "93", "x13"]),
        ({"platforms": "A, 214"}, ["4", "9", "X13", "93", "N33", "x13"]),
        ({"lines": ["x13"]}, ["X13", "x13"]),
        ({"lines": "9, 4"}, ["4", "9"]),
        ({"lines": ["1"]}, []),
        ({"exclude_lines": ["N33", "x13"]}, ["4", "9", "93"]),
        ({"direction": "petrzalka"}, ["X13", "93"]),
        ({"direction": ["PETRŽALKA", "dubrav"]}, ["4", "X13", "93"]),
        ({"direction": "Kollárovo nám., X"}, []),
        ({"lines": ["X13"], "direction": "Petržalka"}, ["X13"]),
    ],
)
async def test_filters(hass: HomeAssistant, options: dict[str, Any], expected: list[str]) -> None:
    """Platform, line and direction filters."""
    assert lines(options) == expected


async def test_walking_time_and_truncation(hass: HomeAssistant) -> None:
    """Departures you cannot catch are dropped; the list is truncated."""
    flt = DepartureFilter.from_options({"walking_time": 3, "max_departures": 2})
    matching = flt.apply(departures(), NOW)
    assert [(dep.line, dep.minutes, dep.leave_in) for dep in matching[:3]] == [
        ("X13", 5, 2),
        ("93", 6, 3),
        ("N33", 8, 5),
    ]
    assert flt.max_departures == 2


async def test_no_walking_time_keeps_imminent(hass: HomeAssistant) -> None:
    """Without walking time nothing is dropped and leave_in equals minutes."""
    matching = DepartureFilter.from_options({}).apply(departures(), NOW)
    assert matching[0].line == "4"
    assert matching[0].leave_in == matching[0].minutes == 1


def test_as_list() -> None:
    """Options may be strings or lists."""
    assert as_list(None) == []
    assert as_list("9, X13 ,") == ["9", "X13"]
    assert as_list(["9", 13, " "]) == ["9", "13"]
    assert as_list("Malacky, Lesná", split=False) == ["Malacky, Lesná"]
