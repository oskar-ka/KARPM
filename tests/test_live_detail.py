"""Detail-page parsing against real Kleinanzeigen ad pages.

Both fixtures are trimmed captures of live BMW R 1200 GS ads (2026-09-13).
Unlike the search results, ad pages are still served by the older stack, so the
`#viewad-*` selectors still apply - but the details list has moved on, which is
what these tests pin down.
"""

from pathlib import Path

import pytest

from karpm.parse.detail import parse_detail_page

FIXTURES = Path(__file__).parent / "fixtures"

FIXED = ("live_detail_bmw_fixed.html",
         "https://www.kleinanzeigen.de/s-anzeige/bmw-r-1200-gs/3422210980-305-16917")
VB = ("live_detail_bmw_vb.html",
      "https://www.kleinanzeigen.de/s-anzeige/bmw-r-1200-gs-ez-11-2005-im-topzustand-mit-koffer"
      "/3511013874-305-7321")


def parse(case):
    name, url = case
    return parse_detail_page((FIXTURES / name).read_text(encoding="utf-8"), url)


@pytest.fixture(scope="module")
def fixed():
    return parse(FIXED)


@pytest.fixture(scope="module")
def vb():
    return parse(VB)


def test_core_fields(fixed):
    assert fixed["id"] == "3422210980"
    assert fixed["title"] == "BMW R 1200 GS"
    assert fixed["price_eur"] == 4000
    assert fixed["price_kind"] == "fixed"
    assert fixed["km"] == 66976
    assert fixed["hp"] == 98
    assert fixed["ccm"] == 1170
    assert fixed["make"] == "BMW"
    assert fixed["first_reg_date"] == "2004-06-01"
    assert fixed["first_reg_year"] == 2004
    assert fixed["postcode"] == "88175"
    assert fixed["seller_type"] == "private"
    assert fixed["posted_at"].startswith("2026-08-16")
    assert fixed["parse_warnings"] == []


def test_negotiable_price(vb):
    assert vb["price_eur"] == 4750
    assert vb["price_kind"] == "vb"
    assert vb["km"] == 60958
    assert vb["first_reg_date"] == "2005-11-01"


def test_inspection_date_is_extracted_from_hu_bis(fixed, vb):
    """The label reads "HU bis September 2028" - a trailing "bis" used to make
    the attribute unmappable, so TÜV validity was silently lost."""
    assert fixed["inspection_until"] == "2028-09-01"
    assert vb["inspection_until"] == "2027-05-01"


def test_raw_attributes_are_clean(fixed):
    """The chip selectors also match the details rows; re-entering them as
    booleans used to double the block and pollute the scoring prompt."""
    attrs = fixed["attributes_json"]
    assert attrs["Kilometerstand"] == "66.976 km"
    assert attrs["HU bis"] == "September 2028"
    assert attrs["Getriebe"] == "Manuell"
    assert all(value != "ja" for value in attrs.values()), f"junk boolean tags: {attrs}"
    assert not any("\n" in key for key in attrs), "a details row leaked in as a tag"
    assert len(attrs) == 8


def test_description_is_captured_verbatim_with_umlauts(fixed):
    description = fixed["description"]
    assert "Verkaufe wegen Neuanschaffung meine geliebte GS." in description
    assert "durchgeführte" in description       # umlauts intact
    assert "unfallfrei" in description
    assert len(description) > 400, "the full advert text must reach the prompt"


def test_images_come_from_the_gallery_not_the_similar_ads(fixed):
    """The page carries a 'similar ads' block full of other bikes' photos."""
    urls = fixed["image_urls"]
    assert len(urls) == 13
    assert all(u.startswith("https://img.kleinanzeigen.de/api/v1/prod-ads/images/") for u in urls)
    assert len(set(urls)) == len(urls), "duplicate image urls"


def test_seller_and_location(fixed):
    assert fixed["seller_name"] == "Neumann"
    assert "Scheidegg" in fixed["location"]


@pytest.mark.parametrize("case", [FIXED, VB])
def test_nothing_important_is_missing(case):
    data = parse(case)
    for field in ("id", "title", "price_eur", "description", "km", "first_reg_year", "hp"):
        assert data[field] not in (None, ""), f"{field} did not parse"
    assert data["image_urls"], "no images found"


def test_ads_do_not_carry_a_model_attribute(fixed):
    """Kleinanzeigen lists "Marke" but not "Modell" for motorcycles, which is
    why SearchConfig has to declare the model - without it, listings cannot be
    grouped into price comparables."""
    assert fixed["make"] == "BMW"
    assert fixed.get("model") is None
    assert "Modell" not in fixed["attributes_json"]
