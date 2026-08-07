"""Mijn NS work-trip pre-selector -- CLI entry point.

WHAT THIS DOES NOT DO: log you in, or click Download. You handle login +
2FA yourself, and you always review and click Download yourself -- the
selection is automated, the actual download is deliberately left to a
human, but the resulting file is still saved automatically for you.

WORKFLOW (repeatable, once per period/month):
  1. In the browser, log in, go to "Journey history & transactions", set
     it to the period you want (e.g. January), and click Show.
  2. Back in the terminal, press Enter.
  3. The script scans the page, groups same-day trips into "journey
     chains" (e.g. metro -> bus -> work) when one trip's destination
     roughly matches the next trip's departure within a short time gap,
     and marks a whole chain as work-relevant if ANY leg touches one of
     your work stations.
  4. It prints exactly what it's about to check (a DRY RUN summary) and
     then immediately checks the relevant boxes (skipping any that are
     already checked) and switches the bottom bar to "X declarations", so
     you can see exactly what's about to be downloaded.
  5. Review on the page, then click Download yourself whenever you're
     ready -- it's automatically saved to ./downloads/ with its real
     filename, regardless of when or how long you take to click it.
  6. Change the period to the next month in the browser, come back, and
     press Enter to repeat from step 3. Type 'q' instead of Enter to quit.
  If a period scan fails partway (timeout, unexpected navigation, etc.),
  the error is logged and you're returned to the prompt to retry that
  same period -- one bad scan no longer kills the whole session.

Usage:
    python3 -m ns_tool.cli
    python3 -m ns_tool.cli --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
    python3 -m ns_tool.cli --debug   # verbose internal diagnostics on stderr

Work stations and the transfer-gap window can also be set once in
config.yaml (copy config.example.yaml to get started) instead of typing
CLI flags every time -- handy if multiple people share this tool and each
commute to different stations. CLI flags override the file when given.
"""

from __future__ import annotations

import argparse
import logging
import queue
import time
from pathlib import Path

from playwright.sync_api import Download, Page, sync_playwright

from .config import load_config
from .downloads import build_download_target, save_download_to_path
from .ns_dom import (
    JS_HELPERS,
    get_declared_count,
    get_rows_with_dates,
    scroll_to_load_all,
    switch_to_declarations,
)
from .trip_model import (
    Trip,
    parse_trip_row,
    select_work_chains,
    to_minutes,
    touches_work,
)

logger = logging.getLogger(__name__)


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
            "were skipped. If this looks high relative to the total, the "
            "row markup on the page may have changed.",
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


def run_one_period(
    page: Page, work_stations: list[str], max_gap_minutes: int, download_dir: Path
) -> None:
    """Scans whatever period is currently shown on the page, checks the
    boxes for work-relevant trip chains after confirmation, and switches
    to the declarations filter so the person can review and click
    Download themselves -- the actual download is deliberately not
    automated, and is instead caught by a download listener registered
    on the browser context (see main())."""
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("input[type=checkbox]", timeout=15000)
    time.sleep(1)

    trips = collect_trips(page)
    print(f"Parsed {len(trips)} trip rows from the page.")

    work_trips = select_work_chains(trips, work_stations, max_gap_minutes)
    if not work_trips:
        print(
            "No matching trips/chains found on this page -- check the period "
            "is showing and your work_stations (config.yaml or --work-station) "
            "match the station names on the page."
        )
        return

    print_dry_run(work_trips, work_stations)
    check_trips(page, work_trips)
    print(f"Checked trips. On-page declared count: {get_declared_count(page)}")

    declared_count = switch_to_declarations(page)
    print(
        f"Switched to the declarations view ({declared_count} declared). "
        f"Review on the page, then click Download when ready -- it'll be "
        f"saved automatically to {download_dir}."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select work-relevant trips on Mijn NS; auto-saves whatever you download.",
    )
    parser.add_argument(
        "--work-station",
        dest="work_stations",
        action="append",
        default=None,
        help=(
            "A station name (substring match) that marks a trip as work-related. "
            "Repeatable. Overrides config.yaml if given."
        ),
    )
    parser.add_argument(
        "--max-gap",
        dest="max_gap_minutes",
        type=int,
        default=None,
        help=(
            "Max minutes between legs to still count as one journey chain. "
            "Overrides config.yaml if given."
        ),
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the config file (default: %(default)s).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log internal diagnostics (scroll/retry/DOM detail) to stderr.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config_path)
    work_stations = args.work_stations if args.work_stations is not None else config.work_stations
    max_gap_minutes = (
        args.max_gap_minutes if args.max_gap_minutes is not None else config.max_gap_minutes
    )
    logger.debug("work_stations=%s max_gap_minutes=%s", work_stations, max_gap_minutes)

    download_dir = Path("downloads").resolve()
    download_dir.mkdir(exist_ok=True)

    # Playwright's "download" event fires on its own dispatcher greenlet.
    # Calling another sync Playwright method (save_as) from inside that
    # handler stalls until the main thread makes its own next Playwright
    # call -- which means it may never happen at all if you quit ('q')
    # right after clicking Download, silently losing the file. So the
    # handler only queues the Download (no Playwright calls); the actual
    # save happens on the main thread, right after input() returns.
    pending_downloads: queue.Queue[Download] = queue.Queue()

    def queue_download(download: Download) -> None:
        pending_downloads.put(download)

    def process_pending_downloads() -> None:
        while not pending_downloads.empty():
            download = pending_downloads.get()
            target = build_download_target(
                download_dir, download.suggested_filename, download.url
            )
            saved_path = save_download_to_path(download, target)
            print(f"\nDownload saved to: {saved_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=PrintPreviewUI",
                "--disable-pdf-viewer-app",
                "--disable-extensions",
                "--disable-plugins-discovery",
                "--allow-file-access-from-files",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.on("download", queue_download)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        context.add_init_script(JS_HELPERS)  # available on every page/navigation
        page = context.new_page()
        page.on("download", queue_download)
        page.goto("https://www.ns.nl/")

        print(f"Downloads will be saved to: {download_dir}")

        first_prompt = (
            "\n>>> Click 'Log in', log in, and go to 'Journey history & "
            "transactions'. Set the period you want and click Show.\n"
            ">>> Press Enter here once you're ready to scan + select, or 'q' to quit: "
        )
        later_prompt = "\nPress Enter to scan + select, or 'q' to quit: "
        prompt = first_prompt

        while True:
            answer = input(prompt).strip().lower()
            prompt = later_prompt
            # Flush before checking for quit -- otherwise a download clicked
            # just before typing 'q' could be dropped when the browser closes.
            process_pending_downloads()
            if answer == "q":
                break
            try:
                run_one_period(page, work_stations, max_gap_minutes, download_dir)
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
