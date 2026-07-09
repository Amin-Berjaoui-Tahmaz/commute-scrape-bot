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
                 building. No Playwright dependency -- this is where the
                 unit tests live.
  cli.py         argparse + the interactive loop (entry point).
tests/
  test_trip_model.py   Unit tests for trip_model.py.
```

## Install

```bash
uv sync --extra dev
uv run playwright install chromium
```

## Run

```bash
uv run -m ns_tool.cli
uv run -m ns_tool.cli --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
uv run -m ns_tool.cli --debug   # verbose internal diagnostics on stderr
```

Workflow, once per period/month:

1. Press Enter in the terminal.
2. In the browser, set "Journey history & transactions" to the period you
   want and click Show.
3. Press Enter again.
4. The script scans the page, groups same-day trips into journey chains
   (e.g. metro -> bus -> work) when one trip's destination roughly matches
   the next trip's departure within `--max-gap` minutes, and marks a whole
   chain as work-relevant if any leg mentions a `--work-station`.
5. It shows a dry run of what it intends to check.
6. It checks the relevant boxes (skipping any already checked) and stops --
   review/scroll/download is on you.
7. Change the period in the browser, come back, press Enter to repeat.
   Type `q` to quit.

If a scan fails partway (timeout, unexpected navigation), the error is
logged and you're returned to the prompt to retry the same period -- one
bad scan no longer kills the session.

## Dev

```bash
uv run mypy ns_tool tests
uv run pytest
```
