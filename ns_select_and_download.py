"""
Mijn NS work-trip selector & PDF downloader.

WHAT THIS DOES NOT DO: log you in. You handle login + 2FA + navigating to
"Journey history & transactions" yourself, with the period already set to
the month you want. Once you're looking at that page with all trips
loaded, come back to the terminal and press Enter.

WHAT THIS DOES:
  1. Scrolls to make sure every transaction is loaded.
  2. Reads every trip row (departure/destination stations, times, date).
  3. Groups same-day trips into "journey chains" when one trip's
     destination station roughly matches the next trip's departure
     station within a short time gap (e.g. metro -> bus -> work).
  4. Marks a whole chain as work-relevant if ANY leg in it touches one
     of your WORK_STATIONS -- so connecting legs that never mention the
     work station by name still get included.
  5. Shows you a DRY RUN of exactly what it intends to check, grouped by
     day, and asks for confirmation before touching any checkboxes.
  6. After confirmation: checks the boxes, switches the bottom bar to
     "X declarations", and clicks Download -- saving the PDF to
     ./downloads/.

Usage:
    python3 ns_select_and_download.py

Requirements:
    pip install playwright --break-system-packages
    playwright install chromium
"""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from playwright.sync_api import sync_playwright

WORK_STATIONS = ["Sliedrecht", "Ketelhaven"]
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Max minutes allowed between one leg's checkout and the next leg's checkin
# for them to be considered the same journey (e.g. transfer time between
# metro and bus). Tune this if real data shows longer/shorter transfers.
MAX_TRANSFER_GAP_MINUTES = 20


# --------------------------------------------------------------------------
# Browser-side scraping (pierces shadow DOM)
# --------------------------------------------------------------------------

ROWS_WITH_DATES_JS = r"""
() => {
    function deepText(node) {
        let text = '';
        if (node.shadowRoot) text += deepText(node.shadowRoot);
        const children = node.childNodes || [];
        for (const child of children) {
            if (child.nodeType === Node.TEXT_NODE) text += child.textContent;
            else if (child.nodeType === Node.ELEMENT_NODE || child.nodeType === Node.DOCUMENT_FRAGMENT_NODE)
                text += deepText(child);
        }
        return text;
    }

    function deepCheckboxCount(node) {
        let count = 0;
        if (node.shadowRoot) count += deepCheckboxCount(node.shadowRoot);
        const children = node.childNodes || [];
        for (const child of children) {
            if (child.nodeType === Node.ELEMENT_NODE) {
                if (child.matches && child.matches('input[type=checkbox]')) count += 1;
                count += deepCheckboxCount(child);
            } else if (child.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                count += deepCheckboxCount(child);
            }
        }
        return count;
    }

    function rowTextFor(cb) {
        // Climb ancestors looking for a container that has >= 2 timestamps
        // (check-in + check-out) AND still only contains this ONE checkbox.
        // If we hit an ancestor with more than one checkbox first, we've
        // climbed past this trip's own row into a multi-trip container --
        // stop and report failure rather than merging rows together.
        const MAX_HOPS = 8;
        let el = cb, text = null;
        for (let i = 0; i < MAX_HOPS && el; i++) {
            el = el.parentElement;
            if (!el) break;
            if (deepCheckboxCount(el) > 1) break;
            const candidate = deepText(el);
            const timeMatches = candidate.match(/\d{2}:\d{2}/g) || [];
            if (timeMatches.length >= 2) { text = candidate; break; }
        }
        return text;
    }

    const DATE_RE = /(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}/;

    // Walk the whole tree in document order (piercing shadow DOM),
    // collecting date headings and checkboxes interleaved so we can
    // attach "nearest preceding date" to each checkbox.
    //
    // NOTE: date headings are often split across sibling elements (e.g.
    // a bold "Monday" span next to a separate "June 22, 2026" span), so
    // a single TEXT_NODE never contains the full date string. Instead,
    // for each ELEMENT we check whether its FULL combined text (deepText,
    // which concatenates across all descendant text nodes) matches the
    // date pattern AND it contains no checkboxes -- that marks it as a
    // heading container. Once matched, we don't recurse further into it
    // (the date has been fully captured), avoiding duplicate detections
    // from nested wrapper elements.
    const items = [];
    function walk(node) {
        if (node.shadowRoot) walk(node.shadowRoot);
        const children = node.childNodes || [];
        for (const child of children) {
            if (child.nodeType === Node.ELEMENT_NODE) {
                if (child.matches && child.matches('input[type=checkbox]')) {
                    items.push({ type: 'checkbox', el: child });
                    continue;
                }
                if (deepCheckboxCount(child) === 0) {
                    const combined = deepText(child);
                    const m = combined.match(DATE_RE);
                    if (m) {
                        items.push({ type: 'date', text: m[0] });
                        continue; // don't recurse -- already captured whole
                    }
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
        if (item.type === 'date') {
            lastDate = item.text;
        } else {
            out.push({
                id: item.el.id || null,
                date: lastDate,
                text: rowTextFor(item.el),
            });
        }
    }
    return out;
}
"""


