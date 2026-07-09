from ns_tool.trip_model import (
    Trip,
    build_chains,
    normalize_station,
    parse_trip_row,
    select_work_chains,
    stations_match,
    to_minutes,
    touches_work,
)

# --------------------------------------------------------------------------
# parse_trip_row
# --------------------------------------------------------------------------


def test_parse_trip_row_basic():
    text = "08:24Rotterdam, Zuidplein (perron G08:58Sliedrecht, Station BaanhoekR-net€ 4.99 Declarations"
    trip = parse_trip_row("cb1", "Monday January 5, 2026", text)
    assert trip == Trip(
        "cb1",
        "Monday January 5, 2026",
        "08:24",
        "Rotterdam, Zuidplein (perron G",
        "08:58",
        "Sliedrecht, Station Baanhoek",
    )


def test_parse_trip_row_missing_date_returns_none():
    text = "08:24Rotterdam, Zuidplein08:58Sliedrecht€ 4.99"
    assert parse_trip_row("cb1", None, text) is None


def test_parse_trip_row_missing_text_returns_none():
    assert parse_trip_row("cb1", "Monday January 5, 2026", None) is None


def test_parse_trip_row_only_one_timestamp_returns_none():
    # A row with just one HH:MM can't be split into dep/dest -- not a trip.
    text = "08:24Rotterdam, Zuidplein€ 4.99"
    assert parse_trip_row("cb1", "Monday January 5, 2026", text) is None


def test_parse_trip_row_strips_carrier_from_destination():
    text = "07:00Papendrecht07:40KetelhavenArriva€ 3.10"
    trip = parse_trip_row("cb2", "Tuesday March 3, 2026", text)
    assert trip is not None
    assert trip.dest_station == "Ketelhaven"


# --------------------------------------------------------------------------
# stations_match / normalize_station
# --------------------------------------------------------------------------


def test_normalize_station_strips_rotterdam_prefix_and_platform():
    assert normalize_station("Rotterdam, Zuidplein (perron G") == "zuidplein"


def test_stations_match_exact():
    assert stations_match("Sliedrecht", "Sliedrecht")


def test_stations_match_substring_either_direction():
    assert stations_match("Sliedrecht, Station Baanhoek", "Sliedrecht")
    assert stations_match("Sliedrecht", "Sliedrecht, Station Baanhoek")


def test_stations_match_rejects_unrelated_stations():
    assert not stations_match("Sliedrecht", "Ketelhaven")


def test_stations_match_empty_strings_never_match():
    assert not stations_match("", "")
    assert not stations_match("Sliedrecht", "")


# --------------------------------------------------------------------------
# to_minutes
# --------------------------------------------------------------------------


def test_to_minutes():
    assert to_minutes("00:00") == 0
    assert to_minutes("01:30") == 90
    assert to_minutes("23:59") == 23 * 60 + 59


# --------------------------------------------------------------------------
# build_chains
# --------------------------------------------------------------------------


def _trip(
    date: str, checkin: str, dep: str, checkout: str, dest: str, cb: str = "cb"
) -> Trip:
    return Trip(cb, date, checkin, dep, checkout, dest)


def test_build_chains_links_adjacent_legs_within_gap():
    d = "Monday January 5, 2026"
    trips = [
        _trip(d, "08:24", "Rotterdam, Zuidplein", "08:58", "Sliedrecht, Baanhoek"),
        # 5 min transfer, station overlaps -> same chain
        _trip(d, "09:03", "Sliedrecht, Baanhoek", "09:15", "Werkadres"),
    ]
    chains = build_chains(trips, max_gap_minutes=20)
    assert len(chains) == 1
    assert len(chains[0]) == 2


def test_build_chains_splits_on_gap_too_large():
    d = "Monday January 5, 2026"
    trips = [
        _trip(d, "08:24", "A", "08:58", "B"),
        # 45 min gap > 20 min max -> new chain
        _trip(d, "09:43", "B", "10:00", "C"),
    ]
    chains = build_chains(trips, max_gap_minutes=20)
    assert len(chains) == 2


def test_build_chains_splits_on_station_mismatch():
    d = "Monday January 5, 2026"
    trips = [
        _trip(d, "08:24", "A", "08:58", "B"),
        # quick transfer but stations don't match -> new chain
        _trip(d, "09:00", "Z", "09:20", "C"),
    ]
    chains = build_chains(trips, max_gap_minutes=20)
    assert len(chains) == 2


def test_build_chains_splits_across_different_days():
    trips = [
        _trip("Monday January 5, 2026", "08:24", "A", "08:58", "B"),
        _trip("Tuesday January 6, 2026", "08:24", "B", "08:58", "C"),
    ]
    chains = build_chains(trips, max_gap_minutes=20)
    assert len(chains) == 2


def test_build_chains_three_leg_journey_stays_one_chain():
    # Regression case for the union-find -> linear-scan rewrite: a 3-leg
    # same-day journey (metro -> bus -> arrival) must stay a single chain,
    # not just pairwise-linked fragments.
    d = "Wednesday January 7, 2026"
    trips = [
        _trip(d, "08:00", "Home", "08:20", "Hub"),
        _trip(d, "08:25", "Hub", "08:50", "Transfer"),
        _trip(d, "08:55", "Transfer", "09:10", "Work"),
    ]
    chains = build_chains(trips, max_gap_minutes=20)
    assert len(chains) == 1
    assert len(chains[0]) == 3


def test_build_chains_sorts_out_of_order_input():
    d = "Monday January 5, 2026"
    trips = [
        _trip(d, "09:03", "Sliedrecht", "09:15", "Werkadres"),
        _trip(d, "08:24", "Rotterdam", "08:58", "Sliedrecht"),
    ]
    chains = build_chains(trips, max_gap_minutes=20)
    assert len(chains) == 1
    assert [t.checkin for t in chains[0]] == ["08:24", "09:03"]


# --------------------------------------------------------------------------
# touches_work / select_work_chains
# --------------------------------------------------------------------------


def test_touches_work_checks_both_legs():
    work_stations = ["Sliedrecht", "Ketelhaven"]
    t = _trip("d", "08:00", "Rotterdam", "08:30", "Sliedrecht, Baanhoek")
    assert touches_work(t, work_stations)
    t2 = _trip("d", "08:00", "Ketelhaven", "08:30", "Rotterdam")
    assert touches_work(t2, work_stations)
    t3 = _trip("d", "08:00", "Rotterdam", "08:30", "Utrecht")
    assert not touches_work(t3, work_stations)


def test_select_work_chains_includes_connecting_legs_not_touching_work():
    # A chain should be selected in full if ANY leg touches work, even if
    # other legs in the chain don't mention a work station themselves.
    d = "Monday January 5, 2026"
    trips = [
        _trip(d, "08:00", "Home", "08:20", "Hub"),
        _trip(d, "08:25", "Hub", "08:50", "Sliedrecht"),
    ]
    selected = select_work_chains(trips, ["Sliedrecht"], max_gap_minutes=20)
    assert len(selected) == 2


def test_select_work_chains_excludes_non_work_chains():
    d = "Monday January 5, 2026"
    trips = [
        _trip(d, "08:00", "Home", "08:20", "Utrecht"),
        _trip(d, "20:00", "Rotterdam", "20:30", "Sliedrecht"),
    ]
    selected = select_work_chains(trips, ["Sliedrecht"], max_gap_minutes=20)
    assert [t.dest_station for t in selected] == ["Sliedrecht"]
