# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-03

First public release. Requires Home Assistant **2025.2.0** or newer.

### Added

- Departures from imhd.sk, pushed over imhd.sk's own socket.io feed: one
  connection per configured stop, no add-on, MQTT broker, pyscript or browser
  needed. Partial per-platform updates are merged, and countdowns are
  recomputed every 30 seconds. A departure is kept until 30 seconds after it
  leaves.
- Robust connection handling: reconnects with back-off (2 s to 60 s), a
  watchdog for boards that go silent, and quiet stops (night, small towns)
  shown as an empty board instead of an error. Entities become unavailable only
  after 300 s without a connection.
- When imhd.sk refuses a connection (codes −10/−11/−12: too many users,
  connections, or connections from your IP address), the integration waits
  15–60 minutes between attempts and creates a repair issue. `imhd.refresh`
  retries immediately.
- All 22 imhd.sk sections: Bratislava, Banská Bystrica, Hlohovec, Košice,
  Liptovský Mikuláš, Martin, Nitra, Nové Zámky, Piešťany, Poprad-Tatry,
  Považská Bystrica, Prešov, Prievidza, Ružomberok, Senica, Skalica, Spišská
  Nová Ves, Trenčín, Trnava, Zvolen, Žilina and the country-wide
  "Slovensko a svet" (`transport`). Vehicle-tracked (realtime) departures were
  seen in Bratislava and Prešov; elsewhere imhd.sk publishes timetable data.
- Config flow: pick the city, then find the stop nearest to your home, nearest
  to a point on the map, by name (imhd.sk search), or by stop ID / pasted
  imhd.sk link (`?st=` board links and `/zastavka/…` stop pages). Options flow
  for the filters, and a reconfigure flow that changes the stop and keeps the
  entity ids (UI entries only; YAML-imported stops are managed in YAML).
- YAML configuration (`imhd: !include imhd.yaml`). `stop:` accepts a stop ID,
  an imhd.sk link or the exact stop name. Items are imported into regular
  config entries, imported entries are updated on restart, and a repair issue
  tells you when a stop was removed from YAML.
- Filters per stop: platforms (labels or ids), lines, excluded lines (for
  example `►` service trips), direction (headsign or terminal stop, case and
  diacritics ignored) and the maximum number of departures.
- Walking time: departures you can't catch any more are hidden, and
  `leave_in` tells you when to leave.
- Entities per stop with language-independent ids (`<domain>.<name>_<key>`):
  `Departures` (template-friendly main sensor with the full departure list),
  `Next departure` (timestamp), `Next line`, `Delay`, `Departure 1…N`, and the
  binary sensors `Time to leave`, `Realtime connection` and `Service alert`.
- Departure details: expected and scheduled time (`time` rounded to the nearest
  minute), minutes and `leave_in`, delay (imhd.sk's own value), realtime flag,
  platform and platform id, headsign, terminal stop and its town, vehicle
  number and model, low-floor and air-conditioning flags, the stop the vehicle
  passed last and how many stops away it is, a "stuck" flag, trip id, and the
  board text recomputed locally (`~` marks timetable-only departures).
- Service alerts (imhd.sk info texts) as a binary sensor with the messages.
- Actions: `imhd.get_departures` and `imhd.find_stops` (with response data)
  and `imhd.refresh`.
- Diagnostics download.
- English and Slovak translations.
- Examples: an annotated `imhd.yaml`, template sensors, a templating cookbook,
  dashboards (core cards and Mushroom), TTS and service-alert automations, an
  openHASP departure board, and a "time to leave" notification blueprint.

[Unreleased]: https://github.com/jakubfabrici/ha-imhd/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jakubfabrici/ha-imhd/releases/tag/v1.0.0
