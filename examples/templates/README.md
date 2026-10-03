# Templating cookbook

Copy-paste [Jinja templates](https://www.home-assistant.io/docs/configuration/templating/)
for IMHD.sk Departures, with the output you should expect. Paste them into
**Developer tools → Template** to experiment, then use them in
[template entities](#using-the-templates), Markdown cards, automations or
notifications.

All examples use a stop named **Hodzovo** (`sensor.hodzovo_departures`, Bratislava,
*Hodžovo nám.*, walking time 3 min). Replace `hodzovo` with the name of your stop.
Ready-made template entities are in [`template_sensors.yaml`](template_sensors.yaml).

## Contents

- [The data you work with](#the-data-you-work-with)
- [Rules of thumb](#rules-of-thumb)
- [Basics](#basics)
- [Filtering departures](#filtering-departures)
- [Formatting](#formatting)
- [Text for people and speakers](#text-for-people-and-speakers)
- [Yes/no questions (for conditions and binary sensors)](#yesno-questions-for-conditions-and-binary-sensors)
- [Using the templates](#using-the-templates)
- [Jinja building blocks used here](#jinja-building-blocks-used-here)

## The data you work with

The main sensor's state is the number of whole minutes until the next departure
(`unknown` when there is none). Its attributes hold everything else (full
reference: [README → The main sensor and its attributes](../../README.md#the-main-sensor-and-its-attributes)):

| Attribute | Example |
|---|---|
| `stop_name`, `stop_id`, `city`, `section` | `Hodžovo nám.`, `83`, `Bratislava`, `ba` |
| `next_line`, `next_destination`, `next_time`, `next_minutes`, `next_delay`, `next_realtime`, `next_platform`, `next_departure` | `44`, `Koliba`, `08:02`, `4`, `1`, `true`, `A`, `2026-10-05T08:02:10+02:00` |
| `lines`, `departure_count`, `info`, `connected`, `last_update`, `platforms` | `["42", "44", "47"]`, `6`, `[]`, `true`, … |
| `departures` | list of departures, see below |

Each item of `departures` is a dict:

```yaml
line: "44"                 # always a string; "►" = unnumbered service trip
destination: Koliba        # headsign, may contain a via stop: "Hrad ► Červený most"
destination_city: Bratislava
departure: "2026-10-05T08:02:10+02:00"   # expected, ISO 8601, to the second
scheduled: "2026-10-05T08:00:45+02:00"   # timetable
time: "08:02"              # expected, local HH:MM, rounded to the nearest minute
scheduled_time: "08:01"
minutes: 4                 # whole minutes until departure (rounded down), >= 0
leave_in: 1                # minutes - walking_time
delay: 1                   # minutes (+ late, - early); null when not vehicle-tracked
realtime: true             # false = timetable only
platform: A                # label, or the platform id where imhd.sk has no labels
vehicle: "6127"            # null when not vehicle-tracked
low_floor: true            # true / false / null (unknown)
air_conditioning: true
stuck: false
text: 4 min                # board text: "*", "<1 min", "4 min", "22:15"; "~" prefix = timetable only
trip_id: -18185607
platform_id: "213"
terminal: Koliba           # terminal stop of the trip
previous_stop: Kozia       # stop the vehicle passed last, or null
stops_away: 1              # stops between the vehicle and your stop, or null
vehicle_type: SOR TNS 12   # vehicle model, or null
```

The outputs below are for this board (it is 07:58, walking time 3 minutes):

| # | Line | Destination | Platform | Scheduled | Expected | Minutes | `leave_in` | Delay | Realtime | Vehicle | Low floor | A/C | `text` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 44 | Koliba | A | 08:01 | 08:02 | 4 | 1 | 1 | yes | 6127, 1 stop away (Kozia) | yes | yes | `4 min` |
| 1 | 42 | Cintorín Vrakuňa | A | 08:04 | 08:04 | 5 | 2 | 0 | yes | 6813, 2 stops away (Sokolská) | yes | yes | `5 min` |
| 2 | 47 | Hrad ► Červený most | B | 08:05 | 08:08 | 10 | 7 | 3 | yes | 6101, 1 stop away (Kollárovo nám.) | yes | no | `10 min` |
| 3 | 44 | Koliba | A | 08:11 | 08:11 | 13 | 10 | – | no | – | – | – | `~13 min` |
| 4 | 42 | Cintorín Vrakuňa | A | 08:19 | 08:19 | 20 | 17 | 0 | yes | 6865, not on its way yet | yes | yes | `20 min` |
| 5 | 44 | Koliba | A | 08:23 | 08:23 | 25 | 22 | – | no | – | – | – | `~25 min` |

## Rules of thumb

- **Guard against missing data.** While Home Assistant starts or the stop is
  unavailable, attributes are missing. Always read the list as
  `state_attr('sensor.hodzovo_departures', 'departures') or []`, and check
  `is none` / `is defined` before using single values.
- **Lines are strings.** Compare with `'44'`, never `44`; `'X13'` and `'N53'`
  are lines too.
- **No `now()` needed for countdowns.** `minutes` and `leave_in` are
  recomputed by the integration every 30 seconds, so a template that uses them
  re-renders on its own. Use `departure` with `now()` only when you need
  second precision.
- **`delay` can be `null`.** Timetable-only departures have no delay (and no
  vehicle, `low_floor`, `previous_stop`, …). Test `d.delay is none` (or use
  `rejectattr('delay', 'none')`) before comparing.
- **`minutes` is rounded down, `time` to the nearest minute.** Like the imhd.sk
  board, `4 min` means 4 to 5 minutes. `departure` has the exact time.
- **Whitespace.** `{%-` and `-%}` remove the whitespace and newlines around a
  tag; use them when the output must be a single line (sensor states are
  limited to 255 characters - put long text into attributes or Markdown cards).

## Basics

### Next departure as a sentence

```jinja
{%- set s = 'sensor.hodzovo_departures' -%}
{%- if state_attr(s, 'next_line') -%}
Line {{ state_attr(s, 'next_line') }} to {{ state_attr(s, 'next_destination') }} leaves at {{ state_attr(s, 'next_time') }} (in {{ states(s) }} min).
{%- else -%}
No departures at the moment.
{%- endif -%}
```

Output: `Line 44 to Koliba leaves at 08:02 (in 4 min).`

### Minutes, with "now" for 0

```jinja
{%- set m = states('sensor.hodzovo_departures') -%}
{%- if m in ['unknown', 'unavailable'] -%} –
{%- elif m | int == 0 -%} now
{%- else -%} {{ m }} min
{%- endif -%}
```

Output: `4 min`

As a reusable macro for every departure:

```jinja
{%- macro eta(m) -%}{{ 'now' if m == 0 else m ~ ' min' }}{%- endmacro -%}
{%- for d in (state_attr('sensor.hodzovo_departures', 'departures') or [])[:3] -%}
{{ d.line }}: {{ eta(d.minutes) }}{{ ', ' if not loop.last }}
{%- endfor -%}
```

Output: `44: 4 min, 42: 5 min, 47: 10 min`

### Exact countdown from the timestamp

```jinja
{%- set dep = state_attr('sensor.hodzovo_departures', 'next_departure') -%}
{{ time_until(as_datetime(dep)) if dep else 'n/a' }}
```

Output: `4 minutes`

### Using a per-departure sensor

`sensor.hodzovo_departure_N` has the minutes as its state and all departure
fields as attributes - handy where you cannot index lists:

```jinja
{{ state_attr('sensor.hodzovo_departure_2', 'line') }} → {{ state_attr('sensor.hodzovo_departure_2', 'destination') }} in {{ states('sensor.hodzovo_departure_2') }} min
```

Output: `42 → Cintorín Vrakuňa in 5 min`

## Filtering departures

`selectattr(attribute, test, value)` keeps the departures for which the test
passes; `rejectattr` drops them; `first` takes the first one (or is undefined
when nothing matched).

### Next three departures of one line

```jinja
{%- set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] -%}
{{ (deps | selectattr('line', 'eq', '44') | map(attribute='time') | list)[:3] | join(', ') }}
```

Output: `08:02, 08:11, 08:23`

### Next departure of any of several lines

```jinja
{%- set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] -%}
{%- set d = deps | selectattr('line', 'in', ['42', '47']) | first -%}
{{ d.line ~ ' at ' ~ d.time if d is defined else 'none' }}
```

Output: `42 at 08:04`

### Departures in one direction

```jinja
{%- set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] -%}
{%- for d in deps | selectattr('destination', 'search', 'vrakuňa', true) -%}
{{ d.line }} at {{ d.time }}{{ ', ' if not loop.last }}
{%- endfor -%}
```

Output: `42 at 08:04, 42 at 08:19`

`search` is a regular-expression test; the final `true` ignores case. To
ignore diacritics as well, set the `direction` option of the stop instead -
that filter is case- and diacritics-insensitive.

### Departures from one platform

```jinja
{{ (state_attr('sensor.hodzovo_departures', 'departures') or [])
   | selectattr('platform', 'eq', 'B') | map(attribute='line') | join(', ') }}
```

Output: `47`

### Grouped by platform

```jinja
{%- for p in (state_attr('sensor.hodzovo_departures', 'departures') or []) | groupby('platform') -%}
Platform {{ p.grouper }}: {% for d in p.list %}{{ d.line }} {{ d.time }}{{ ', ' if not loop.last }}{% endfor %}{{ '\n' if not loop.last }}
{%- endfor -%}
```

Output:

```text
Platform A: 44 08:02, 42 08:04, 44 08:11, 42 08:19, 44 08:23
Platform B: 47 08:08
```

### Next departure per line

```jinja
{%- for g in (state_attr('sensor.hodzovo_departures', 'departures') or []) | groupby('line') -%}
{{ g.grouper }}: {{ (g.list | first).minutes }} min{{ ', ' if not loop.last }}
{%- endfor -%}
```

Output: `42: 5 min, 44: 4 min, 47: 10 min`

### Number of departures in the next 15 minutes

```jinja
{{ (state_attr('sensor.hodzovo_departures', 'departures') or [])
   | selectattr('minutes', 'le', 15) | list | count }}
```

Output: `4`

### First low-floor departure

```jinja
{%- set d = (state_attr('sensor.hodzovo_departures', 'departures') or [])
            | selectattr('low_floor') | first -%}
{{ 'Line ' ~ d.line ~ ' at ' ~ d.time if d is defined else 'No low-floor departure known' }}
```

Output: `Line 44 at 08:02`. `selectattr('low_floor')` keeps only `true`, so
unknown (`null`) vehicles, which include all timetable-only departures, are
skipped.

### Delayed departures

```jinja
{%- set late = (state_attr('sensor.hodzovo_departures', 'departures') or [])
              | rejectattr('delay', 'none') | selectattr('delay', 'gt', 0) | list -%}
{%- for d in late -%}{{ d.line }} +{{ d.delay }}{{ ', ' if not loop.last }}{%- endfor -%}
```

Output: `44 +1, 47 +3`

### All destinations served

```jinja
{{ (state_attr('sensor.hodzovo_departures', 'departures') or [])
   | map(attribute='destination') | unique | sort | join(', ') }}
```

Output: `Cintorín Vrakuňa, Hrad ► Červený most, Koliba`

## Formatting

### Markdown table

```jinja
| Line | Destination | Departs | Live |
|:---:|:---|---:|:---:|
{%- for d in state_attr('sensor.hodzovo_departures', 'departures') or [] %}
| **{{ d.line }}** | {{ d.destination }} | {{ 'now' if d.minutes == 0 else d.minutes ~ ' min' if d.minutes < 60 else d.time }} | {{ '●' if d.realtime else '' }} |
{%- endfor %}
```

Output:

| Line | Destination | Departs | Live |
|:---:|:---|---:|:---:|
| **44** | Koliba | 4 min | ● |
| **42** | Cintorín Vrakuňa | 5 min | ● |
| **47** | Hrad ► Červený most | 10 min | ● |
| **44** | Koliba | 13 min |  |
| **42** | Cintorín Vrakuňa | 20 min | ● |
| **44** | Koliba | 25 min |  |

Note the whitespace control: `{%- for … %}` removes the newline *before*
each row, and the row itself starts on a new line, so every row lands on its
own line without blank lines in between (which would break the table).

### Delay as text

```jinja
{%- set d = state_attr('sensor.hodzovo_departures', 'next_delay') -%}
{%- if d is none -%} timetable only
{%- elif d > 0 -%} {{ d }} min late
{%- elif d < 0 -%} {{ d | abs }} min early
{%- else -%} on time
{%- endif -%}
```

Output: `1 min late`

### Scheduled vs. expected time

```jinja
{%- for d in (state_attr('sensor.hodzovo_departures', 'departures') or [])[:3] -%}
{{ d.line }} {{ d.scheduled_time }}{{ ' → ' ~ d.time if d.time != d.scheduled_time else '' }}{{ '\n' if not loop.last }}
{%- endfor -%}
```

Output:

```text
44 08:01 → 08:02
42 08:04
47 08:05 → 08:08
```

### Realtime marker

```jinja
{%- for d in (state_attr('sensor.hodzovo_departures', 'departures') or [])[:4] -%}
{{ d.line }} {{ d.time }} {{ '●' if d.realtime else '○' }}{{ '\n' if not loop.last }}
{%- endfor -%}
```

Output:

```text
44 08:02 ●
42 08:04 ●
47 08:08 ●
44 08:11 ○
```

### Compact one-liner for small displays

```jinja
{%- set ns = namespace(items=[]) -%}
{%- for d in (state_attr('sensor.hodzovo_departures', 'departures') or [])[:4] -%}
  {%- set ns.items = ns.items + [d.line ~ ' ' ~ (d.minutes ~ "'" if d.minutes < 60 else d.time)] -%}
{%- endfor -%}
{{ ns.items | join(' · ') if ns.items else '-' }}
```

Output: `44 4' · 42 5' · 47 10' · 44 13'`

`namespace` is needed because a plain `{% set %}` inside a loop does not survive
the loop.

### imhd.sk's own countdown text

`text` is what the imhd.sk board shows: `*` (departing now), `<1 min`,
`N min` up to 60 minutes, then the clock time. Timetable-only departures get a
`~` in front (`~13 min`).

```jinja
{%- for d in (state_attr('sensor.hodzovo_departures', 'departures') or [])[:3] -%}
{{ d.line }} {{ d.text }}{{ ' · ' if not loop.last }}
{%- endfor -%}
```

Output: `44 4 min · 42 5 min · 47 10 min`

### Vehicles

```jinja
{%- for d in (state_attr('sensor.hodzovo_departures', 'departures') or []) | selectattr('vehicle') -%}
{{ d.line }}: #{{ d.vehicle }}{{ ' (AC)' if d.air_conditioning else '' }}{{ ', ' if not loop.last }}
{%- endfor -%}
```

Output: `44: #6127 (AC), 42: #6813 (AC), 47: #6101, 42: #6865 (AC)`

### Where is the vehicle?

`previous_stop` is the stop the vehicle has passed last and `stops_away` how
many stops it still has to go. Both are `null` until the vehicle is on its way.

```jinja
{%- set d = (state_attr('sensor.hodzovo_departures', 'departures') or [])
            | selectattr('previous_stop') | first -%}
{%- if d is defined -%}
{{ d.line }} (#{{ d.vehicle }}) is {{ d.stops_away }} stop{{ 's' if d.stops_away != 1 }} away, last seen at {{ d.previous_stop }}.
{%- else -%} No vehicle on its way.
{%- endif -%}
```

Output: `44 (#6127) is 1 stop away, last seen at Kozia.`

### Timetable-only departures

```jinja
{{ (state_attr('sensor.hodzovo_departures', 'departures') or [])
   | rejectattr('realtime') | map(attribute='text') | join(', ') }}
```

Output: `~13 min, ~25 min`

## Text for people and speakers

### When do I have to leave?

```jinja
{%- set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] -%}
{%- if deps -%}
{%- set d = deps[0] -%}
{%- if d.leave_in <= 0 -%} Leave now for the {{ d.line }}!
{%- else -%} Leave in {{ d.leave_in }} min for the {{ d.line }} at {{ d.time }}.
{%- endif -%}
{%- else -%} Nothing to catch right now.
{%- endif -%}
```

Output: `Leave in 1 min for the 44 at 08:02.`

With `walking_time` set, departures you cannot reach are already removed, so
the first departure is always the next one you can catch.

### Slovak sentence (with correct plural)

Slovak uses *1 minútu*, *2-4 minúty*, *5+ minút*:

```jinja
{%- set s = 'sensor.hodzovo_departures' -%}
{%- set m = state_attr(s, 'next_minutes') -%}
{%- if m is none -%} Momentálne nič nepremáva.
{%- else -%}
{%- set jednotka = 'minútu' if m == 1 else 'minúty' if m in [2, 3, 4] else 'minút' -%}
Linka {{ state_attr(s, 'next_line') }} smer {{ state_attr(s, 'next_destination') }} odchádza
{{- ' teraz' if m == 0 else ' o ' ~ m ~ ' ' ~ jednotka }} ({{ state_attr(s, 'next_time') }}).
{%- endif -%}
```

Output: `Linka 44 smer Koliba odchádza o 4 minúty (08:02).`

### TTS-friendly sentence

Full words, no symbols, and singular/plural handled - reads well on any TTS
engine:

```jinja
{%- set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] -%}
{%- macro mins(n) -%}{{ n }} {{ 'minute' if n == 1 else 'minutes' }}{%- endmacro -%}
{%- if deps | count == 0 -%}
There are no departures from {{ state_attr('sensor.hodzovo_departures', 'stop_name') }} right now.
{%- else -%}
{%- set a = deps[0] -%}
The next {{ a.line }} to {{ a.destination }} {{ 'is leaving now' if a.minutes == 0 else 'leaves in ' ~ mins(a.minutes) }}
{%- if a.delay and a.delay > 0 %}, running {{ mins(a.delay) }} late{% endif %}.
{%- if deps | count > 1 %} After that, the {{ deps[1].line }} to {{ deps[1].destination }} in {{ mins(deps[1].minutes) }}.{% endif %}
{%- endif -%}
```

Output: `The next 44 to Koliba leaves in 4 minutes, running 1 minute late. After that, the 42 to Cintorín Vrakuňa in 5 minutes.`

## Yes/no questions (for conditions and binary sensors)

These render `True` or `False` and can be used as template conditions,
template triggers or template binary sensors.

| Question | Template | Output |
|---|---|---|
| Is a 44 coming within 10 minutes? | `{{ (state_attr('sensor.hodzovo_departures', 'departures') or []) \| selectattr('line', 'eq', '44') \| selectattr('minutes', 'le', 10) \| list \| count > 0 }}` | `True` |
| Is anything 3+ minutes late? | `{{ (state_attr('sensor.hodzovo_departures', 'departures') or []) \| rejectattr('delay', 'none') \| selectattr('delay', 'ge', 3) \| list \| count > 0 }}` | `True` |
| Is the next departure live? | `{{ state_attr('sensor.hodzovo_departures', 'next_realtime') == true }}` | `True` |
| Is there a service alert? | `{{ is_state('binary_sensor.hodzovo_disruption', 'on') }}` | `False` |
| Is the data fresh (< 15 min)? | `{{ (now() - as_datetime(state_attr('sensor.hodzovo_departures', 'last_update'))).total_seconds() < 900 }}` | `True` |

A template **trigger** fires when its template changes from false to true - for
example "notify me once when a 44 is 10 minutes away":

```yaml
triggers:
  - trigger: template
    value_template: >
      {{ (state_attr('sensor.hodzovo_departures', 'departures') or [])
         | selectattr('line', 'eq', '44') | selectattr('minutes', 'le', 10)
         | list | count > 0 }}
```

## Using the templates

### Template entities (YAML)

```yaml
# configuration.yaml
template:
  - sensor:
      - name: "Hodzovo next 44"
        unique_id: hodzovo_next_44
        unit_of_measurement: min
        state: >
          {% set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] %}
          {% set d = deps | selectattr('line', 'eq', '44') | first %}
          {{ d.minutes if d is defined else none }}
        availability: >
          {{ (state_attr('sensor.hodzovo_departures', 'departures') or [])
             | selectattr('line', 'eq', '44') | list | count > 0 }}

  - binary_sensor:
      - name: "Hodzovo next departure delayed"
        unique_id: hodzovo_next_departure_delayed
        state: "{{ (state_attr('sensor.hodzovo_departures', 'next_delay') or 0) >= 3 }}"
```

[`template_sensors.yaml`](template_sensors.yaml) contains ten ready-made template
entities (sentence, next departure of a line, departures in one direction,
count in the next 15 minutes, next low-floor, compact board, legacy
`dep1_line`-style attributes, delayed / leave-for-line / live-data binary
sensors). Include it with `template: !include imhd_templates.yaml`.

### Template helpers (UI)

**Settings → Devices & services → Helpers → Create helper → Template →
Template sensor**, paste the template into *State template* and set the unit
(`min`) if the state is a number. The preview shows the result immediately.

### Markdown card

```yaml
type: markdown
content: >
  {%- set deps = state_attr('sensor.hodzovo_departures', 'departures') or [] -%}
  {% for d in deps[:5] -%}
  **{{ d.line }}** {{ d.destination }} – {{ 'now' if d.minutes == 0 else d.minutes ~ ' min' }}<br>
  {% endfor %}
```

See [`../dashboards/`](../dashboards/) for complete cards.

### Notifications and TTS

Any `message:` of a notify or TTS action accepts templates:

```yaml
action: notify.mobile_app_my_phone
data:
  message: >
    {{ state_attr('sensor.hodzovo_departures', 'next_line') }} to
    {{ state_attr('sensor.hodzovo_departures', 'next_destination') }} in
    {{ states('sensor.hodzovo_departures') }} min
```

## Jinja building blocks used here

| Building block | What it does |
|---|---|
| `state_attr(entity, 'attr')` | Read an attribute (`None` if missing). |
| `states(entity)` | Read the state as a string (`'4'`, `'unknown'`). |
| `x or []` | Fall back to an empty list when `x` is `None`. |
| `list[:3]` | First three items. |
| `selectattr('key', 'eq', value)` / `rejectattr(...)` | Keep / drop items by a field (`eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `none`, `search`). |
| `map(attribute='time')` | Take one field of every item. |
| `first` | First item; *undefined* when the list is empty - test with `is defined`. |
| `groupby('platform')` | Group items; each group has `.grouper` and `.list`. |
| `unique`, `sort`, `join(', ')`, `count` | Usual list helpers. |
| `namespace(...)` | A variable you can change inside a loop. |
| `{%- ... -%}` | Trim whitespace around a tag. |
| `time_until(dt)`, `as_datetime(text)` | Human countdown / parse an ISO timestamp. |
