# Contributing

Thanks for helping to improve IMHD.sk Departures! Bug reports, fixes, new
examples and translations are all welcome.

## Ground rules

* **Be gentle with imhd.sk.** The integration is unofficial. Changes must not
  add polling or extra connections: one socket.io connection per configured
  stop, HTTP only for stop look-ups and the stop's timetable page (at most
  every 5 minutes per stop, normally about every 2 hours). Tests must never hit
  the real server - use the captured payloads in `tests/fixtures/`.
* **No private data** in code, fixtures, screenshots or examples: no tokens,
  hostnames, IP addresses, home coordinates or your personal stop. Use the
  public example stops: Bratislava *Hodžovo nám.* 83 (platforms A–D),
  Košice *Nám. osloboditeľov* 1130 and Žilina *Hurbanova* 1831 (both with
  unlabeled platforms).
* **Respect imhd.sk's terms.** imhd.sk data may be used for personal purposes
  only. Don't add features meant to republish or redistribute it.
* Keep user-facing texts in `strings.json` and in **both** translations
  (`translations/en.json`, `translations/sk.json`).
* **Stay compatible with the oldest supported Home Assistant**, which is
  `homeassistant` in `hacs.json` (2025.2.0, running on Python 3.13). Don't use
  Python 3.14-only syntax such as `except A, B:` without parentheses. CI
  byte-compiles the integration with Python 3.13.

## Development setup

You need Python 3.14 (the version the Home Assistant release pinned for the
tests requires) and git. The integration itself must also run on Python 3.13,
see the ground rules above.
[uv](https://docs.astral.sh/uv/) is the quickest way:

```bash
git clone https://github.com/jakubfabrici/ha-imhd.git
cd ha-imhd
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r requirements_test.txt
```

Plain `venv` + `pip` works too:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt
```

`requirements_test.txt` pulls in
[pytest-homeassistant-custom-component](https://github.com/MatthieuDartiailh/pytest-homeassistant-custom-component),
which pins a matching Home Assistant version.

## Tests and linting

```bash
pytest tests -q          # unit + integration tests (no network)
ruff check .             # lint (configuration in pyproject.toml)
ruff format .            # optional: format the code
```

CI runs the same commands plus a Python 3.13 byte-compile check
(`.github/workflows/tests.yml`), and
[hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest/) and the
HACS validation (`.github/workflows/validate.yml`).

Tests live in `tests/`; realtime payloads and HTML pages captured from imhd.sk
live in `tests/fixtures/`. When you fix a parsing problem, add the payload that
triggered it as a new fixture (strip anything that is not needed).

## Trying it in Home Assistant

The simplest loop is a throw-away Home Assistant in the same virtualenv:

```bash
mkdir -p config/custom_components
ln -s "$PWD/custom_components/imhd" config/custom_components/imhd
hass -c config            # http://localhost:8123
```

(`config/` is git-ignored.) Enable debug logging for `custom_components.imhd`
to see what the feed sends. Use the examples in `examples/` for dashboards and
templates.

## Adding a city

imhd.sk calls a city a *section*; it is the first part of the URL, e.g.
`https://imhd.sk/ke/...` is Košice (`ke`).

1. Find the section code in the city menu on [imhd.sk](https://imhd.sk/) (the
   links look like `/<code>/mhd`).
2. Check that the online departure board works for a stop in that city:
   `https://imhd.sk/<code>/online-zastavkova-tabula?st=<stop id>`. You can get a
   stop id from
   `https://imhd.sk/<code>/api/cepo?op=GetNearestStop&lat=<lat>&lng=<lng>&longName=1&ss=1`.
3. Add the code and the city name to `SECTIONS` in
   `custom_components/imhd/const.py` (keep the list sorted by code).
4. Add the code to the `city` select options of `find_stops` in
   `custom_components/imhd/services.yaml`, and its label to
   `selector.city.options` in `strings.json`, `translations/en.json` and
   `translations/sk.json`.
5. Add a test (at least the section resolution in the config flow / API tests)
   and, if the city behaves differently, a captured fixture.
6. Mention the city in `README.md` and `CHANGELOG.md`.

## Pull requests

* One topic per pull request; describe what and why.
* Update `CHANGELOG.md` under **Unreleased**.
* Update README / examples when you change options, entities, attributes or
  actions. Users' templates rely on entity ids (`<domain>.<name>_<key>`),
  attribute names and departure fields, so treat them as a public API. The
  documented template outputs are rendered from the example board in the
  README; keep them in sync.
* Make sure `pytest tests -q` and `ruff check .` pass.

## Releases (maintainer)

1. Bump `version` in `custom_components/imhd/manifest.json`.
2. Move the **Unreleased** changelog entries under the new version.
3. Tag `vX.Y.Z` and publish a GitHub release; HACS picks it up from there.
