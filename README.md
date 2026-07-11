# ns-tool

Mijn NS work-trip pre-selector. Selects (checks) the checkboxes for your
work-related trips so you can review and download the declaration yourself
on the actual NS site. Never logs in and never clicks Download for you.

## Layout

```
ns_tool/
  ns_dom.py      Playwright page interaction: shadow-DOM JS helpers,
                 scrolling, scraping. Needs a browser.
  trip_model.py  Pure Python: Trip, parsing, station matching, chain
                 building. No Playwright dependency -- this is where most
                 unit tests live.
  config.py      config.yaml loading (work stations, max gap).
  cli.py         argparse + the interactive loop (entry point).
tests/
  test_trip_model.py   Unit tests for trip_model.py.
  test_config.py       Unit tests for config.py.
config.example.yaml    Template config -- copy to config.yaml and edit,
                        see "Configuring your stations" below.
```

## Install

```bash
uv sync --extra dev
uv run playwright install chromium
```

## Configuring your stations

Everything configurable lives in one file: `config.yaml`, in this folder.

```yaml
work_stations:
  - Your Work Station
  - Your Other Work Station
max_gap_minutes: 20
```

## Run

```bash
uv run -m ns_tool.cli
uv run -m ns_tool.cli --work-station "Amsterdam Zuid" --work-station "Utrecht Centraal" --max-gap 15
uv run -m ns_tool.cli --debug   # verbose internal diagnostics on stderr
```

Workflow, once per period/month:

1. Press Enter in the terminal.
2. In the browser, set "Journey history & transactions" to the period you
   want and click Show.
3. Press Enter again.
4. The script scans the page, groups same-day trips into journey chains
   (e.g. metro -> bus -> work) when one trip's destination roughly matches
   the next trip's departure within the configured gap, and marks a whole
   chain as work-relevant if any leg mentions a configured work station.
5. It shows a dry run of what it intends to check.
6. It checks the relevant boxes (skipping any already checked) and stops --
   review/scroll/download is on you.
7. Change the period in the browser, come back, press Enter to repeat.
   Type `q` to quit.

If a scan fails partway (timeout, unexpected navigation), the error is
logged and you're returned to the prompt to retry the same period -- one
bad scan no longer kills the session.

## How chain matching works

Trips are scraped one leg at a time -- but a real commute is often
several legs back to back. The tool links same-day legs into a chain
when one leg's arrival station roughly matches the next leg's departure
station within `max_gap_minutes`, then selects the *whole* chain the
moment *any* leg touches a configured work station -- walking backward
to also pick up the earlier legs that don't mention a work station at
all:

```
Trip rows scraped separately:
[Home -> Transfer] 08:00-08:20
       │
       │ transfer station matches
       ▼
[Transfer -> Work] 08:25-08:50

Chain formed:
Home -> Transfer -> Work
```
**If the last leg matches a work station, then the whole chain is selected together.**


## Dev

```bash
uv run mypy ns_tool tests
uv run pytest
```
