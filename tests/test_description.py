"""Descriptions arrive as HTML from two of the three parsing layers.

The JSON-LD block and the Astro payload both carry the description as markup;
only the CSS-selector path reads get_text(). Storing the string as it stands put
`<br />` and `&#x2F;` in the database, in the scoring prompt and on the page.
"""

from pathlib import Path

import pytest

from karpm import db
from karpm.parse.detail import parse_detail_page
from karpm.parse.fields import html_to_text

FIXTURES = Path(__file__).parent / "fixtures"


# --- the conversion itself ----------------------------------------------

def test_br_becomes_a_line_break():
    assert html_to_text("one<br />two") == "one\ntwo"


@pytest.mark.parametrize("tag", ["<br>", "<br/>", "<br />", "<BR>", "<br    />"])
def test_every_spelling_of_br(tag):
    assert html_to_text(f"one{tag}two") == "one\ntwo"


def test_entities_are_resolved():
    assert html_to_text("Enduro&#x2F;Reiseenduro") == "Enduro/Reiseenduro"
    assert html_to_text("Koffer &amp; Topcase") == "Koffer & Topcase"
    assert html_to_text("H&ouml;he") == "Höhe"


def test_a_double_break_is_kept_as_a_paragraph():
    assert html_to_text("one<br /><br />two") == "one\n\ntwo"


def test_more_than_two_breaks_collapse():
    """Otherwise a run of empty lines takes over the page."""
    assert html_to_text("one<br /><br /><br /><br />two") == "one\n\ntwo"


def test_remaining_tags_are_dropped_but_their_text_is_kept():
    assert html_to_text('<span class="x" id="y">Inserat</span>') == "Inserat"


def test_block_elements_end_a_line():
    assert html_to_text("<p>one</p><p>two</p>") == "one\ntwo"
    assert html_to_text("<ul><li>ABS</li><li>Heizgriffe</li></ul>") == "ABS\nHeizgriffe"


def test_plain_text_is_returned_unchanged():
    assert html_to_text("Verkaufe meine GS.\nKeine Mängel.") == "Verkaufe meine GS.\nKeine Mängel."


def test_prose_containing_an_angle_bracket_survives():
    """"unter < 5000" is text a person typed, not a tag."""
    assert html_to_text("Preis < 5000 VB") == "Preis < 5000 VB"


def test_an_escaped_tag_stays_visible_text():
    """If someone literally wrote <br> in their ad, that is what they meant."""
    assert html_to_text("schreibe &lt;br&gt; hier") == "schreibe <br> hier"


def test_none_and_empty():
    assert html_to_text(None) is None
    assert html_to_text("") is None
    assert html_to_text("<br />") is None


def test_trailing_whitespace_per_line_is_trimmed():
    assert html_to_text("one   <br />   two") == "one\ntwo"


# --- the page it was found on -------------------------------------------

def test_the_astro_ad_page_yields_text_not_markup():
    """The page this was reported from. Its payload is HTML, and every other
    layer of the parser would have been fine."""
    html = (FIXTURES / "live_detail_astro.html").read_text(encoding="utf-8")
    description = parse_detail_page(html, "https://x/1")["description"]

    assert "<br" not in description
    assert "&#x" not in description
    assert "</span>" not in description
    # The content is all still there, a line at a time.
    assert description.startswith("BMW R120Gs im gepflegtem Zustand")
    assert "letzter Service bei 55200Km" in description
    assert "Enduro/Reiseenduro" in description
    assert "Erstzulassung: 8/2004" in description


@pytest.mark.parametrize("fixture", ["live_detail_bmw_fixed.html", "live_detail_bmw_vb.html"])
def test_the_older_ad_pages_are_unaffected(fixture):
    """They read the description through get_text() and were always clean."""
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    description = parse_detail_page(html, "https://x/1")["description"]
    assert "<" not in description and "&#" not in description
    assert description.startswith("Verkaufe")


