"""The new Astro ad page.

Kleinanzeigen is migrating ad pages to an Astro app. Those pages ship a single
`<img>` and hand the gallery to a hydration payload for JavaScript to render,
so a scraper reading the markup finds one photo where the ad has thirteen -
and nothing about that looks like an error.

The fixture is a real such page, trimmed to the one island that carries the
ad's data.
"""

from pathlib import Path

import pytest

from karpm.parse.detail import astro_ad_data, image_sources, parse_detail_page

FIXTURES = Path(__file__).parent / "fixtures"
ASTRO = "live_detail_astro.html"
URL = "https://www.kleinanzeigen.de/s-anzeige/bmw-r-1200-gs/3508662191-305-7402"


@pytest.fixture(scope="module")
def page():
    return (FIXTURES / ASTRO).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed(page):
    return parse_detail_page(page, URL)


def test_the_markup_really_does_only_hold_one_photo(page):
    """Establishes that this is not a parser fault: the page genuinely ships
    one image and no JSON-LD gallery at all."""
    sources = image_sources(page)
    assert sources["json-ld ImageObject"] == 0
    assert sources["selector .galleryimage-element img"] == 0
    assert sources["selector #viewad-image"] == 1


def test_the_gallery_comes_from_the_hydration_payload(page, parsed):
    assert image_sources(page)["astro hydration payload"] == 13
    assert len(parsed["image_urls"]) == 13
    assert len({u.partition("?")[0] for u in parsed["image_urls"]}) == 13


def test_the_largest_rendition_is_taken(parsed):
    """Each entry offers thumbnail through xxLarge; the biggest is the point."""
    assert all("rule=$_57" in u or "rule=$_59" in u for u in parsed["image_urls"]), \
        parsed["image_urls"][0]


def test_the_ads_own_fields_still_parse(parsed):
    assert parsed["id"] == "3508662191"
    assert parsed["title"] == "BMW R 1200 GS"
    assert parsed["price_eur"] == 3990
    assert parsed["km"] == 56000
    assert parsed["hp"] == 98
    assert parsed["ccm"] == 1200
    assert parsed["first_reg_year"] == 2004
    assert parsed["description"]
    assert parsed["parse_warnings"] == []


def test_the_posting_date_comes_from_the_payload(parsed):
    """These pages carry no #viewad-extra-info date for the markup parser."""
    assert parsed["posted_at"].startswith("2026-09-10")


def test_the_seller_is_stated_rather_than_guessed(parsed):
    """userDetails.commercial is a boolean; the old route scanned the first
    6000 characters of page text for the word "gewerblich"."""
    assert parsed["seller_type"] == "commercial"
    assert parsed["seller_id"] == "46292366"
    assert parsed["seller_name"] == "Behl Motorräder"


def test_attributes_come_through(parsed):
    attrs = parsed["attributes_json"]
    assert attrs["Kilometerstand"] == "56.000 km"
    assert attrs["Marke"] == "BMW"
    assert all("\n" not in key for key in attrs)


def test_an_old_stack_page_is_unaffected():
    """Both page shapes are live at once, so the old route has to keep working."""
    old = (FIXTURES / "live_detail_bmw_fixed.html").read_text(encoding="utf-8")
    assert astro_ad_data.__module__            # imported
    data = parse_detail_page(old, "https://www.kleinanzeigen.de/s-anzeige/x/3422210980-305-1")
    assert len(data["image_urls"]) == 13
    assert data["seller_type"] == "private"


def test_a_page_with_no_payload_does_not_crash():
    assert parse_detail_page("<html><body>nothing</body></html>")["image_urls"] == []
