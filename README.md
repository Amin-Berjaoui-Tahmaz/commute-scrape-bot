# ns-tool

Mijn NS work-trip selector. Selects (checks) the checkboxes for your
work-related trips and switches to the declarations filter so you can
review them -- then you click Download yourself, and the resulting file
is saved automatically to `./downloads/` with its real filename. Never
logs you in, and never clicks Download for you -- that's deliberately
left as a human review step.

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
Copy the template and edit it:

```bash
cp config.example.yaml config.yaml
```

```yaml
work_stations:
  - Sliedrecht
  - Ketelhaven
max_gap_minutes: 20
```

That's it -- no other files, no environment variables. `config.yaml` is
gitignored, so each user keeps their own copy with their own stations.
If it doesn't exist, built-in defaults are used (Sliedrecht/Ketelhaven,
20 min) -- handy for a shared office where most people commute to the
same stations. CLI flags always override the file, for one-off runs
without editing it.

## Run

```bash
uv run -m ns_tool.cli
uv run -m ns_tool.cli --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
uv run -m ns_tool.cli --debug   # verbose internal diagnostics on stderr
```

Workflow, once per period/month:

1. In the browser, log in and go to "Journey history & transactions", set
   it to the period you want, and click Show.
2. Back in the terminal, press Enter.
3. The script scans the page, groups same-day trips into journey chains
   (e.g. metro -> bus -> work) when one trip's destination roughly matches
   the next trip's departure within the configured gap, and marks a whole
   chain as work-relevant if any leg mentions a configured work station.
4. It shows a dry run of what it intends to check and asks for confirmation
   (y/N) before touching anything.
5. After confirmation: checks the relevant boxes (skipping any already
   checked) and switches the bottom bar to "X declarations".
6. Review on the page, then click Download yourself whenever you're
   ready -- it's automatically saved to `./downloads/` with its real
   filename, no matter when you click it.
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
moment *any* leg touches a work station -- walking backward to also pick
up the earlier legs that don't mention "Sliedrecht" or "Ketelhaven" at
all:

```
   Home              Hub            Sliedrecht
     o───────────────►o───────────────►o
     │  08:00-08:20   │  08:25-08:50   │
     └── leg 1 ───────┘── leg 2 ───────┘
                                        ▲
                             matches a work_station
                                        │
     ◄──────────── walk back ───────────
     leg 1 gets selected too, even though
     "Home -> Hub" never mentions work at all
```

So a single work trip never shows up half-checked just because only
its last leg happens to say "Sliedrecht". See `build_chains` /
`select_work_chains` in `trip_model.py` for the actual implementation.

## Dev

```bash
uv run mypy ns_tool tests
uv run pytest
```
