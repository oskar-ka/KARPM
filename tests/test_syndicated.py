"""The mobile.de spec sheet that some ads carry inside their description.

The risk here is not missing a block - it is eating a description that was never
one. A seller listing "Reifen: neu / Kette: neu / Bremsen: neu" has written
three label lines, and cutting those out would throw away exactly the sort of
thing the scoring prompt wants.
"""

from pathlib import Path

import pytest

from karpm.parse import syndicated
from karpm.parse.detail import parse_detail_page

FIXTURES = Path(__file__).parent / "fixtures"

BLOCK = """Gepflegte GS aus zweiter Hand.
Scheckheft lückenlos.

Motorrad, Enduro/Reiseenduro
Gebrauchtfahrzeug

Erstzulassung: 8/2004
Kraftstoffart: --
Anzahl der Fahrzeughalter: 2
HU: Neu
Farbe (Hersteller): --
Farbe: Gelb
Antriebsart: Kardan

Ausstattung
ABS, Griffheizung, Scheckheftgepflegt, Sturzbügel

Inserat bereitgestellt von"""


def test_the_block_becomes_attributes():
    attributes, _, _ = syndicated.split(BLOCK)
    assert attributes["Erstzulassung"] == "8/2004"
    assert attributes["Anzahl der Fahrzeughalter"] == "2"
    assert attributes["Antriebsart"] == "Kardan"
    assert attributes["Farbe"] == "Gelb"


def test_values_stated_as_unknown_are_dropped():
    """mobile.de writes "--" where it has nothing; storing that as a colour
    would be worse than storing nothing."""
    attributes, _, _ = syndicated.split(BLOCK)
    assert "Kraftstoffart" not in attributes
    assert "Farbe (Hersteller)" not in attributes


def test_the_equipment_list_is_separated():
    _, equipment, _ = syndicated.split(BLOCK)
    assert equipment == ["ABS", "Griffheizung", "Scheckheftgepflegt", "Sturzbügel"]


def test_the_vehicle_type_above_the_block_is_taken():
    attributes, _, _ = syndicated.split(BLOCK)
    assert attributes["Art"] == "Enduro/Reiseenduro"
    assert attributes["Fahrzeugzustand"] == "Gebrauchtfahrzeug"


def test_what_the_seller_wrote_is_kept():
    _, _, rest = syndicated.split(BLOCK)
    assert rest == "Gepflegte GS aus zweiter Hand.\nScheckheft lückenlos."


def test_the_attribution_footer_goes_with_the_block():
    _, _, rest = syndicated.split(BLOCK)
    assert "Inserat bereitgestellt von" not in rest


# --- what must not be touched -------------------------------------------

def test_a_sellers_own_list_is_left_alone():
    """Three label lines, and every one of them is worth reading."""
    text = ("Verkaufe meine GS.\nReifen: neu\nKette: neu\nBremsen: neu\n"
            "Alles gemacht, siehe Rechnungen.")
    attributes, equipment, rest = syndicated.split(text)
    assert attributes == {} and equipment == []
    assert rest == text


def test_one_label_in_a_sentence_is_left_alone():
    text = "Verkaufe meine GS. Farbe: schwarz. Bei Fragen gerne melden."
    assert syndicated.split(text) == ({}, [], text)


def test_a_block_without_its_markers_is_left_alone():
    """Without something only mobile.de writes, it is someone's own list."""
    text = "Zustand: gut\nPreis: VB\nKontakt: nur Telefon"
    assert syndicated.split(text)[2] == text


def test_a_marker_is_enough_even_without_the_footer():
    text = ("Schöne Maschine.\n\nErstzulassung: 3/2015\nAnzahl der Fahrzeughalter: 1\n"
            "Antriebsart: Kette")
    attributes, _, rest = syndicated.split(text)
    assert attributes["Antriebsart"] == "Kette"
    assert rest == "Schöne Maschine."


@pytest.mark.parametrize("text", [None, "", "Nur ein Satz ohne alles."])
def test_nothing_to_do(text):
    assert syndicated.split(text) == ({}, [], text)


def test_a_description_that_is_only_a_block_comes_back_empty():
    attributes, _, rest = syndicated.split(
        "Erstzulassung: 1/2020\nAntriebsart: Kette\nFarbe: rot")
    assert attributes["Farbe"] == "rot"
    assert rest is None


# --- against the page it was found on ------------------------------------

def test_the_astro_ad_fills_columns_that_were_null():
    """Before this, the page's six attributes were all we took from it and the
    spec sheet sat in the description as prose."""
    html = (FIXTURES / "live_detail_astro.html").read_text(encoding="utf-8")
    parsed = parse_detail_page(html, "https://x/1")

    assert parsed["owners"] == 2
    assert parsed["color"] == "Gelb"
    assert parsed["drive_type"] == "Kardan"
    assert parsed["condition"] == "Gebrauchtfahrzeug"
    assert "ABS" in parsed["equipment_json"]
    assert "Erstzulassung" not in (parsed["description"] or "")


def test_the_block_wins_where_it_is_more_precise():
    """The page says "2004"; the block says "8/2004". Eight months matter when
    the number they feed is the bike's age."""
    html = (FIXTURES / "live_detail_astro.html").read_text(encoding="utf-8")
    assert parse_detail_page(html, "https://x/1")["first_reg_date"] == "2004-08-01"


def test_a_specific_type_beats_the_category():
    """"Art: Motorräder" is true of every ad in a motorcycle search."""
    html = (FIXTURES / "live_detail_astro.html").read_text(encoding="utf-8")
    assert parse_detail_page(html, "https://x/1")["bike_type"] == "Enduro/Reiseenduro"


@pytest.mark.parametrize("fixture", ["live_detail_bmw_fixed.html", "live_detail_bmw_vb.html"])
def test_hand_written_ads_are_untouched(fixture):
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    parsed = parse_detail_page(html, "https://x/1")
    assert parsed["description"].startswith("Verkaufe")
    assert parsed.get("equipment_json") is None


def test_the_old_stack_gets_its_transmission():
    """It was the one attribute on that page with no column of its own."""
    html = (FIXTURES / "live_detail_bmw_fixed.html").read_text(encoding="utf-8")
    assert parse_detail_page(html, "https://x/1")["transmission"] == "Manuell"
