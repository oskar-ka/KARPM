"""Tests against real Kleinanzeigen markup and the encoding bug it exposed.

`tests/fixtures/live_search_astro.html` is trimmed from an actual search-results
page captured 2026-09-13, after Kleinanzeigen rebuilt the site in Astro. Its
class names are Tailwind utilities that carry no meaning and will churn, so the
parser keys off text shape instead - these tests pin that behaviour.
"""

from pathlib import Path

import pytest
import requests

from karpm.http import decode
from karpm.parse.search import parse_search_page

FIXTURE = Path(__file__).parent / "fixtures" / "live_search_astro.html"


@pytest.fixture(scope="module")
def result():
    return parse_search_page(FIXTURE.read_text(encoding="utf-8"))


def test_every_ad_yields_an_id_title_and_price(result):
    assert len(result["items"]) == 5
    for item in result["items"]:
        assert item["id"].isdigit()
        assert item["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/")
        assert item["title"], f"{item['id']} has no title"
        assert item["price_eur"] is not None, f"{item['id']} has no price"


def test_umlauts_survive(result):
    """German text must arrive intact - it is what the scoring prompt reads."""
    text = " ".join(
        (i["title"] or "") + " " + (i["snippet"] or "") + " " + (i["location"] or "")
        for i in result["items"]
    )
    assert "Schlüssel" in text
    assert "Ã" not in text, "mojibake: the page was decoded as Latin-1"


def test_negotiable_prices_are_flagged(result):
    by_id = {i["id"]: i for i in result["items"]}
    assert by_id["3510912315"]["price_eur"] == 600
    assert by_id["3510912315"]["price_kind"] == "vb"
    assert by_id["3370386693"]["price_eur"] == 999
    assert by_id["3370386693"]["price_kind"] == "fixed"


def test_reduced_price_uses_the_current_not_the_struck_through_one(result):
    """This ad shows '850 €' beside a struck-through '1.150 €'."""
    ad = next(i for i in result["items"] if i["id"] == "3505149097")
    assert ad["price_eur"] == 850


def test_wanted_ads_are_flagged(result):
    """'Gesuch' means someone wants to buy - not an offer we should collect."""
    wanted = [i["id"] for i in result["items"] if i["is_wanted"]]
    assert wanted == ["3501249075"]
    assert next(i for i in result["items"] if i["is_wanted"])["title"].startswith("Suche")


def test_commercial_sellers_are_flagged(result):
    assert [i["id"] for i in result["items"] if i["is_commercial"]] == ["3483130292"]


def test_location_and_date_are_extracted(result):
    by_id = {i["id"]: i for i in result["items"]}
    assert by_id["3370386693"]["location"] == "90449 Gebersdorf"
    assert by_id["3370386693"]["posted_at"].startswith("2026-04-02")


def test_thumbnail_and_snippet_come_from_the_embedded_json(result):
    ad = next(i for i in result["items"] if i["id"] == "3370386693")
    assert ad["thumbnail"].startswith("https://img.kleinanzeigen.de/")
    assert "Schlüssel" in ad["snippet"]


def test_next_page_uses_the_next_arrow_not_a_numbered_link(result):
    """Matching 'Seite N' would pick 'Seite 1' on page 2 and walk backwards."""
    assert result["next_url"].endswith(
        "/seite:2/c305l5510+motorraeder_roller.marke_s:bmw+motorraeder_roller.type_s:motorrad"
    )


def test_pagination_stops_on_the_last_page():
    html = FIXTURE.read_text(encoding="utf-8").replace('aria-label="Nächste"', 'aria-label="x"')
    assert parse_search_page(html)["next_url"] is None


# --- the encoding bug ---------------------------------------------------------

def _response(body: bytes, content_type: str) -> requests.Response:
    resp = requests.Response()
    resp._content = body
    resp.status_code = 200
    resp.headers["Content-Type"] = content_type
    resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
    return resp


UMLAUT_PAGE = '<html><head><meta charset="UTF-8"></head><body>Bremsklötze für 1.250 €</body></html>'


def test_utf8_page_without_a_header_charset_is_not_mangled():
    """requests defaults text/* to ISO-8859-1 when the header omits a charset,
    which double-encodes every umlaut. The meta tag must win."""
    resp = _response(UMLAUT_PAGE.encode("utf-8"), "text/html")
    assert resp.encoding in (None, "ISO-8859-1")        # the trap
    assert "Bremsklötze für" in decode(resp)
    assert "Ã¶" not in decode(resp)


def test_explicit_header_charset_is_respected():
    resp = _response(UMLAUT_PAGE.encode("utf-8"), "text/html; charset=utf-8")
    assert "Bremsklötze für" in decode(resp)


def test_unknown_declared_charset_falls_back_instead_of_raising():
    body = '<html><head><meta charset="totally-not-a-charset"></head><body>ö</body></html>'
    assert decode(_response(body.encode("utf-8"), "text/html"))
