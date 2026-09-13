"""Distance, and the numbers a person works out in their head while reading an ad."""

from datetime import date

import pytest

from karpm import db, derived, geo


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    db.init_db(connection)
    yield connection
    connection.close()


def listing(conn, **overrides):
    data = {"id": "111", "url": "https://x/111", "title": "BMW R 1200 GS",
            "description": "d", "price_eur": 5900, "search_name": "gs"}
    data.update(overrides)
    db.upsert_listing(conn, data)
    conn.commit()
    return conn.execute("SELECT * FROM listings WHERE id = ?", (data["id"],)).fetchone()


# --- distance ------------------------------------------------------------

@pytest.mark.parametrize("home, there, expected, tolerance", [
    ("22765", "80331", 767, 40),        # Hamburg to Munich
    ("10115", "20095", 316, 30),        # Berlin to Hamburg
    ("50667", "40213", 43, 15),         # Cologne to Dusseldorf
])
def test_distance_between_real_postcodes(home, there, expected, tolerance):
    got = geo.distance_km(home, there)
    assert abs(got - expected) <= tolerance, f"{home}->{there} came out {got}"


def test_the_postcode_is_found_in_a_location_string():
    assert geo.postcode("22765 Hamburg - Altona") == "22765"
    assert geo.postcode("Hamburg - Altona") is None


def test_an_unknown_postcode_is_not_a_distance_of_zero():
    """Zero reads as "just round the corner", which is a lie about a bike that
    might be 600 km away."""
    assert geo.distance_km("22765", "99999") is None
    assert geo.distance_km(None, "80331") is None
    assert geo.distance_km("22765", None) is None


def test_the_table_covers_germany():
    assert len(geo._centroids()) > 8000


# --- age and use ---------------------------------------------------------

def test_km_per_year(conn):
    row = listing(conn, km=56000, first_reg_date="2004-08-01")
    assert derived.km_per_year(row, date(2024, 8, 1)) == 2800


def test_km_per_year_needs_both_halves(conn):
    # Separate ids: upserting the same one twice keeps the earlier values, by
    # the rule that a missing field never overwrites a good one.
    no_km = listing(conn, id="a", km=None, first_reg_date="2010-01-01")
    no_date = listing(conn, id="b", km=5000, first_reg_date=None, first_reg_year=None)
    assert derived.km_per_year(no_km) is None
    assert derived.km_per_year(no_date) is None


def test_a_year_only_registration_still_gives_an_age(conn):
    row = listing(conn, km=10000, first_reg_year=2020, first_reg_date=None)
    assert derived.age_years(row, date(2025, 1, 1)) == pytest.approx(5.0, abs=0.1)


def test_a_bike_registered_today_has_no_km_per_year(conn):
    """Dividing by an age of zero is how you get a million km a year."""
    row = listing(conn, km=10, first_reg_date="2026-09-13")
    assert derived.km_per_year(row, date(2026, 9, 13)) is None


# --- roadworthiness ------------------------------------------------------

def test_months_of_hu_left(conn):
    row = listing(conn, inspection_until="2027-06-01")
    assert derived.hu_months_left(row, date(2026, 9, 1)) == 9


def test_an_expired_hu_is_negative_not_missing(conn):
    row = listing(conn, inspection_until="2026-03-01")
    assert derived.hu_months_left(row, date(2026, 9, 1)) == -6


def test_no_hu_date_is_unknown(conn):
    assert derived.hu_months_left(listing(conn)) is None


# --- the market ----------------------------------------------------------

def test_days_on_market_prefers_the_posting_date(conn):
    row = listing(conn, posted_at="2026-09-01T10:00:00")
    from datetime import datetime
    assert derived.days_on_market(row, datetime(2026, 9, 13)) == 12


def test_days_on_market_falls_back_to_when_we_first_saw_it(conn):
    row = listing(conn, posted_at=None)
    assert derived.days_on_market(row) is not None


def test_a_price_cut_is_measured_from_the_first_asking_price(conn):
    listing(conn)
    db.add_history(conn, "111", "price_change", price_eur=6200, prev_price_eur=6500)
    db.add_history(conn, "111", "price_change", price_eur=5900, prev_price_eur=6200)
    conn.commit()
    assert derived.total_price_drop(conn, "111") == (600, 9)


def test_a_price_that_never_moved_has_no_cut(conn):
    listing(conn)
    assert derived.total_price_drop(conn, "111") is None


def test_a_price_that_went_up_is_not_a_cut(conn):
    listing(conn)
    db.add_history(conn, "111", "price_change", price_eur=6500, prev_price_eur=5900)
    conn.commit()
    assert derived.total_price_drop(conn, "111") is None


# --- the whole set -------------------------------------------------------

def test_summarise_returns_every_key_even_when_it_knows_nothing(conn):
    """The page reads these by name; a missing key would be a 500."""
    row = listing(conn, km=None, first_reg_date=None, posted_at=None,
                  inspection_until=None, postcode=None, location=None)
    summary = derived.summarise(conn, row, home_plz=None)
    assert set(summary) == {"age_years", "km_per_year", "hu_months_left",
                            "days_on_market", "price_drop", "distance_km",
                            "equipment", "photo_count"}
    assert summary["km_per_year"] is None
    assert summary["equipment"] == []


def test_summarise_fills_in_what_it_can(conn):
    row = listing(conn, km=56000, first_reg_date="2004-08-01",
                  inspection_until="2027-06-01", postcode="80331",
                  equipment_json=["ABS", "Griffheizung"])
    summary = derived.summarise(conn, row, home_plz="22765", today=date(2026, 9, 1))
    assert summary["km_per_year"] == 2534
    assert summary["hu_months_left"] == 9
    assert 700 < summary["distance_km"] < 850
    assert summary["equipment"] == ["ABS", "Griffheizung"]
