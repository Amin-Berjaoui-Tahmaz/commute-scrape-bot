"""
Mijn NS work-trip pre-selector.

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
     your WORK_STATIONS.
  5. It shows a DRY RUN of exactly what it intends to check and asks for
     confirmation before touching any checkboxes.
  6. After confirmation, it checks the relevant boxes (skipping any that
     are already checked) and stops -- review/scroll/download is on you.
  7. Change the period to the next month in the browser, come back, and
     press Enter to repeat from step 4. Type 'q' instead of Enter to quit.

Usage:
    python3 ns_select_and_download.py

Requirements:
    pip install playwright --break-system-packages
    playwright install chromium
"""
import re
import time
from dataclasses import dataclass
from itertools import groupby
from typing import Optional

from playwright.sync_api import Page, sync_playwright

WORK_STATIONS = ["Sliedrecht", "Ketelhaven"]

# Max minutes allowed between one leg's checkout and the next leg's checkin
# for them to be considered the same journey (e.g. transfer time between
# metro and bus). Tune this if real data shows longer/shorter transfers.
MAX_TRANSFER_GAP_MINUTES = 20

# Known OV carriers -- used to split destination station from carrier name
# when they're concatenated without whitespace in the row text, e.g.
# "Rotterdam, ZuidpleinR-net€ 6.03" -> dest="Rotterdam, Zuidplein"
CARRIERS_RE = re.compile(r"R-net|RET|NS|HTM|Arriva|Connexxion|Keolis|Syntus|GVB|EBS|Qbuzz")
DATE_RE = (
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}"
)


# --------------------------------------------------------------------------
# Browser-side helpers (pierce shadow DOM). Injected once via
# add_init_script so every page.evaluate() call below can reuse the same
# window.__ns.* functions instead of redefining them each time.
# --------------------------------------------------------------------------

JS_HELPERS = r"""
window.__ns = (() => {
    function deepText(node) {
        let text = '';
        if (node.shadowRoot) text += deepText(node.shadowRoot);
        for (const child of node.childNodes || []) {
            if (child.nodeType === Node.TEXT_NODE) text += child.textContent;
            else if (child.nodeType === Node.ELEMENT_NODE || child.nodeType === Node.DOCUMENT_FRAGMENT_NODE)
                text += deepText(child);
        }
        return text;
    }

    function deepCheckboxCount(node) {
        let count = 0;
        if (node.shadowRoot) count += deepCheckboxCount(node.shadowRoot);
        for (const child of node.childNodes || []) {
            if (child.nodeType === Node.ELEMENT_NODE) {
                if (child.matches && child.matches('input[type=checkbox]')) count += 1;
                count += deepCheckboxCount(child);
            } else if (child.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                count += deepCheckboxCount(child);
            }
        }
        return count;
    }

    // Climb from a checkbox until we hit an ancestor with >=2 timestamps
    // (check-in + check-out) that still contains only this ONE checkbox.
    // Bail out if we hit an ancestor with more checkboxes first -- that
    // means we've climbed past this trip's own row.
    function rowText(cb, maxHops = 8) {
        let el = cb;
        for (let i = 0; i < maxHops && el; i++) {
            el = el.parentElement;
            if (!el) break;
            if (deepCheckboxCount(el) > 1) break;
            const candidate = deepText(el);
            if ((candidate.match(/\d{2}:\d{2}/g) || []).length >= 2) return candidate;
        }
        return null;
    }

    return { deepText, deepCheckboxCount, rowText };
})();
"""

# Walks the whole tree in document order (piercing shadow DOM), collecting
# date headings and checkboxes interleaved so each checkbox can be tagged
# with its nearest preceding date. Date headings are often split across
# sibling elements, so we match on each element's *combined* text and only
# recurse into elements that contain no checkboxes and aren't themselves a
# full date match (avoids duplicate detections from wrapper elements).
SCRAPE_ROWS_JS = (
    r"""
() => {
    const DATE_RE = /"""
    + DATE_RE
    + r"""/;
    const { deepText, deepCheckboxCount, rowText } = window.__ns;

    const items = [];
    function walk(node) {
        if (node.shadowRoot) walk(node.shadowRoot);
        for (const child of node.childNodes || []) {
            if (child.nodeType === Node.ELEMENT_NODE) {
                if (child.matches && child.matches('input[type=checkbox]')) {
                    items.push({ type: 'checkbox', el: child });
                    continue;
                }
                if (deepCheckboxCount(child) === 0) {
                    const m = deepText(child).match(DATE_RE);
                    if (m) { items.push({ type: 'date', text: m[0] }); continue; }
                }
                walk(child);
            } else if (child.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                walk(child);
            }
        }
    }
    walk(document);

    const out = [];
    let lastDate = null;
    for (const item of items) {
        if (item.type === 'date') lastDate = item.text;
        else out.push({ id: item.el.id || null, date: lastDate, text: rowText(item.el) });
    }
    return out;
}
"""
)

