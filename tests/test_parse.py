"""Parser tests against fixture pages.

The committed fixtures are SYNTHETIC - they reproduce the markup shapes the
parsers target, so they prove the extraction plumbing, not that the selectors
still match the live site. Verify the real thing with:

    karpm probe --url "<a real search url>" --save tests/fixtures/live_search.html

and drop the saved file in here; the tests below run against any fixture you add.
"""

from pathlib import Path

import pytest

from karpm.parse.detail import parse_detail_page
from karpm.parse.search import parse_search_page

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_search_page_extracts_items():
    result = parse_search_page(_read("search_page.html"))
    assert result["selector"] is not None
    assert len(result["items"]) == 2, "duplicate ad ids must be collapsed"

    first = result["items"][0]
    assert first["id"] == "2847612345"
    assert first["url"].endswith("/s-anzeige/yamaha-mt-07-abs/2847612345-305-2074")
    assert first["title"] == "Yamaha MT-07 ABS, Scheckheft"
    assert first["price_eur"] == 5900
    assert first["price_kind"] == "vb"


def test_search_page_finds_next_page():
    result = parse_search_page(_read("search_page.html"))
    assert result["next_url"].endswith("/s-motorraeder-roller/seite:2/yamaha-mt-07/k0c305")


def test_search_page_empty_html_is_not_an_exception():
    result = parse_search_page("<html><body>nothing here</body></html>")
    assert result["items"] == []
    assert result["next_url"] is None


def test_detail_page_typed_fields():
    url = "https://www.kleinanzeigen.de/s-anzeige/yamaha-mt-07-abs/2847612345-305-2074"
    data = parse_detail_page(_read("detail_page.html"), url)

    assert data["id"] == "2847612345"
    assert data["title"] == "Yamaha MT-07 ABS, Scheckheft"
    assert data["price_eur"] == 5900
    assert data["km"] == 18400
    assert data["hp"] == 75
    assert data["ccm"] == 689
    assert data["first_reg_year"] == 2019
    assert data["first_reg_date"] == "2019-05-01"
    assert data["inspection_until"] == "2027-06-01"
    assert data["owners"] == 2
    assert data["bike_type"] == "Naked Bike"
    assert data["damaged"] == 0
    assert data["full_service_hist"] == 1
    assert data["postcode"] == "22765"
    assert data["location"] == "Hamburg - Altona"
    assert data["seller_type"] == "private"
    assert data["seller_id"] == "55512345"
    assert data["view_count"] == 417
    assert "Scheckheft lückenlos" in data["description"]
    assert data["parse_warnings"] == []


def test_detail_page_collects_images():
    data = parse_detail_page(_read("detail_page.html"))
    assert len(data["image_urls"]) == 2
    # The gallery serves several sizes; we normalise to the largest variant.
    assert all(url.endswith("_57.jpg") for url in data["image_urls"])


def test_detail_page_records_raw_attributes():
    data = parse_detail_page(_read("detail_page.html"))
    assert data["attributes_json"]["Kilometerstand"] == "18.400 km"
    assert data["attributes_json"]["Scheckheftgepflegt"] == "ja"


def test_detail_page_warns_when_markup_is_unrecognised():
    data = parse_detail_page("<html><body><p>totally different</p></body></html>")
    assert "missing:title" in data["parse_warnings"]
    assert "missing:price_eur" in data["parse_warnings"]
    assert "missing:id" in data["parse_warnings"]


@pytest.mark.parametrize("name", [p.name for p in FIXTURES.glob("live_search*.html")])
def test_live_search_fixture_if_present(name):
    result = parse_search_page(_read(name))
    assert result["items"], f"{name}: no listings parsed from a real page"


@pytest.mark.parametrize("name", [p.name for p in FIXTURES.glob("live_detail*.html")])
def test_live_detail_fixture_if_present(name):
    data = parse_detail_page(_read(name), "https://www.kleinanzeigen.de/s-anzeige/x/123456789-305-2074")
    assert data["title"], f"{name}: no title parsed from a real page"
    assert data["price_eur"] is not None or "missing:price_eur" in data["parse_warnings"]


def test_negotiable_price_survives_json_ld():
    """JSON-LD reports a bare number; only the visible price says 'VB'.

    Regression: the structured-data layer used to win outright and every
    negotiable price was recorded as a fixed one.
    """
    data = parse_detail_page(_read("detail_page.html"))
    assert data["price_eur"] == 5900
    assert data["price_kind"] == "vb"


def test_price_falls_back_to_json_ld_when_markup_changes():
    html = _read("detail_page.html").replace('id="viewad-price"', 'id="changed-price"')
    data = parse_detail_page(html)
    assert data["price_eur"] == 5900


def test_page_kind_detection():
    """A results page is full of /s-anzeige/ links, so only the URL may use it."""
    from karpm.cli import _page_kind

    assert _page_kind(_read("search_page.html"), "auto", None) == "search"
    assert _page_kind(_read("detail_page.html"), "auto", None) == "detail"
    assert _page_kind("<html></html>", "auto",
                      "https://www.kleinanzeigen.de/s-anzeige/x/123-305-2074") == "detail"
    assert _page_kind(_read("detail_page.html"), "search", None) == "search"
