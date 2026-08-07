"""Browser-side helpers and page scraping.

Everything here talks to a live Playwright Page, which is why it's kept
separate from trip_model.py: this module needs a browser to test, the other
one doesn't.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional, TypedDict, cast

from playwright.sync_api import Page


class ScrapedRow(TypedDict):
    id: Optional[str]
    date: Optional[str]
    text: Optional[str]

logger = logging.getLogger(__name__)

DATE_RE = (
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}"
)

# Pierce shadow DOM. Injected once via add_init_script so every
# page.evaluate() call below can reuse the same window.__ns.* functions
# instead of redefining them each time.
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
    logger.debug("scroll_to_load_all settled at %d checkboxes", prev_count)
    return prev_count


def get_rows_with_dates(page: Page, retries: int = 5) -> list[ScrapedRow]:
    """Returns [{id, date, text}, ...], one entry per checkbox on the page,
    each with its isolated row text and nearest preceding date heading."""
    for attempt in range(retries):
        try:
            # page.evaluate returns Any -- cast to the shape we know the JS
            # side produces so callers get real typing instead of Any leaking
            # through the whole rest of the pipeline.
            return cast(list[ScrapedRow], page.evaluate(SCRAPE_ROWS_JS))
        except Exception as e:
            if (
                "context was destroyed" in str(e).lower()
                or "navigation" in str(e).lower()
            ):
                logger.debug(
                    "get_rows_with_dates retry %d/%d after: %s", attempt + 1, retries, e
                )
                time.sleep(0.5)
                continue
            raise
    logger.warning("get_rows_with_dates gave up after %d retries", retries)
    return []


def get_declared_count(page: Page) -> Optional[int]:
    """Reads the live 'X declarations' counter off the page."""
    m = re.search(r"(\d+)\s*declarat", page.evaluate(DECLARED_COUNT_JS), re.IGNORECASE)
    return int(m.group(1)) if m else None


def switch_to_declarations(page: Page) -> Optional[int]:
    """Switches the bottom filter bar to "X declarations" so the person
    can see exactly what's about to be downloaded and click Download
    themselves -- deliberately not automated, since a human needs to
    review before that happens. Returns the declared count shown after
    switching, or None if it couldn't be read."""
    radios = page.locator("input[type=radio]")
    n = radios.count()
    logger.debug("found %d radio button(s) on the page", n)
    if n < 2:
        raise RuntimeError(
            f"Expected at least 2 radio buttons in the download bar, found "
            f"{n}. Page may not be fully loaded."
        )
    radios.nth(1).check(force=True)  # second radio = the "declarations" one
    time.sleep(1)  # give Angular a tick to recompute the filtered list

    if not radios.nth(1).is_checked():
        raise RuntimeError(
            "The 'declarations' radio did not register as checked."
        )
    return get_declared_count(page)

def switch_to_all_trips(page: Page) -> None:
    """Reset the bottom filter bar to the full journey list.

    This is important when processing multiple periods in the same browser
    session. Mijn NS can retain the declarations filter after the previous
    download, which prevents the next declarations view from being
    properly refreshed.
    """
    radios = page.locator("input[type=radio]")
    n = radios.count()

    logger.debug("found %d radio button(s) on the page", n)

    if n < 2:
        raise RuntimeError(
            f"Expected at least 2 radio buttons in the download bar, found {n}."
        )

    radios.nth(0).check(force=True)

    time.sleep(1)

    if not radios.nth(0).is_checked():
        raise RuntimeError(
            "The 'all trips' radio did not register as checked."
        )
