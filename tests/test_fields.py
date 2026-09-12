from datetime import date, datetime

import pytest

from karpm.parse.fields import (
    ad_id_from_url,
    apply_attributes,
    parse_int_de,
    parse_location,
    parse_month_year,
    parse_posted,
    parse_price,
    parse_seller_type,
    slug,
)


@pytest.mark.parametrize("text,expected", [
    ("12.345 km", 12345),
    ("1.234.567", 1234567),
    ("75 PS", 75),
    ("689 ccm", 689),
    ("", None),
    (None, None),
    ("keine Angabe", None),
])
def test_parse_int_de(text, expected):
    assert parse_int_de(text) == expected


@pytest.mark.parametrize("text,amount,kind", [
    ("4.500 € VB", 4500, "vb"),
    ("4.500 €", 4500, "fixed"),
    ("Zu verschenken", 0, "free"),
    ("VB", None, "vb"),
    ("6.250 € Verhandlungsbasis", 6250, "vb"),
    (None, None, "unknown"),
])
def test_parse_price(text, amount, kind):
    assert parse_price(text) == (amount, kind)


@pytest.mark.parametrize("text,expected", [
    ("05/2019", date(2019, 5, 1)),
    ("2019", date(2019, 1, 1)),
    ("Mai 2019", date(2019, 5, 1)),
    ("03.2021", date(2021, 3, 1)),
    ("keine Angabe", None),
])
def test_parse_month_year(text, expected):
    assert parse_month_year(text) == expected


def test_parse_posted_relative():
    now = datetime(2026, 3, 12, 18, 0)
    assert parse_posted("Heute, 14:23", now) == datetime(2026, 3, 12, 14, 23)
    assert parse_posted("Gestern, 09:12", now) == datetime(2026, 3, 11, 9, 12)
    assert parse_posted("01.03.2026", now) == datetime(2026, 3, 1, 0, 0)


def test_parse_location():
    assert parse_location("22765 Hamburg - Altona") == ("22765", "Hamburg - Altona")
    assert parse_location("Hamburg") == (None, "Hamburg")


def test_parse_seller_type():
    assert parse_seller_type("Privater Anbieter") == "private"
    assert parse_seller_type("Gewerblicher Anbieter") == "commercial"
    assert parse_seller_type("Händler") == "commercial"
    assert parse_seller_type("") == "unknown"


def test_ad_id_from_url():
    url = "https://www.kleinanzeigen.de/s-anzeige/yamaha-mt-07/2847612345-305-2074"
    assert ad_id_from_url(url) == "2847612345"
    assert ad_id_from_url(None) is None


def test_slug_normalises_umlauts_and_punctuation():
    assert slug("Erstzulassung:") == "erstzulassung"
    assert slug("Anzahl Fahrzeughalter") == "anzahlfahrzeughalter"
    assert slug("HU/TÜV") == "hutuv"


def test_apply_attributes_maps_typed_fields():
    fields, warnings = apply_attributes({
        "Kilometerstand": "18.400 km",
        "Erstzulassung": "05/2019",
        "Leistung": "75 PS",
        "Hubraum": "689 ccm",
        "Fahrzeugzustand": "Unbeschädigtes Fahrzeug",
        "HU/TÜV": "06/2027",
        "Anzahl Fahrzeughalter": "2",
        "Art": "Naked Bike",
        "Scheckheftgepflegt": "ja",
        "Irgendwas Neues": "Wert",          # unknown labels are ignored, not fatal
    })
    assert fields["km"] == 18400
    assert fields["first_reg_date"] == "2019-05-01"
    assert fields["first_reg_year"] == 2019
    assert fields["hp"] == 75
    assert fields["ccm"] == 689
    assert fields["damaged"] == 0
    assert fields["inspection_until"] == "2027-06-01"
    assert fields["owners"] == 2
    assert fields["bike_type"] == "Naked Bike"
    assert fields["full_service_hist"] == 1
    assert warnings == []


def test_apply_attributes_warns_on_missing():
    _, warnings = apply_attributes({"Art": "Enduro"})
    assert "missing:km" in warnings
    assert "missing:first_reg_year" in warnings


def test_damaged_vehicle_detected():
    fields, _ = apply_attributes({"Fahrzeugzustand": "Beschädigtes Fahrzeug"})
    assert fields["damaged"] == 1
