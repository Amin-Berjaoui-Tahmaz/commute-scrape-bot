# ns-tool

Mijn NS work-trip selector. Selects (checks) the checkboxes for your
work-related trips and switches to the declarations filter so you can
review them -- then you click Download yourself, and the resulting file
is saved automatically to `./downloads/` with its real filename. Never
logs you in, and never clicks Download for you -- that's deliberately
left as a human review step.

## Setup

```bash
git clone https://github.com/Amin-Berjaoui-Tahmaz/commute-scrape-bot.git
cd commute-scrape-bot
uv sync --extra dev
uv run playwright install chromium   # one-time, downloads the browser binary
uv run -m ns_tool.cli
```

First run with no `config.yaml` walks you through a couple of prompts
(work station names, transfer gap) and saves your answers.

To change your stations later, edit `config.yaml` directly (or delete it
to be asked again):

```yaml
work_stations:
  - Sliedrecht
  - Ketelhaven
max_gap_minutes: 20
```

## Running it

```bash
uv run -m ns_tool.cli
uv run -m ns_tool.cli --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
```

(or `ns-select` in place of `uv run -m ns_tool.cli`, if you installed via `uv tool install`)

1. In the browser, log in and go to "Journey history & transactions", set
   the period you want, and click Show.
2. Click "Scan + select trips" in the overlay button (bottom-right of the
   page) -- or press Enter in the terminal, whichever's easier.
3. The script scans the page, chains same-day trips together (see below),
   checks the boxes for any chain touching a work station, and switches
   the bottom bar to "X declarations".
4. Review on the page, then click Download yourself whenever you're
   ready -- it's saved to `./downloads/` automatically.
5. Change the period in the browser, click/press again to repeat. Close
   the browser or press Ctrl+C when done.

## How chain matching works

A real commute is often several legs back to back. The tool links
same-day legs into a chain when one leg's arrival station roughly
matches the next leg's departure station within `max_gap_minutes`, then
selects the *whole* chain the moment *any* leg touches a work station --
walking backward to also pick up earlier legs that don't mention a work
station at all:

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

See `select_work_chains` in `ns_tool/trip_model.py` for the implementation.