DECLARED_COUNT_JS = "() => window.__ns.deepText(document.body)"


def scroll_to_load_all(page: Page, max_rounds: int = 40, pause: float = 0.4) -> int:
    """Mijn NS lazy-loads trips on scroll. Keep scrolling until the number
    of checkboxes on the page stops increasing."""
    prev_count = -1
    for _ in range(max_rounds):
        count = page.locator("input[type=checkbox]").count()
        if count == prev_count:
            break
        prev_count = count
        page.mouse.wheel(0, 3000)
        time.sleep(pause)
    return prev_count


def get_rows_with_dates(page: Page, retries: int = 5) -> list[dict]:
    """Returns [{id, date, text}, ...], one entry per checkbox on the page,
    each with its isolated row text and nearest preceding date heading."""
    for _ in range(retries):
        try:
            return page.evaluate(SCRAPE_ROWS_JS)
        except Exception as e:
            if "context was destroyed" in str(e).lower() or "navigation" in str(e).lower():
                time.sleep(0.5)
                continue
            raise
    return []


def get_declared_count(page: Page) -> Optional[int]:
    """Reads the live 'X declarations' counter off the page."""
    m = re.search(r"(\d+)\s*declarat", page.evaluate(DECLARED_COUNT_JS), re.IGNORECASE)
    return int(m.group(1)) if m else None


def dump_diagnostics(page: Page, path: str = "debug_dump.txt") -> None:
    """Write out everything we can see about checkboxes/rows on the page,
    so a failure can be diagnosed from real data instead of guesswork."""
    info = page.evaluate(
        """
        () => {
            const inputs = Array.from(document.querySelectorAll('input[type=checkbox]'));
            return {
                input_checkbox_count: inputs.length,
                input_checkbox_samples: inputs.slice(0, 5).map(cb => cb.outerHTML),
                all_input_types: Array.from(document.querySelectorAll('input')).map(i => i.type),
                body_has_sliedrecht: document.body.innerText.includes('Sliedrecht'),
                body_has_ketelhaven: document.body.innerText.includes('Ketelhaven'),
            };
        }
        """
    )
    rows = get_rows_with_dates(page)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"input[type=checkbox] count: {info['input_checkbox_count']}\n")
        f.write(f"all <input> types on page: {info['all_input_types']}\n")
        f.write(f"page text contains 'Sliedrecht': {info['body_has_sliedrecht']}\n")
        f.write(f"page text contains 'Ketelhaven': {info['body_has_ketelhaven']}\n\n")
        f.write("--- input[type=checkbox] samples (outerHTML) ---\n")
        for s in info["input_checkbox_samples"]:
            f.write(s + "\n\n")
        f.write("--- rows with dates (first 15) ---\n")
        for i, r in enumerate(rows[:15]):
            f.write(f"[{i}] id={r.get('id')!r} date={r.get('date')!r}\n     text={r.get('text')!r}\n\n")
    page.screenshot(path="debug_screenshot.png", full_page=False)
    print(f"\nWrote diagnostics to {path} and debug_screenshot.png")


# --------------------------------------------------------------------------
# Trip parsing & journey-chain building
# --------------------------------------------------------------------------

@dataclass
class Trip:
    checkbox_id: str
    date: str
    checkin: str
    dep_station: str
    checkout: str
    dest_station: str

    @property
    def touches_work(self) -> bool:
        return any(s in self.dep_station or s in self.dest_station for s in WORK_STATIONS)


def normalize_station(name: str) -> str:
    s = re.sub(r"^Rotterdam,\s*", "", name.strip())
    return re.sub(r"\(.*", "", s).strip().lower()