def scroll_to_load_all(page, max_rounds=40, pause=0.4):
    """Mijn NS appears to lazy-load trips on scroll. Keep scrolling until
    the number of checkboxes on the page stops increasing."""
    prev_count = -1
    for _ in range(max_rounds):
        count = page.locator("input[type=checkbox]").count()
        if count == prev_count:
            break
        prev_count = count
        page.mouse.wheel(0, 3000)
        time.sleep(pause)
    return prev_count


def get_rows_with_dates(page, retries=5):
    """Returns [{id, date, text}, ...], one entry per checkbox on the page,
    each with its isolated row text and nearest preceding date heading."""
    for attempt in range(retries):
        try:
            return page.evaluate(ROWS_WITH_DATES_JS)
        except Exception as e:
            if (
                "context was destroyed" in str(e).lower()
                or "navigation" in str(e).lower()
            ):
                time.sleep(0.5)
                continue
            raise
    return []


def dump_diagnostics(page, path="debug_dump.txt"):
    """Write out everything we can see about checkboxes/rows on the page,
    so a failure can be diagnosed from real data instead of guesswork."""
    info = page.evaluate(
        """
        () => {
            const out = {};
            const inputs = Array.from(document.querySelectorAll('input[type=checkbox]'));
            out.input_checkbox_count = inputs.length;
            out.input_checkbox_samples = inputs.slice(0, 5).map(cb => cb.outerHTML);
            const allInputs = Array.from(document.querySelectorAll('input'));
            out.all_input_types = allInputs.map(i => i.type);
            out.body_has_sliedrecht = document.body.innerText.includes('Sliedrecht');
            out.body_has_ketelhaven = document.body.innerText.includes('Ketelhaven');
            return out;
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
            f.write(
                f"[{i}] id={r.get('id')!r} date={r.get('date')!r}\n     text={r.get('text')!r}\n\n"
            )
    page.screenshot(path="debug_screenshot.png", full_page=False)
    print(f"\nWrote diagnostics to {path} and debug_screenshot.png")
    return info


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


def normalize_station(name):
    s = re.sub(r"^Rotterdam,\s*", "", name.strip())
    s = re.sub(r"\(.*", "", s).strip()
    return s.lower()


def stations_match(a, b):
    a, b = normalize_station(a), normalize_station(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


# Known OV carriers -- used to split destination station from carrier name
# when they are concatenated without whitespace in the row text.
# e.g. "Rotterdam, ZuidpleinR-net€ 6.03" -> dest="Rotterdam, Zuidplein"
CARRIERS_RE = re.compile(
    r"R-net|RET|NS|HTM|Arriva|Connexxion|Keolis|Syntus|GVB|EBS|Qbuzz"
)


def parse_trip_row(checkbox_id, date, text):
    """Pulls (checkin time, departure station, checkout time, destination
    station) out of a row's flattened text. Returns None if the row
    doesn't look like a parseable single trip.

    The page renders rows in a compact format with no whitespace between
    fields, e.g.:
        '08:24Rotterdam, Zuidplein (perron G08:58Sliedrecht, Station BaanhoekR-net€ 4.99 Declarations'

    Splitting on HH:MM timestamps gives:
        parts[0] = ''                                   (before checkin)
        parts[1] = 'Rotterdam, Zuidplein (perron G'     (dep station)
        parts[2] = 'Sliedrecht, Station BaanhoekR-net€ 4.99 Declarations'
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^ dest + carrier + amount

    We then strip the carrier and amount from parts[2] to get dest_station.
    """
    if not text or not date:
        return None
    times = re.findall(r"\d{2}:\d{2}", text)
    if len(times) < 2:
        return None
    parts = re.split(r"\d{2}:\d{2}", text)
    if len(parts) < 3:
        return None
    dep_station = parts[1].strip(" \n\t-()")
    # parts[2]: strip everything from '€' onward, then strip the carrier name
    dest_raw = re.split(r"€", parts[2])[0]
    dest_station = CARRIERS_RE.split(dest_raw)[0].strip(" \n\t-()")
    if not dep_station or not dest_station:
        return None
    return Trip(checkbox_id, date, times[0], dep_station, times[1], dest_station)


def to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def build_chains(trips, max_gap_minutes=MAX_TRANSFER_GAP_MINUTES):
    """Union-Find over same-day trips, sorted chronologically, linking
    consecutive trips when one's destination roughly matches the next's
    departure within max_gap_minutes."""
    by_day = {}
    for t in trips:
        by_day.setdefault(t.date, []).append(t)

    chains = []
    for date, day_trips in by_day.items():
        day_trips.sort(key=lambda t: to_minutes(t.checkin))
        parent = list(range(len(day_trips)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        for i in range(len(day_trips) - 1):
            a, b = day_trips[i], day_trips[i + 1]
            gap = to_minutes(b.checkin) - to_minutes(a.checkout)
            if 0 <= gap <= max_gap_minutes and stations_match(
                a.dest_station, b.dep_station
            ):
                union(i, i + 1)

        groups = {}
        for i, t in enumerate(day_trips):
            groups.setdefault(find(i), []).append(t)
        chains.extend(groups.values())

    return chains


def select_work_chains(trips, max_gap_minutes=MAX_TRANSFER_GAP_MINUTES):
    """Returns the flat list of Trips that belong to a chain touching any
    WORK_STATIONS entry on at least one leg."""
    chains = build_chains(trips, max_gap_minutes)
    work_trips = []
    for chain in chains:
        touches_work = any(
            any(
                station in t.dep_station or station in t.dest_station
                for station in WORK_STATIONS
            )
            for t in chain
        )
        if touches_work:
            work_trips.extend(chain)
    return work_trips


# --------------------------------------------------------------------------
# Page interaction: selecting, switching to declarations, downloading
# --------------------------------------------------------------------------


def get_declared_count(page):
    """Reads the live 'X declarations' counter off the page, piercing
    shadow DOM, so we can verify the site itself registered our clicks."""
    text = page.evaluate(
        """
        () => {
            function deepText(node) {
                let t = '';
                if (node.shadowRoot) t += deepText(node.shadowRoot);
                const children = node.childNodes || [];
                for (const child of children) {
                    if (child.nodeType === Node.TEXT_NODE) t += child.textContent;
                    else if (child.nodeType === Node.ELEMENT_NODE ||
                             child.nodeType === Node.DOCUMENT_FRAGMENT_NODE) t += deepText(child);
                }
                return t;
            }
            return deepText(document.body);
        }
        """
    )
    m = re.search(r"(\d+)\s*declarat", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def collect_trips(page):
    """Scrolls to load everything, scrapes rows, and parses them into Trip
    objects (skipping the master 'select all' checkbox and any row that
    couldn't be parsed)."""
    scroll_to_load_all(page)
    raw_rows = get_rows_with_dates(page)

    trips = []
    skipped = 0
    for row in raw_rows:
        if row.get("id") == "declare-all-checkbox":
            continue
        trip = parse_trip_row(row.get("id"), row.get("date"), row.get("text"))
        if trip is None:
            skipped += 1
            continue
        trips.append(trip)

    if skipped:
        print(
            f"Note: {skipped} checkbox rows could not be parsed (no date/text "
            f"found) and were skipped. If this number looks high relative to "
            f"the total, run dump_diagnostics() to inspect why."
        )

    return trips


def print_dry_run(work_trips):
    print(
        f"\n[DRY RUN] {len(work_trips)} trip(s) would be checked, across "
        f"{len(set(t.date for t in work_trips))} day(s):\n"
    )
    by_day = {}
    for t in work_trips:
        by_day.setdefault(t.date, []).append(t)
    for date, day_trips in by_day.items():
        day_trips.sort(key=lambda t: to_minutes(t.checkin))
        print(f"  {date}")
        for t in day_trips:
            is_direct = any(
                s in t.dep_station or s in t.dest_station for s in WORK_STATIONS
            )
            tag = " *" if is_direct else "  (connecting leg)"
            print(
                f"    {t.checkin}-{t.checkout}  {t.dep_station} -> {t.dest_station}{tag}"
            )
    print()


def check_trips(page, work_trips):
    """Checks the checkboxes for the given trips and fires a 'change' event
    on each so Angular's model layer registers the update."""
    checked = 0
    for t in work_trips:
        if not t.checkbox_id:
            continue
        box = page.locator(f'[id="{t.checkbox_id}"]')
        try:
            if box.count() and box.first.is_visible():
                box.first.scroll_into_view_if_needed()
                if not box.first.is_checked():
                    box.first.check(force=True)
                    page.evaluate(
                        "(el) => el.dispatchEvent(new Event('change', { bubbles: true }))",
                        box.first.element_handle(),
                    )
                checked += 1
        except Exception:
            pass
    return checked


def switch_to_declarations_and_download(page):
    before = get_declared_count(page)
    print(f"On-page declared count BEFORE switching radio: {before}")

    radios = page.locator("input[type=radio]")
    n = radios.count()
    if n < 2:
        raise RuntimeError(
            f"Expected at least 2 radio buttons in the download "
            f"bar, found {n}. Page may not be fully loaded."
        )
    radios.nth(1).check(force=True)  # second radio = the "declarations" one
    time.sleep(1)  # give Angular a tick to recompute the filtered list

    is_checked = radios.nth(1).is_checked()
    after = get_declared_count(page)
    print(f"Radio[1] checked after click: {is_checked}")
    print(f"On-page declared count AFTER switching radio: {after}")

    if not is_checked:
        raise RuntimeError(
            "The 'declarations' radio did not register as checked -- "
            "stopping before download."
        )

    with page.expect_download() as download_info:
        page.get_by_role("button", name=re.compile("Download", re.I)).click()
    download = download_info.value
    out_path = DOWNLOAD_DIR / download.suggested_filename
    download.save_as(out_path)
    return out_path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
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
        page = context.new_page()
        page.goto("https://www.ns.nl/")

        input(
            "\n>>> Click 'Log in' yourself, log in, go to 'Journey history & "
            "transactions', set the period you want, and click Show.\n"
            ">>> Once all trips for that month are visible, press Enter here to continue...\n"
        )

        page.wait_for_load_state("networkidle")
        page.wait_for_selector("input[type=checkbox]", timeout=15000)
        time.sleep(1)

        trips = collect_trips(page)
        print(f"Parsed {len(trips)} trip rows from the page.")

        work_trips = select_work_chains(trips)

        if not work_trips:
            print(
                "No matching trips/chains found -- dumping diagnostics so we "
                "can see what's actually on the page..."
            )
            dump_diagnostics(page)
            input("Press Enter to close the browser...")
            return

        print_dry_run(work_trips)
        answer = (
            input(
                f"Proceed to check these {len(work_trips)} trip(s) and download? [y/N] "
            )
            .strip()
            .lower()
        )
        if answer != "y":
            print("Aborted -- no checkboxes were touched.")
            input("Press Enter to close the browser...")
            return

        checked = check_trips(page, work_trips)
        print(f"Checked {checked} trips on the page.")
        print(
            f"On-page declared count right after selection: {get_declared_count(page)}"
        )

        out_path = switch_to_declarations_and_download(page)
        print(f"Downloaded: {out_path}")

        input("Done. Press Enter to close the browser...")


if __name__ == "__main__":
    main()
