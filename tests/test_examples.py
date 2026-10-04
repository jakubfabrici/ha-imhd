"""Tests for the example configurations."""

from __future__ import annotations

from pathlib import Path

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

EXAMPLES = Path(__file__).parents[1] / "examples"


async def test_openhasp_sends_only_drawable_characters(hass: HomeAssistant) -> None:
    """The plate's font has no "►" (service trips): header, badge and destination send ">"."""
    config = yaml.safe_load(
        (EXAMPLES / "openhasp" / "imhd_openhasp.yaml").read_text(encoding="utf-8")
    )
    hass.states.async_set(
        "sensor.hodzovo_departures",
        "4",
        {
            "stop_name": "Hodžovo nám.",
            "city": "Bratislava",
            "lines": ["42", "44", "►"],
            "connected": True,
            "departures": [
                {
                    "line": "►",
                    "destination": "Hrad ► Depo Hroboňova",
                    "minutes": 4,
                    "time": "23:10",
                    "realtime": False,
                }
            ],
        },
    )
    variables = dict(config["variables"])
    for name, template in config["actions"][0]["variables"].items():
        variables[name] = Template(template, hass).async_render(variables)

    commands = dict(variables["commands"])
    assert commands["p1b3.text"] == "Bratislava • 42, 44, >"
    assert commands["p1b10.text"] == ">"
    assert commands["p1b11.text"] == "Hrad > Depo Hroboňova"
    assert not any("►" in str(value) for value in commands.values())