def stations_match(a: str, b: str) -> bool:
    a, b = normalize_station(a), normalize_station(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_trip_row(checkbox_id: Optional[str], date: Optional[str], text: Optional[str]) -> Optional[Trip]:
    """Pulls (checkin, dep_station, checkout, dest_station) out of a row's
    flattened text, e.g.:
        '08:24Rotterdam, Zuidplein (perron G08:58Sliedrecht, Station BaanhoekR-net€ 4.99 Declarations'
    Splitting on HH:MM timestamps isolates the departure station between
    the two times, and the destination + carrier + price after the second.
    Returns None if the row doesn't look like a parseable single trip.
    """
    if not text or not date:
        return None
    times = re.findall(r"\d{2}:\d{2}", text)
    parts = re.split(r"\d{2}:\d{2}", text)
    if len(times) < 2 or len(parts) < 3:
        return None
    dep_station = parts[1].strip(" \n\t-()")
    dest_raw = re.split(r"€", parts[2])[0]
    dest_station = CARRIERS_RE.split(dest_raw)[0].strip(" \n\t-()")
    if not dep_station or not dest_station:
        return None
    return Trip(checkbox_id, date, times[0], dep_station, times[1], dest_station)


def build_chains(trips: list[Trip], max_gap_minutes: int = MAX_TRANSFER_GAP_MINUTES) -> list[list[Trip]]:
    """Groups each day's trips (sorted chronologically) into journey chains.
    A chain only ever links *adjacent* trips in that sorted order (one
    trip's destination roughly matching the next's departure within the
    transfer window), so a single linear scan is enough -- no union-find
    needed."""
    chains = []
    for _date, day_trips in groupby(sorted(trips, key=lambda t: (t.date, to_minutes(t.checkin))), key=lambda t: t.date):
        current: list[Trip] = []
        for t in day_trips:
            if current:
                prev = current[-1]
                gap = to_minutes(t.checkin) - to_minutes(prev.checkout)
                if not (0 <= gap <= max_gap_minutes and stations_match(prev.dest_station, t.dep_station)):
                    chains.append(current)
                    current = []
            current.append(t)
        if current:
            chains.append(current)
    return chains


def select_work_chains(trips: list[Trip], max_gap_minutes: int = MAX_TRANSFER_GAP_MINUTES) -> list[Trip]:
    """Returns the flat list of Trips belonging to a chain that touches any
    WORK_STATIONS entry on at least one leg."""
    return [
        t
        for chain in build_chains(trips, max_gap_minutes)
        if any(t.touches_work for t in chain)
        for t in chain
    ]


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
        print(
            f"Note: {skipped} checkbox rows could not be parsed (no date/text found) "
            f"and were skipped. If this looks high relative to the total, check "
            f"debug_dump.txt after a run with no matches."
        )
    return trips


# --------------------------------------------------------------------------
# Page interaction: dry run, selecting, switching to declarations, download
# --------------------------------------------------------------------------

def print_dry_run(work_trips: list[Trip]) -> None:
    by_day: dict[str, list[Trip]] = {}
    for t in work_trips:
        by_day.setdefault(t.date, []).append(t)

    print(f"\n[DRY RUN] {len(work_trips)} trip(s) would be checked, across {len(by_day)} day(s):\n")
    for date, day_trips in by_day.items():
        print(f"  {date}")
        for t in sorted(day_trips, key=lambda t: to_minutes(t.checkin)):
            tag = " *" if t.touches_work else "  (connecting leg)"
            print(f"    {t.checkin}-{t.checkout}  {t.dep_station} -> {t.dest_station}{tag}")
    print()


def check_one(page: Page, t: Trip) -> None:
    box = page.locator(f'[id="{t.checkbox_id}"]').first
    box.scroll_into_view_if_needed()
    time.sleep(0.1)

    if box.is_checked():
        print(f"  {t.checkin} {t.dep_station[:25]:<25} -> {t.dest_station[:25]:<25} | already checked, skipped")
        return

    before_count = get_declared_count(page)
    page.evaluate("el => el.click()", box.element_handle())
    time.sleep(0.3)
    after_check, after_count = box.is_checked(), get_declared_count(page)
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

def run_one_period(page: Page) -> None:
    """Scans whatever period is currently shown on the page, and checks
    the boxes for work-relevant trip chains after confirmation. Never
    downloads anything -- that's a manual step on the actual site."""
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("input[type=checkbox]", timeout=15000)
    time.sleep(1)

    trips = collect_trips(page)
    print(f"Parsed {len(trips)} trip rows from the page.")

    work_trips = select_work_chains(trips)
    if not work_trips:
        print("No matching trips/chains found -- dumping diagnostics so we can see what's actually on the page...")
        dump_diagnostics(page)
        return

    print_dry_run(work_trips)
    check_trips(page, work_trips)
    print(f"Done. On-page declared count: {get_declared_count(page)}")
    print("Review/scroll/download on the actual page whenever you're ready.")


def main() -> None:
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
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        context.add_init_script(JS_HELPERS)  # available on every page/navigation
        page = context.new_page()
        page.goto("https://www.ns.nl/")

        input(
            "\n>>> Click 'Log in' yourself, log in, and go to 'Journey history & "
            "transactions'.\n>>> Press Enter here once you're there...\n"
        )

        while True:
            answer = input(
                "\n>>> Set the period you want (e.g. a month) and click Show.\n"
                ">>> Press Enter to scan + pre-select that period, or type 'q' to quit: "
            ).strip().lower()
            if answer == "q":
                break
            run_one_period(page)

        print("\nDone -- closing browser.")


if __name__ == "__main__":
    main()
