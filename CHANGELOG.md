# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-03

First public release.

### Added

- Realtime departures from imhd.sk, pushed over imhd.sk's own socket.io feed
  (one connection per configured stop, automatic reconnect with back-off and
  a watchdog for stale data). No add-on, MQTT broker, pyscript or browser needed.
- Support for all imhd.sk cities/sections: Bratislava, Banská Bystrica,
  Hlohovec, Košice, Liptovský Mikuláš, Martin, Nitra, Nové Zámky, Piešťany,
  Poprad-Tatry, Považská Bystrica, Prešov, Prievidza, Ružomberok, Senica,
  Skalica, Spišská Nová Ves, Trenčín, Trnava, Zvolen and Žilina.
- Config flow: pick the city, then find the stop by name, nearest to your home,
  nearest to a point on the map, or by stop ID / imhd.sk URL. Options flow for
  the filters and a reconfigure flow to change the stop.
- YAML configuration (`imhd: !include imhd.yaml`), imported into regular config
  entries; a repair issue tells you when a stop was removed from YAML.
- Filters per stop: platforms, lines, excluded lines, direction (case and
  diacritics insensitive), maximum number of departures.
- Walking time: departures you cannot catch any more are hidden, `leave_in`
  tells you when to leave.
- Entities per stop: `Departures` (template-friendly main sensor with the full
  departure list in its attributes), `Next departure` (timestamp), `Next line`,
  `Delay`, `Departure 1…N`, `Time to leave`, `Realtime connection` and
  `Service alert` binary sensors.
- Departure details: expected and scheduled time, delay, realtime flag,
  platform, vehicle number, low-floor and air-conditioning flags, "stuck"
  vehicle flag, imhd.sk display text and trip id.
- Service alerts (imhd.sk info texts) as a binary sensor with the messages.
- Actions: `imhd.get_departures` and `imhd.find_stops` (with response data)
  and `imhd.refresh`.
- Diagnostics download and a repair issue when imhd.sk rejects a stop.
- English and Slovak translations.
- Examples: annotated `imhd.yaml`, template sensors, dashboards (core cards and
  Mushroom), TTS and service-alert automations, an openHASP departure board,
  and a "time to leave" notification blueprint.

[Unreleased]: https://github.com/jakubfabrici/ha-imhd/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jakubfabrici/ha-imhd/releases/tag/v1.0.0