# --- repairing what is already stored ------------------------------------

MANGLED = "Zustand gut<br />TÜV neu<br /><br />Enduro&#x2F;Reiseenduro"
REPAIRED = "Zustand gut\nTÜV neu\n\nEnduro/Reiseenduro"


@pytest.fixture
def old_database(tmp_path):
    """A database as it was before the fix: markup in the descriptions."""
    conn = db.connect(tmp_path / "old.db")
    db.init_db(conn)
    for listing_id, description in (("111", MANGLED), ("222", "already clean\ntext")):
        db.upsert_listing(conn, {
            "id": listing_id, "url": f"https://x/{listing_id}", "title": "BMW R 1200 GS",
            "description": description, "price_eur": 5900, "search_name": "gs",
        })
    conn.execute("PRAGMA user_version=3")       # wind back to before the repair
    conn.commit()
    return conn


def test_repair_rewrites_stored_markup(old_database):
    assert db.repair_descriptions(old_database) == 1
    got = old_database.execute(
        "SELECT description FROM listings WHERE id = '111'").fetchone()["description"]
    assert got == REPAIRED


def test_repair_leaves_clean_rows_alone(old_database):
    before = old_database.execute(
        "SELECT description, content_hash FROM listings WHERE id = '222'").fetchone()
    db.repair_descriptions(old_database)
    after = old_database.execute(
        "SELECT description, content_hash FROM listings WHERE id = '222'").fetchone()
    assert after["description"] == before["description"]
    assert after["content_hash"] == before["content_hash"]


def test_repair_updates_the_content_hash(old_database):
    """So the next scoring run redoes them, rather than the next scrape
    recording an edit that never happened."""
    before = old_database.execute(
        "SELECT content_hash FROM listings WHERE id = '111'").fetchone()["content_hash"]
    db.repair_descriptions(old_database)
    row = old_database.execute(
        "SELECT description, title, price_eur, content_hash FROM listings "
        "WHERE id = '111'").fetchone()
    assert row["content_hash"] != before
    assert row["content_hash"] == db.content_hash(
        row["title"], row["description"], row["price_eur"])


def test_repair_is_idempotent(old_database):
    assert db.repair_descriptions(old_database) == 1
    assert db.repair_descriptions(old_database) == 0


def test_opening_an_older_database_repairs_it(old_database, caplog):
    """It runs on init, so it happens whichever command is reached for first."""
    with caplog.at_level("WARNING"):
        db.init_db(old_database)
    got = old_database.execute(
        "SELECT description FROM listings WHERE id = '111'").fetchone()["description"]
    assert got == REPAIRED
    # And it says so, including that re-scoring will cost money.
    assert "1 description" in caplog.text
    assert "credits" in caplog.text


def test_a_repaired_listing_is_queued_for_scoring(old_database):
    """The point of recomputing the hash: its score was made from the old text."""
    db.add_score(old_database, "111", {
        "model": "claude-opus-5", "prompt_version": "v1",
        "content_hash": old_database.execute(
            "SELECT content_hash FROM listings WHERE id = '111'").fetchone()["content_hash"],
        "overall": 4, "fit": 4, "value": 4, "fair_price_eur": 6000,
        "headline": "h", "reasoning": "r", "pros": [], "cons": [], "red_flags": [],
    })
    old_database.commit()

    def queued():
        return [r["id"] for r in db.unscored_listings(old_database, True, "v1", 10)]

    assert "111" not in queued(), "it was just scored against its current text"
    db.repair_descriptions(old_database)
    assert "111" in queued(), "its text changed, so the score no longer matches"


def test_a_fresh_database_is_not_touched(tmp_path, caplog):
    """Nothing to repair, and nothing to say about it."""
    conn = db.connect(tmp_path / "new.db")
    with caplog.at_level("WARNING"):
        db.init_db(conn)
    assert "description" not in caplog.text
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
