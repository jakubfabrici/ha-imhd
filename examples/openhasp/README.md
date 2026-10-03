# openHASP departure board

Show the next five departures of a stop on an [openHASP](https://www.openhasp.com/)
touch plate (320×480 portrait, e.g. a WT32-SC01 or any other 3.5" plate).
No pyscript, no add-on: a single Home Assistant automation reads
`sensor.<stop>_departures` and pushes the texts and colours over MQTT.

![openHASP plate showing departures](../../docs/images/openhasp.png)

| File | What it is |
|---|---|
| [`pages.jsonl`](pages.jsonl) | Layout for page 1 of the plate (header, 5 rows, footer). |
| [`imhd_openhasp.yaml`](imhd_openhasp.yaml) | Automation that fills the layout from the departures sensor. |

## Requirements

* An openHASP plate (firmware 0.7.x) with a 320×480 portrait screen, connected
  to your MQTT broker. Note its **plate name** (Settings → MQTT on the plate's
  web page); the examples use `plate01`.
* The **MQTT** integration in Home Assistant, using the same broker.
* At least one stop configured in IMHD.sk Departures. The examples use the
  stop named `Hodzovo` → `sensor.hodzovo_departures`.

## 1. Upload the layout

1. Open the plate's web page → **Utilities → File Editor** (or **Files**) and
   upload [`pages.jsonl`](pages.jsonl). If you already have a `pages.jsonl`,
   merge the lines into it (they only use page 1; change `"page":1` if needed).
2. Make sure **Settings → HASP Design → Startup layout** is `/pages.jsonl`.
3. Restart the plate (or publish `restart` to `hasp/plate01/command/restart`).

The layout uses these colours:

| Element | Colour |
|---|---|
| Background | `#1A1A2E` |
| Header and footer bar | `#16213E` |
| Column-header bar and row separators | `#0F3460` |
| Column-header text, footer text | `#4488CC` |
| Line badge | `#E94560` |
| Time: under 5 min / under 15 min / later | `#00FF88` / `#FFCC00` / white |

## 2. Add the automation

1. **Settings → Automations & scenes → Create automation → ⋮ → Edit in YAML**.
2. Paste [`imhd_openhasp.yaml`](imhd_openhasp.yaml).
3. Edit the block at the top:

   ```yaml
   variables:
     plate: plate01                                   # your plate name
     departures_sensor: sensor.hodzovo_departures     # your stop
     disruption_sensor: binary_sensor.hodzovo_disruption
     subtitle: ""                                     # "" = "<city> · <lines>"
   ```

4. Use the same sensor in the first trigger (`entity_id: sensor.hodzovo_departures`);
   Home Assistant does not allow variables in a state trigger.
5. Save. The plate is redrawn whenever the sensor changes (at least every
   30 seconds while there are departures), when the plate comes online and
   every 5 minutes.

## What gets published

Every run publishes plain openHASP commands to `hasp/<plate>/command/<object>.<property>`:

| Object | Property | Value |
|---|---|---|
| `p1b2` | `text` | Stop name, e.g. `Hodžovo nám.` |
| `p1b3` | `text` | Sub-title, e.g. `Bratislava · 42, 44, 47` |
| `p1b{10+4i}` | `text`, `hidden` | Line badge of row *i* (0–4); hidden when there is no departure |
| `p1b{11+4i}` | `text` | Destination, cut to 18 characters |
| `p1b{12+4i}` | `text`, `text_color` | `4min` under 60 minutes, otherwise `HH:MM`; green < 5 min, yellow < 15 min, white otherwise |
| `p1b{13+4i}` | – | Row separator (static) |
| `p1b30` | `text` | `Updated HH:MM` (time of the last data from imhd.sk) |
| `p1b31` | `text`, `text_color` | `LIVE`, `TIMETABLE`, `OFFLINE` or `ALERT` |

So row 0 uses objects 10/11/12, row 1 uses 14/15/16, … row 4 uses 26/27/28.

## Customising

* **Another page:** replace `p1b` in the automation and `"page":1` in
  `pages.jsonl` with your page number.
* **Several stops on several pages:** duplicate the automation, change the
  sensor and the page prefix.
* **Fewer MQTT messages:** openHASP also accepts a JSON array of commands on
  `hasp/<plate>/command/json`. Replace the `repeat` block with a single publish:

  ```yaml
  - action: mqtt.publish
    data:
      topic: "hasp/{{ plate }}/command/json"
      payload: >-
        {{ commands | map('join', '=') | list | to_json }}
  ```

* **Diacritics:** the built-in fonts of current openHASP firmware include the
  Latin-Extended characters used in Slovak (č, ľ, š, ť, ž, ô, …). If your
  custom font shows boxes instead, strip them in the automation, e.g.
  `(d.destination or '') | replace('č', 'c') | replace('š', 's') | …`.
* **Bigger text:** `text_font` in `pages.jsonl` selects the font size (16, 24,
  32 … depending on the fonts on your plate). Keep the direction column at
  16 px if you want 18 characters to fit.
