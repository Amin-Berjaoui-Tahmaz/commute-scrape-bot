"""Mijn NS work-trip pre-selector -- CLI entry point.

This is a helper, not a full automation: it never downloads anything.
It selects (checks) the checkboxes for your work-related trips so you can
review and download the declaration yourself on the actual NS site.

WHAT THIS DOES NOT DO: log you in, or click Download. You handle login +
2FA yourself, and you always press Download (if you want to) manually.

WORKFLOW (repeatable, once per period/month):
  1. On the terminal, press Enter.
  2. In the browser, set "Journey history & transactions" to the period
     you want (e.g. January) and click Show.
  3. Back in the terminal, press Enter again.
  4. The script scans the page, groups same-day trips into "journey
     chains" (e.g. metro -> bus -> work) when one trip's destination
     roughly matches the next trip's departure within a short time gap,
     and marks a whole chain as work-relevant if ANY leg touches one of
     your work stations.
  5. It shows a DRY RUN of exactly what it intends to check and asks for
     confirmation before touching any checkboxes.
  6. After confirmation, it checks the relevant boxes (skipping any that
     are already checked) and stops -- review/scroll/download is on you.
  7. Change the period to the next month in the browser, come back, and
     press Enter to repeat from step 4. Type 'q' instead of Enter to quit.
  If a period scan fails partway (timeout, unexpected navigation, etc.),
  the error is logged and you're returned to the prompt to retry that
  same period -- one bad scan no longer kills the whole session.

Usage:
    python3 -m ns_tool.cli
    python3 -m ns_tool.cli --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
    python3 -m ns_tool.cli --debug   # verbose internal diagnostics on stderr
"""

from __future__ import annotations

import argparse
import logging
import time

from playwright.sync_api import Page, sync_playwright

from .ns_dom import (
    JS_HELPERS,
    dump_diagnostics,
    get_declared_count,
    get_rows_with_dates,
    scroll_to_load_all,
)
from .trip_model import (
    DEFAULT_MAX_TRANSFER_GAP_MINUTES,
    Trip,
    build_chains,
    parse_trip_row,
    select_work_chains,
    to_minutes,
    touches_work,
)

logger = logging.getLogger(__name__)

DEFAULT_WORK_STATIONS = ["Sliedrecht", "Ketelhaven"]


# --------------------------------------------------------------------------
# Scraping -> Trip objects
# --------------------------------------------------------------------------


def collect_trips(page: Page) -> list[Trip]:
    """Scrolls to load everything, scrapes rows, and parses them into Trip
    objects (skipping the master 'select all' checkbox and any row that
    couldn't be parsed)."""
    scroll_to_load_all(page)
    raw_rows = get_rows_with_dates(page)

    trips, skipped = [], 0
    for row in raw_rows:
        if row.get("id") == "declare-all-checkbox":
            continue
        trip = parse_trip_row(row.get("id"), row.get("date"), row.get("text"))
        if trip is None:
            skipped += 1
        else:
            trips.append(trip)

    if skipped:
        # User-relevant (affects trust in the results) but not an error --
        # info level, not a print, so it doesn't clutter interactive output
        # when things are working fine.
        logger.info(
            "%d checkbox rows could not be parsed (no date/text found) and "
            "were skipped. If this looks high relative to the total, check "
            "debug_dump.txt after a run with no matches.",
            skipped,
        )
    return trips


# --------------------------------------------------------------------------
# Page interaction: dry run, selecting
# --------------------------------------------------------------------------


def print_dry_run(work_trips: list[Trip], work_stations: list[str]) -> None:
    by_day: dict[str, list[Trip]] = {}
    for t in work_trips:
        by_day.setdefault(t.date, []).append(t)

    print(
        f"\n[DRY RUN] {len(work_trips)} trip(s) would be checked, across {len(by_day)} day(s):\n"
    )
    for date, day_trips in by_day.items():
        print(f"  {date}")
        for t in sorted(day_trips, key=lambda t: to_minutes(t.checkin)):
            tag = " *" if touches_work(t, work_stations) else "  (connecting leg)"
            print(
                f"    {t.checkin}-{t.checkout}  {t.dep_station} -> {t.dest_station}{tag}"
            )
    print()


