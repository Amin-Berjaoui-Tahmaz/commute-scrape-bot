"""Mijn NS work-trip pre-selector -- CLI entry point.

Workflow:
  1. Open browser.
  2. Log in and navigate to Journey history & transactions.
  3. Set the desired period and click Show.
  4. Press Enter.
  5. Bot scans and selects the relevant trips.
  6. Bot switches to the declarations view.
  7. Review the declarations and click Download in the browser.
  8. The PDF is automatically saved to ./downloads/.
  9. Change the period in the browser and click Show.
 10. Press Enter again.
 11. Repeat for as many periods as needed.
 12. Press Ctrl+C when finished.

The browser remains open for the entire session.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import load_config
from .ns_dom import (
    JS_HELPERS,
    get_declared_count,
    get_rows_with_dates,
    scroll_to_load_all,
    switch_to_declarations,
    switch_to_all_trips,
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
    """Scroll to load everything, scrape rows, and parse them into Trip
    objects."""

    scroll_to_load_all(page)

    raw_rows = get_rows_with_dates(page)

    trips: list[Trip] = []
    skipped = 0

    for row in raw_rows:
        # Skip the master "select all" checkbox.
        if row.get("id") == "declare-all-checkbox":
            continue

        trip = parse_trip_row(
            row.get("id"),
            row.get("date"),
            row.get("text"),
        )

        if trip is None:
            skipped += 1
        else:
            trips.append(trip)

    if skipped:
        logger.info(
            "%d checkbox rows could not be parsed and were skipped.",
            skipped,
        )

    return trips


# --------------------------------------------------------------------------
# Dry run / information
# --------------------------------------------------------------------------


def print_dry_run(
    work_trips: list[Trip],
    work_stations: list[str],
) -> None:
    """Print what the bot is about to select."""

    by_day: dict[str, list[Trip]] = {}

    for trip in work_trips:
        by_day.setdefault(trip.date, []).append(trip)

    print(
        f"\n[PLAN] {len(work_trips)} trip(s) will be checked "
        f"across {len(by_day)} day(s):\n"
    )

    for date, day_trips in by_day.items():
        print(f"  {date}")

        for trip in sorted(
            day_trips,
            key=lambda t: to_minutes(t.checkin),
        ):
            tag = (
                " *"
                if touches_work(trip, work_stations)
                else "  (connecting leg)"
            )

            print(
                f"    {trip.checkin}-{trip.checkout}  "
                f"{trip.dep_station} -> {trip.dest_station}{tag}"
            )

    print()


# --------------------------------------------------------------------------
# Checkbox interaction
# --------------------------------------------------------------------------


def check_one(page: Page, trip: Trip) -> None:
    """Check one trip unless it is already checked."""

    box = page.locator(
        f'[id="{trip.checkbox_id}"]'
    ).first

    box.scroll_into_view_if_needed()

    time.sleep(0.1)

    if box.is_checked():
        print(
            f"  {trip.checkin} "
            f"{trip.dep_station[:25]:<25} -> "
            f"{trip.dest_station[:25]:<25} | "
            f"already checked"
        )
        return

    before_count = get_declared_count(page)

    # Use the DOM click because these are sometimes wrapped in
    # custom Angular components.
    page.evaluate(
        "el => el.click()",
        box.element_handle(),
    )

    time.sleep(0.3)

    after_checked = box.is_checked()
    after_count = get_declared_count(page)

    logger.debug(
        "checkbox=%s checked=%s declarations=%s->%s",
        trip.checkbox_id,
        after_checked,
        before_count,
        after_count,
    )

    print(
        f"  {trip.checkin} "
        f"{trip.dep_station[:25]:<25} -> "
        f"{trip.dest_station[:25]:<25} | "
        f"checked={after_checked}"
    )


def check_trips(
    page: Page,
    work_trips: list[Trip],
) -> None:
    """Check every selected work trip."""

    print("\n--- checking trips ---")

    for trip in work_trips:
        if trip.checkbox_id:
            check_one(page, trip)


# --------------------------------------------------------------------------
# One period
# --------------------------------------------------------------------------


def run_one_period(
    page: Page,
    work_stations: list[str],
    max_gap_minutes: int,
    download_dir: Path,
) -> bool:
    """Process the currently displayed period.

    Returns True if a download was successfully saved.
    Returns False if there were no matching trips.
    """

    # The page should already be showing the selected period.
    page.wait_for_load_state("networkidle")

    page.wait_for_selector(
        "input[type=checkbox]",
        timeout=15000,
    )

    time.sleep(1)

    # --------------------------------------------------------------
    # Scan
    # --------------------------------------------------------------

    # Always reset the NS filter to "all trips" before scanning.
    # This is necessary when processing multiple months in one browser
    # session because NS can leave the declarations filter active after
    # the previous download.
    switch_to_all_trips(page)
    trips = collect_trips(page)

    print(
        f"\nParsed {len(trips)} trip rows from the page."
    )

    # --------------------------------------------------------------
    # Determine relevant trips
    # --------------------------------------------------------------

    work_trips = select_work_chains(
        trips,
        work_stations,
        max_gap_minutes,
    )

    if not work_trips:
        print(
            "\nNo matching trips/chains found on this page."
        )
        print(
            "Check that the correct period is displayed and "
            "that your work stations match the station names."
        )
        return False

    # --------------------------------------------------------------
    # Show what will happen
    # --------------------------------------------------------------

    print_dry_run(
        work_trips,
        work_stations,
    )

    # NO CONFIRMATION.
    #
    # The user has already chosen to run the bot by pressing Enter.
    # They can then inspect the actual declarations in the browser.

    # --------------------------------------------------------------
    # Select trips
    # --------------------------------------------------------------

    check_trips(
        page,
        work_trips,
    )

    declared_before = get_declared_count(page)

    print(
        f"\nChecked trips."
    )

    print(
        f"On-page declared count: {declared_before}"
    )

    # --------------------------------------------------------------
    # Switch to declarations
    # --------------------------------------------------------------

    declared_count = switch_to_declarations(page)

    print(
        f"\nSwitched to declarations view "
        f"({declared_count} declared)."
    )

    print(
        "\nReview the declarations in the browser."
    )

    print(
        "Click DOWNLOAD when you're satisfied."
    )

    # --------------------------------------------------------------
    # Wait for the browser download
    # --------------------------------------------------------------

    print(
        "\nWaiting for Download..."
    )

    try:
        download = page.wait_for_event(
            "download",
            timeout=10 * 60 * 1000,
        )

    except PlaywrightTimeoutError:
        print(
            "\nTimed out waiting for a download."
        )
        print(
            "The browser is still open."
        )
        return False

    # --------------------------------------------------------------
    # Save the file
    # --------------------------------------------------------------

    filename = download.suggested_filename

    target = download_dir / filename

    download.save_as(target)

    print(
        f"\n✓ Download saved to:"
    )
    print(
        f"  {target}"
    )

    return True


# --------------------------------------------------------------------------
# CLI arguments
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select work-relevant trips on Mijn NS "
            "and automatically save downloads."
        )
    )

    parser.add_argument(
        "--work-station",
        dest="work_stations",
        action="append",
        default=None,
        help=(
            "Station name used to identify work trips. "
            "Repeatable."
        ),
    )

    parser.add_argument(
        "--max-gap",
        dest="max_gap_minutes",
        type=int,
        default=None,
        help=(
            "Maximum gap between journey legs for them "
            "to count as one chain."
        ),
    )

    parser.add_argument(
        "--config",
        dest="config_path",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml.",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )

    return parser.parse_args()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.debug
            else logging.INFO
        ),
        format="%(levelname)s %(name)s: %(message)s",
    )

    # --------------------------------------------------------------
    # Configuration
    # --------------------------------------------------------------

    config = load_config(args.config_path)

    work_stations = (
        args.work_stations
        if args.work_stations is not None
        else config.work_stations
    )

    max_gap_minutes = (
        args.max_gap_minutes
        if args.max_gap_minutes is not None
        else config.max_gap_minutes
    )

    # --------------------------------------------------------------
    # Download directory
    # --------------------------------------------------------------

    download_dir = Path("downloads").resolve()

    download_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------------
    # Browser
    # --------------------------------------------------------------

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
            viewport={
                "width": 1400,
                "height": 900,
            },
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        # Hide webdriver flag.
        context.add_init_script(
            """
            Object.defineProperty(
                navigator,
                'webdriver',
                {
                    get: () => undefined
                }
            );
            """
        )

        # Install our shadow-DOM helpers.
        context.add_init_script(
            JS_HELPERS
        )

        page = context.new_page()

        page.goto(
            "https://www.ns.nl/"
        )

        print(
            f"\nDownloads will be saved to:"
            f"\n  {download_dir}"
        )

        print(
            "\n>>> Log in and navigate to "
            "'Journey history & transactions'."
        )

        print(
            ">>> Select the period you want and click Show."
        )

        print(
            ">>> Press Enter to process the current period."
        )

        # ----------------------------------------------------------
        # MULTI-PERIOD LOOP
        # ----------------------------------------------------------

        try:
            while True:

                # --------------------------------------------------
                # User selects/loads the period in the browser
                # --------------------------------------------------

                input(
                    "\n>>> Press Enter to scan + select: "
                )

                # --------------------------------------------------
                # Process it
                # --------------------------------------------------

                downloaded = run_one_period(
                    page=page,
                    work_stations=work_stations,
                    max_gap_minutes=max_gap_minutes,
                    download_dir=download_dir,
                )

                # --------------------------------------------------
                # Prepare for next period
                # --------------------------------------------------

                if downloaded:
                    print(
                        "\n✓ Finished this period."
                    )

                print(
                    "\n>>> You can now select another period "
                    "in the browser and click Show."
                )

                print(
                    ">>> Then press Enter to process it."
                )

                print(
                    ">>> Press Ctrl+C when you're completely done."
                )

        except KeyboardInterrupt:
            print(
                "\n\nStopping -- closing browser."
            )

        finally:
            browser.close()


if __name__ == "__main__":
    main()
