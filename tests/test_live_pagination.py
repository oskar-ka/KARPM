"""Pagination against real captured pages.

Fixtures are pages 1, 2 and the last page of a real BMW R1200GS search in
Bayern (2026-09-13). Pagination is the part of the scraper where a wrong answer
is expensive: stop too early and ads are silently missed, fail to stop and the
run walks forever.
"""

from pathlib import Path

import pytest

from karpm import pipeline
from karpm.config import Config, SearchConfig
from karpm.http import Page
from karpm.parse.search import parse_search_page

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = "https://www.kleinanzeigen.de/s-motorraeder-roller/bayern/bmw-r1200gs/k0c305l5510"


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class PaginatedFetcher:
    """Serves the three captured pages; any other page is empty."""

    def __init__(self):
        self.requested = []

    def get(self, url, referer=None, binary=False, delay_range=None):
        return self.fetch(url, referer=referer, binary=binary).content

    def fetch(self, url, referer=None, binary=False, delay_range=None):
        self.requested.append(url)
        if "seite:2" in url:
            return Page(read("live_search_p2.html"), url, 200)
        if "seite:3" in url:
            # stand in for pages 3-11, then hand over to the real last page
            return Page(read("live_search_plast.html"), url, 200)
        return Page(read("live_search_p1.html"), url, 200)


def test_page_one_reports_the_search_size():
    result = parse_search_page(read("live_search_p1.html"))
    assert result["total_results"] == 280
    assert result["per_page"] == 25
    assert result["page_count"] == 12
    assert result["next_url"].endswith("/seite:2/bmw-r1200gs/k0c305l5510")


def test_a_middle_page_continues_the_sequence():
    result = parse_search_page(read("live_search_p2.html"))
    assert result["total_results"] == 280
    assert result["per_page"] == 25
    assert result["next_url"].endswith("/seite:3/bmw-r1200gs/k0c305l5510")


def test_the_last_page_has_no_next_link():
    """This is the only thing that stops the walk."""
    result = parse_search_page(read("live_search_plast.html"))
    assert result["next_url"] is None


def test_the_last_pages_partial_range_reports_no_page_size():
    """It reads "276 - 280 von 280": 5 ads, not a page size of 5."""
    result = parse_search_page(read("live_search_plast.html"))
    assert result["total_results"] == 280
    assert result["per_page"] is None
    assert result["page_count"] is None


def test_page_one_carries_a_paid_top_placement():
    """Which is why page one holds 26 ads when the page size is 25."""
    items = parse_search_page(read("live_search_p1.html"))["items"]
    assert items[0]["is_promoted"] is True
    assert items[0]["is_commercial"] is True
    assert not any(i["is_promoted"] for i in items[1:])


def test_every_page_yields_usable_ads():
    for name in ("live_search_p1.html", "live_search_p2.html", "live_search_plast.html"):
        for item in parse_search_page(read(name))["items"]:
            assert item["id"].isdigit()
            assert item["title"]
            assert item["price_eur"] is not None, f"{name}: {item['id']} has no price"


def test_image_counts_are_read_from_the_thumbnails():
    items = parse_search_page(read("live_search_p1.html"))["items"]
    counts = [i["image_count"] for i in items]
    assert any(c and c > 1 for c in counts), f"no photo counts parsed: {counts}"


# --- walking the whole thing --------------------------------------------------

@pytest.fixture
def conf():
    cfg = Config()
    cfg.searches = [SearchConfig(name="bmw", url=FIRST, max_pages=None,
                                 make="BMW", model="R 1200 GS")]
    return cfg


def test_the_walk_stops_at_the_last_page(conf):
    plan = pipeline.enumerate_search(conf, PaginatedFetcher(), conf.searches[0])
    assert plan.pages_walked == 3, "should stop when a page offers no next link"
    assert plan.truncated is False
    assert plan.total_results == 280
    assert plan.page_count == 12


def test_the_walk_deduplicates_a_promoted_ad_seen_twice(conf):
    """A TOP placement on page one reappears organically further in; counting
    it twice would fetch it twice and inflate every total."""
    class RepeatsTheTopAd(PaginatedFetcher):
        def fetch(self, url, referer=None, binary=False, delay_range=None):
            page = super().fetch(url, referer=referer, binary=binary)
            if "seite:2" in url:
                # page 2 now also offers the ad that was promoted on page 1
                promoted = parse_search_page(read("live_search_p1.html"))["items"][0]["id"]
                first_on_p2 = parse_search_page(page.content)["items"][0]["id"]
                return page._replace(content=page.content.replace(first_on_p2, promoted))
            return page

    plan = pipeline.enumerate_search(conf, RepeatsTheTopAd(), conf.searches[0])
    ids = [i["id"] for i in plan.items]
    assert len(ids) == len(set(ids)), "the same ad was collected twice"
    assert plan.duplicates == 1


def test_page_one_stays_the_authority_on_page_size(conf):
    """The last page cannot report a page size, and must not erase page one's."""
    plan = pipeline.enumerate_search(conf, PaginatedFetcher(), conf.searches[0])
    assert plan.per_page == 25
    assert plan.page_count == 12


def test_max_pages_marks_the_run_truncated(conf):
    conf.searches[0].max_pages = 2
    plan = pipeline.enumerate_search(conf, PaginatedFetcher(), conf.searches[0])
    assert plan.pages_walked == 2
    assert plan.truncated is True