def check_one(page: Page, t: Trip) -> None:
    box = page.locator(f'[id="{t.checkbox_id}"]').first
    box.scroll_into_view_if_needed()
    time.sleep(0.1)

    if box.is_checked():
        print(
            f"  {t.checkin} {t.dep_station[:25]:<25} -> {t.dest_station[:25]:<25} | already checked, skipped"
        )
        return

    before_count = get_declared_count(page)
    page.evaluate("el => el.click()", box.element_handle())
    time.sleep(0.3)
    after_check, after_count = box.is_checked(), get_declared_count(page)
    logger.debug(
        "checkbox %s dom_checked=%s declared_before=%s declared_after=%s",
        t.checkbox_id,
        after_check,
        before_count,
        after_count,
    )
    print(
        f"  {t.checkin} {t.dep_station[:25]:<25} -> {t.dest_station[:25]:<25} | "
        f"dom: F->{str(after_check)[0]}  declarations: {before_count}->{after_count}"
    )


def check_trips(page: Page, work_trips: list[Trip]) -> None:
    print("\n--- checking trips ---")
    for t in work_trips:
        if t.checkbox_id:
            check_one(page, t)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def run_one_period(page: Page, work_stations: list[str], max_gap_minutes: int) -> None:
    """Scans whatever period is currently shown on the page, and checks
    the boxes for work-relevant trip chains after confirmation. Never
    downloads anything -- that's a manual step on the actual site."""
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("input[type=checkbox]", timeout=15000)
    time.sleep(1)

    trips = collect_trips(page)
    print(f"Parsed {len(trips)} trip rows from the page.")

    work_trips = select_work_chains(trips, work_stations, max_gap_minutes)
    if not work_trips:
        print(
            "No matching trips/chains found -- dumping diagnostics so we can see what's actually on the page..."
        )
        dump_diagnostics(page)
        return

    print_dry_run(work_trips, work_stations)
    check_trips(page, work_trips)
    print(f"Done. On-page declared count: {get_declared_count(page)}")
    print("Review/scroll/download on the actual page whenever you're ready.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-select work-relevant trips on Mijn NS for manual review/download.",
    )
    parser.add_argument(
        "--work-station",
        dest="work_stations",
        action="append",
        default=None,
        help=(
            "A station name (substring match) that marks a trip as work-related. "
            f"Repeatable. Default: {DEFAULT_WORK_STATIONS}"
        ),
    )
    parser.add_argument(
        "--max-gap",
        dest="max_gap_minutes",
        type=int,
        default=DEFAULT_MAX_TRANSFER_GAP_MINUTES,
        help="Max minutes between legs to still count as one journey chain (default: %(default)s).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log internal diagnostics (scroll/retry/DOM detail) to stderr.",
    )
    args = parser.parse_args()
    if not args.work_stations:
        args.work_stations = DEFAULT_WORK_STATIONS
    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        context.add_init_script(JS_HELPERS)  # available on every page/navigation
        page = context.new_page()
        page.goto("https://www.ns.nl/")

        input(
            "\n>>> Click 'Log in' yourself, log in, and go to 'Journey history & "
            "transactions'.\n>>> Press Enter here once you're there...\n"
        )

        while True:
            answer = (
                input("\nPress Enter to scan + select, or 'q' to quit: ")
                .strip()
                .lower()
            )
            if answer == "q":
                break
            try:
                run_one_period(page, args.work_stations, args.max_gap_minutes)
            except Exception:
                # A selector timeout or mid-scan navigation used to crash the
                # whole session and lose everything already checked. Now we
                # log it and hand control back to the prompt so the same
                # period can just be retried.
                logger.exception(
                    "Scan of this period failed -- the page state should be "
                    "unaffected. You can retry (press Enter again), fix the "
                    "period in the browser, or 'q' to quit."
                )

        print("\nDone -- closing browser.")


if __name__ == "__main__":
    main()
