from ns_select_and_download import parse_trip_row, select_work_chains


def test_june_26_zuidplein_to_rijnhaven_is_selected_when_connected_to_work_trip():
    rows = [
        (
            "x",
            "Friday June 26, 2026",
            "18:11Zuidplein18:17RijnhavenRET€ 0.36 Declarations ",
        ),
        (
            "y",
            "Friday June 26, 2026",
            "17:36Sliedrecht, Station Baanhoek18:10Rotterdam, ZuidpleinR-net€ 6.03 Declarations ",
        ),
    ]

    trips = [parse_trip_row(trip_id, date, text) for trip_id, date, text in rows]
    work_trips = select_work_chains(trips)

    assert [trip.checkbox_id for trip in work_trips] == ["y", "x"]
