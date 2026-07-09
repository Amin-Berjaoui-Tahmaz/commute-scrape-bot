"""Trip parsing and journey-chain building.

Everything here is pure Python (no Playwright), which is exactly why it's
split out: it's the part with actual logic to get wrong, and the part that
can be unit tested without a browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import groupby
from typing import Optional, Sequence

# Known OV carriers -- used to split destination station from carrier name
# when they're concatenated without whitespace in the row text, e.g.
# "Rotterdam, ZuidpleinR-net€ 6.03" -> dest="Rotterdam, Zuidplein"
CARRIERS_RE = re.compile(
    r"R-net|RET|NS|HTM|Arriva|Connexxion|Keolis|Syntus|GVB|EBS|Qbuzz"
)

DEFAULT_MAX_TRANSFER_GAP_MINUTES = 25


@dataclass
class Trip:
    # checkbox_id can be missing (e.g. row scraped without an id attribute),
    # so this is Optional rather than str -- mypy will flag any code path
    # that uses it without a None check.
    checkbox_id: Optional[str]
    date: str
    checkin: str
    dep_station: str
    checkout: str
    dest_station: str


def touches_work(trip: Trip, work_stations: Sequence[str]) -> bool:
    """Whether either leg of this trip mentions one of the work stations.

    This used to be a Trip.touches_work property that read a module-level
    WORK_STATIONS constant. Pulling it out to a plain function means the
    station list is just data passed in by the caller (CLI args), not a
    global the model secretly depends on.
    """
    return any(s in trip.dep_station or s in trip.dest_station for s in work_stations)


def normalize_station(name: str) -> str:
    s = re.sub(r"^Rotterdam,\s*", "", name.strip())
    return re.sub(r"\(.*", "", s).strip().lower()


def stations_match(a: str, b: str) -> bool:
    na, nb = normalize_station(a), normalize_station(b)
    return bool(na) and bool(nb) and (na == nb or na in nb or nb in na)


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_trip_row(
    checkbox_id: Optional[str], date: Optional[str], text: Optional[str]
) -> Optional[Trip]:
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


def build_chains(
    trips: list[Trip],
    max_gap_minutes: int = DEFAULT_MAX_TRANSFER_GAP_MINUTES,
) -> list[list[Trip]]:
    """Groups each day's trips (sorted chronologically) into journey chains.
    A chain only ever links *adjacent* trips in that sorted order (one
    trip's destination roughly matching the next's departure within the
    transfer window), so a single linear scan is enough -- no union-find
    needed."""
    chains = []
    for _date, day_trips in groupby(
        sorted(trips, key=lambda t: (t.date, to_minutes(t.checkin))),
        key=lambda t: t.date,
    ):
        current: list[Trip] = []
        for t in day_trips:
            if current:
                prev = current[-1]
                gap = to_minutes(t.checkin) - to_minutes(prev.checkout)
                if not (
                    0 <= gap <= max_gap_minutes
                    and stations_match(prev.dest_station, t.dep_station)
                ):
                    chains.append(current)
                    current = []
            current.append(t)
        if current:
            chains.append(current)
    return chains


def select_work_chains(
    trips: list[Trip],
    work_stations: Sequence[str],
    max_gap_minutes: int = DEFAULT_MAX_TRANSFER_GAP_MINUTES,
) -> list[Trip]:
    """Returns the flat list of Trips belonging to a chain that touches any
    work_stations entry on at least one leg."""
    return [
        t
        for chain in build_chains(trips, max_gap_minutes)
        if any(touches_work(t, work_stations) for t in chain)
        for t in chain
    ]
