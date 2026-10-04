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
  recomputed every 30 seconds, in real time, so they stay right across the
  summer-time changes. A departure is kept until 30 seconds after it leaves.
- Few state changes on busy stops: identical repeats, prediction shifts of a
  few seconds within the minute shown and changes to departures no entity
  shows write nothing. Departures are listed by countdown like on the imhd.sk
  board, so `minutes` never decreases down the list. The Next departure, Next
  line and Delay sensors write about once a minute plus real changes (delay,
  line); the shown vehicles' positions (`previous_stop`, `stops_away`) are
  updated with the countdown, every 30 seconds. The platforms imhd.sk now and
  then sends empty for a moment keep their departures for 5 seconds, so the
  entities don't flicker to a later departure or `unknown`.
  `departures`, `info`, `next_minutes` and `last_update` aren't recorded.
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
  "Slovakia & world" (*Slovensko a svet*, `transport`; `city:` accepts all
  three names). Vehicle-tracked (realtime) departures were seen in Bratislava
  and Prešov; elsewhere imhd.sk publishes timetable data.
- Config flow: pick the city, then find the stop nearest to your home, nearest
  to a point on the map, by name (imhd.sk search), or by stop ID / pasted
  imhd.sk link (`?st=` board links and `/zastavka/…` stop pages). Nearby
  stops of another city are saved (and returned by `imhd.find_stops`) with
  that city's section and board link. Options flow for the filters, and a
  reconfigure flow that changes the stop and keeps the entity ids (UI entries
  only; YAML-imported stops are managed in YAML).
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
  `Next departure` (timestamp of the minute imhd.sk shows), `Next line`,
  `Delay`, `Departure 1…N`, and the binary sensors `Time to leave` (with the
  departure's `minutes` and `leave_in`), `Realtime connection` and
  `Service alert`.
- Departure details: expected and scheduled time (`departure` and `time` are
  the minute the imhd.sk board shows; `imhd.get_departures` returns imhd.sk's
  prediction to the second), minutes and `leave_in`, delay (imhd.sk's own
  value), realtime flag, platform and platform id, headsign, terminal stop and
  its town, vehicle number and model, low-floor and air-conditioning flags, the
  stop the vehicle passed last and how many stops away it is, a "stuck" flag,
  trip id, and the board text recomputed locally (`~` marks timetable-only
  departures).
- Service alerts (imhd.sk info texts) as a binary sensor with the messages.
  The alert is kept across reconnects and restored after a restart or reload
  until imhd.sk confirms it, so an active alert doesn't turn off and on again.
  An alert of another stop (after a reconfigure) is not restored.
- Actions: `imhd.get_departures` and `imhd.find_stops` (with response data)
  and `imhd.refresh`.
- Diagnostics download.
- English and Slovak translations.
- Brand icon (Material Design Icons "bus-clock", Apache-2.0), so the
  integrations page and the device page no longer show a placeholder on Home
  Assistant versions that load brand images from custom integrations.
- Examples: an annotated `imhd.yaml`, template sensors, a templating cookbook,
  dashboards (core cards and Mushroom; the full view shows a service alert
  once, in its own card), a TTS automation, a service-alert notification (one
  notification per new alert, silent across restarts, reloads and feed
  outages), an openHASP departure board (destinations shortened at whole words,
  only characters the plate's default font can draw), and a "time to leave"
  notification blueprint (its `minutes` is the sensor's own countdown).

[Unreleased]: https://github.com/jakubfabrici/ha-imhd/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jakubfabrici/ha-imhd/releases/tag/v1.0.0
