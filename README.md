# ns-tool

Mijn NS work-trip selector. Selects (checks) the checkboxes for your
work-related trips and switches to the declarations filter so you can
review them -- then you click Download yourself, and the resulting file
is saved automatically to `./downloads/` with its real filename. Never
logs you in, and never clicks Download for you -- that's deliberately
left as a human review step.

## Quick start

```bash
uv tool install git+https://github.com/Amin-Berjaoui-Tahmaz/commute-scrape-bot.git
playwright install chromium   # one-time, downloads the browser binary
ns-select
```

First run with no `config.yaml` walks you through a couple of prompts
(work station names, transfer gap) and saves your answers -- no file
editing needed. See [Install](#install) below if you'd rather work from
a clone, or [Configuring your stations](#configuring-your-stations) to
edit the file by hand.

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

For development (editable clone, tests, mypy):

```bash
uv sync --extra dev
uv run playwright install chromium
```

For just running it -- no clone needed (see Quick start above):

```bash
uv tool install git+https://github.com/Amin-Berjaoui-Tahmaz/commute-scrape-bot.git
playwright install chromium
```

This works because `pyproject.toml` already declares an `ns-select`
console script (`[project.scripts]`) -- `uv tool install` reads that and
puts `ns-select` on your PATH in its own isolated environment, same as
`pipx`. To pick up a new commit later, rerun the same command with
`--reinstall`.

## Configuring your stations

Everything configurable lives in one file: `config.yaml`, in this folder
(or wherever you run `ns-select`/`uv run -m ns_tool.cli` from).

**First run:** if `config.yaml` doesn't exist yet, the tool asks for your
work station(s) and transfer gap interactively and writes the file for
you -- no editing required.

**By hand:** copy the template and edit it yourself if you prefer, or to
change it later:

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
If it doesn't exist and you're not at an interactive terminal (e.g. a
script or CI), built-in defaults are used instead (Sliedrecht/Ketelhaven,
20 min). CLI flags always override the file, for one-off runs without
editing it.

## Run

```bash
uv run -m ns_tool.cli   # from a clone
ns-select                # if installed via `uv tool install`
ns-select --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
ns-select --debug         # verbose internal diagnostics on stderr
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
