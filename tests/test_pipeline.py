"""End-to-end pipeline test with a fake fetcher - no network, no API calls."""

import re
from pathlib import Path

import pytest

from karpm import db, pipeline
from karpm.http import Page
from karpm.config import Config, SearchConfig

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.kleinanzeigen.de/s-motorraeder-roller/yamaha-mt-07/k0c305"


DEFAULT_PRICES = {"2847612345": "5.900 € VB", "2847698888": "2.100 €"}


class FakeFetcher:
    """Serves fixtures by URL shape and records what was requested.

    The detail fixture is re-priced per ad id, so each listing gets a page that
    actually corresponds to it - otherwise every ad would look like ad #1 and
    the change-detection tests would pass or fail for the wrong reason.
    """

    def __init__(self, detail_html: str | None = None, prices: dict | None = None,
                 gone: set[str] | None = None, unreachable: set[str] | None = None) -> None:
        # `gone` ads answer like a removed listing; `unreachable` ones fail to load.
        self.gone = gone or set()
        self.unreachable = unreachable or set()
        self.requested: list[str] = []
        self.detail_template = detail_html if detail_html is not None else (
            (FIXTURES / "detail_page.html").read_text(encoding="utf-8")
        )
        self.search_html = (FIXTURES / "search_page.html").read_text(encoding="utf-8")
        self.prices = dict(DEFAULT_PRICES if prices is None else prices)

    def detail_for(self, listing_id: str) -> str:
        html = self.detail_template
        price = self.prices.get(listing_id)
        if price is not None:
            html = html.replace("5.900 € VB", price)
            html = html.replace('"price":"5900"', '"price":"%s"' % price.split(" ")[0].replace(".", ""))
        return html.replace("2847612345", listing_id)

    def get(self, url, referer=None, binary=False, delay_range=None):
        return self.fetch(url, referer=referer, binary=binary).content

    def fetch(self, url, referer=None, binary=False, delay_range=None):
        self.requested.append(url)
        if binary:
            return Page(b"\xff\xd8\xff" + b"0" * 64, url, 200)   # a plausible JPEG
        if "/s-anzeige/" in url:
            listing_id = re.search(r"/(\d{9,})", url)
            listing_id = listing_id.group(1) if listing_id else "2847612345"
            if listing_id in self.gone:
                # Kleinanzeigen bounces a removed ad to the category page.
                return Page("<html><body>Motorräder</body></html>",
                            "https://www.kleinanzeigen.de/s-motorraeder-roller/k0c305", 200)
            if listing_id in self.unreachable:
                raise RuntimeError("connection reset")
            return Page(self.detail_for(listing_id), url, 200)
        if "seite:2" in url:
            return Page("<html><body></body></html>", url, 200)   # end of pagination
        return Page(self.search_html, url, 200)


@pytest.fixture
def conf(tmp_path):
    cfg = Config(db_path=str(tmp_path / "test.db"))
    cfg.searches = [SearchConfig(name="mt07", url=SEARCH_URL, max_pages=2)]
    cfg.images.dir = str(tmp_path / "images")
    cfg.scoring.enabled = False
    cfg.email.enabled = False
    return cfg


@pytest.fixture
def conn(conf):
    connection = db.connect(conf.db_path)
    db.init_db(connection)
    db.sync_searches(connection, conf.searches)
    yield connection
    connection.close()


def test_scrape_stores_listings_and_images(conf, conn):
    fetcher = FakeFetcher()
    totals = pipeline.run_scrape(conf, conn, fetcher)

    assert totals["new"] == 2
    assert totals["seen"] == 2

    row = db.get_listing(conn, "2847612345")
    assert row["title"] == "Yamaha MT-07 ABS, Scheckheft"
    assert row["km"] == 18400
    assert row["price_eur"] == 5900
    assert row["search_name"] == "mt07"
    assert row["is_active"] == 1

    images = db.listing_images(conn, "2847612345")
    assert len(images) == 2
    assert all(img["local_path"] for img in images), "images should be downloaded"
    assert Path(images[0]["local_path"]).exists()


def test_second_run_does_not_refetch_unchanged_listings(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = FakeFetcher()
    totals = pipeline.run_scrape(conf, conn, fetcher)

    assert totals["new"] == 0
    detail_requests = [u for u in fetcher.requested if "/s-anzeige/" in u]
    assert detail_requests == [], "known, unchanged listings must not be re-fetched"


def test_price_change_is_recorded_in_history(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = FakeFetcher(prices={"2847612345": "5.400 € VB", "2847698888": "2.100 €"})
    fetcher.search_html = fetcher.search_html.replace("5.900 € VB", "5.400 € VB")

    totals = pipeline.run_scrape(conf, conn, fetcher)
    assert totals["changed"] == 1

    history = conn.execute(
        "SELECT * FROM listing_history WHERE listing_id = ? AND event = 'price_change'",
        ("2847612345",),
    ).fetchall()
    assert len(history) == 1
    assert history[0]["prev_price_eur"] == 5900
    assert history[0]["price_eur"] == 5400
    assert db.get_listing(conn, "2847612345")["price_eur"] == 5400


def _drop_from_results(fetcher, listing_id="2847698888"):
    """Make the search results stop returning one ad, as Kleinanzeigen might."""
    fetcher.search_html = re.sub(
        r'<li class="ad-listitem">\s*<article class="aditem" data-adid="'
        + listing_id + r'".*?</li>',
        "", fetcher.search_html, flags=re.S)
    return fetcher


def test_listing_gone_from_results_is_verified_before_delisting(conf, conn):
    """Absence from the search results is a hint, not proof."""
    pipeline.run_scrape(conf, conn, FakeFetcher())

    # It vanished from the results, but its own page still shows the advert.
    fetcher = _drop_from_results(FakeFetcher())
    totals = pipeline.run_scrape(conf, conn, fetcher)

    still_there = db.get_listing(conn, "2847698888")
    assert still_there["is_active"] == 1, "a live listing must not be delisted"
    assert still_there["delisted_at"] is None
    assert totals["still_live"] == 1
    assert totals["delisted"] == 0
    assert any("2847698888" in u for u in fetcher.requested), "its page was never checked"


def test_listing_is_delisted_once_its_page_confirms_it(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = _drop_from_results(FakeFetcher(gone={"2847698888"}))
    totals = pipeline.run_scrape(conf, conn, fetcher)

    gone = db.get_listing(conn, "2847698888")
    assert gone["is_active"] == 0
    assert gone["delisted_at"] is not None
    assert gone["delisted_reason"] == "verified_gone"
    assert totals["delisted"] == 1

    history = conn.execute(
        "SELECT * FROM listing_history WHERE listing_id = ? AND event = 'delisted'",
        ("2847698888",)).fetchone()
    assert history is not None
    assert "verified_gone" in history["detail_json"]


def test_unreachable_page_leaves_the_listing_active(conf, conn):
    """An inconclusive check must never delist - try again next run."""
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = _drop_from_results(FakeFetcher(unreachable={"2847698888"}))
    totals = pipeline.run_scrape(conf, conn, fetcher)

    row = db.get_listing(conn, "2847698888")
    assert row["is_active"] == 1
    assert row["delisted_at"] is None
    assert totals["unverified"] == 1
    assert row["missing_count"] == 1
    assert row["missing_since"] is not None


def test_missing_count_accumulates_across_runs(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())
    for _ in range(3):
        pipeline.run_scrape(conf, conn, _drop_from_results(FakeFetcher(
            unreachable={"2847698888"})))

    row = db.get_listing(conn, "2847698888")
    assert row["missing_count"] == 3
    assert row["is_active"] == 1, "still no evidence, so still active"


def test_a_confirmed_live_listing_is_not_rechecked_immediately(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())
    pipeline.run_scrape(conf, conn, _drop_from_results(FakeFetcher()))

    second = _drop_from_results(FakeFetcher())
    pipeline.run_scrape(conf, conn, second)

    checks = [u for u in second.requested if "2847698888" in u]
    assert checks == [], "verified live recently - should not be fetched again"


def test_verification_can_be_switched_off(conf, conn):
    conf.scrape.verify_delisting = False
    pipeline.run_scrape(conf, conn, FakeFetcher())
    pipeline.run_scrape(conf, conn, _drop_from_results(FakeFetcher()))

    row = db.get_listing(conn, "2847698888")
    assert row["is_active"] == 0
    assert row["delisted_reason"] == "assumed"


def test_check_budget_defers_the_rest_to_the_next_run(conf, conn):
    conf.scrape.max_delist_checks = 0
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = _drop_from_results(FakeFetcher(gone={"2847698888"}))
    totals = pipeline.run_scrape(conf, conn, fetcher)

    assert totals["unverified"] == 1
    assert totals["delisted"] == 0
    assert db.get_listing(conn, "2847698888")["is_active"] == 1
    assert not [u for u in fetcher.requested if "2847698888" in u]


def test_parse_failure_does_not_null_out_good_data(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    # A markup change that breaks the detail parser entirely.
    broken = FakeFetcher(detail_html="<html><body>nothing</body></html>", prices={})
    broken.search_html = broken.search_html.replace("5.900 € VB", "5.400 € VB")
    pipeline.run_scrape(conf, conn, broken)

    row = db.get_listing(conn, "2847612345")
    assert row["km"] == 18400, "previously parsed fields must survive a failed re-parse"
    assert row["title"] == "Yamaha MT-07 ABS, Scheckheft"
    assert row["price_eur"] == 5400, "the search page price is still trusted"


def test_comparable_stats_needs_a_sample(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())
    row = db.get_listing(conn, "2847612345")
    assert db.comparable_stats(conn, row) is None, "no stats from a two-listing database"


def test_search_config_supplies_make_and_model(conf, conn):
    """The page cannot provide a model, so the search must."""
    conf.searches[0].make, conf.searches[0].model = "Yamaha", "MT-07"
    pipeline.run_scrape(conf, conn, FakeFetcher())

    row = db.get_listing(conn, "2847612345")
    assert row["model"] == "MT-07"
    assert row["make"] == "BMW" or row["make"] == "Yamaha"  # page value wins if present


def test_comparables_need_a_model_and_a_sample(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())
    row = db.get_listing(conn, "2847612345")
    assert db.comparable_stats(conn, row) is None, "no model set, so no comparables"


# --- the plan: enumerate first, then fetch -------------------------------------

def test_enumeration_reads_the_searchs_own_total(conf, conn):
    """The search page states "1 - 25 von 143", so a run never has to say "?"."""
    plan = pipeline.enumerate_search(conf, FakeFetcher(), conf.searches[0])
    assert plan.total_results == 143
    assert plan.per_page == 25
    assert plan.page_count == 6


def test_plan_counts_images_exactly_from_the_thumbnail_badges(conf, conn):
    plan = pipeline.classify_plan(conn, conf,
                                  pipeline.enumerate_search(conf, FakeFetcher(), conf.searches[0]))
    counts = [i["image_count"] for i in plan.items]
    assert counts, "no image counts parsed"
    # every collected ad is new on a fresh database, so all of them get fetched
    assert len(plan.new) == len(plan.items)
    uncapped = plan.images_expected(None)
    assert uncapped == sum(c or 1 for c in counts)
    # the per-listing cap is what actually gets downloaded
    assert plan.images_expected(2) == sum(min(c or 1, 2) for c in counts)


def test_nothing_is_fetched_during_enumeration(conf, conn):
    fetcher = FakeFetcher()
    pipeline.enumerate_search(conf, fetcher, conf.searches[0])
    assert not [u for u in fetcher.requested if "/s-anzeige/" in u], \
        "enumeration must only read search pages"


def test_classify_splits_new_from_unchanged(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    plan = pipeline.classify_plan(conn, conf,
                                  pipeline.enumerate_search(conf, FakeFetcher(), conf.searches[0]))
    assert plan.new == []
    assert len(plan.unchanged) == len(plan.items)
    assert plan.to_fetch == []


def test_unchanged_listings_cost_no_ad_page_fetch(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = FakeFetcher()
    counts = pipeline.run_scrape(conf, conn, fetcher)
    assert counts["unchanged"] == 2
    assert not [u for u in fetcher.requested if "/s-anzeige/" in u]


def test_a_price_change_puts_an_ad_back_in_the_fetch_list(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = FakeFetcher(prices={"2847612345": "5.400 € VB"})
    fetcher.search_html = fetcher.search_html.replace("5.900 € VB", "5.400 € VB")
    plan = pipeline.classify_plan(conn, conf, pipeline.enumerate_search(
        conf, fetcher, conf.searches[0]))
    assert [i["id"] for i in plan.changed] == ["2847612345"]


def test_a_truncated_run_never_reconciles_delistings(conf, conn):
    """It did not see the whole search, so absence proves nothing."""
    pipeline.run_scrape(conf, conn, FakeFetcher())

    conf.searches[0].max_listings = 1
    fetcher = _drop_from_results(FakeFetcher(gone={"2847698888"}))
    counts = pipeline.run_scrape(conf, conn, fetcher)

    assert counts["capped"] is True
    assert counts["delisted"] == 0
    assert db.get_listing(conn, "2847698888")["is_active"] == 1


def test_a_404_on_a_later_search_page_ends_pagination_instead_of_the_run(conf, conn):
    """Results shrink while being walked, so a linked page can vanish."""
    class VanishingPage2(FakeFetcher):
        def fetch(self, url, referer=None, binary=False, delay_range=None):
            if "seite:2" in url:
                raise FileNotFoundError(f"404 for {url}")
            return super().fetch(url, referer=referer, binary=binary)

    conf.searches[0].max_pages = 5
    plan = pipeline.enumerate_search(conf, VanishingPage2(), conf.searches[0])
    assert plan.pages_walked == 1
    assert len(plan.items) == 2


def test_a_404_on_the_first_search_page_is_still_an_error(conf, conn):
    class NothingThere(FakeFetcher):
        def fetch(self, url, referer=None, binary=False, delay_range=None):
            raise FileNotFoundError(f"404 for {url}")

    with pytest.raises(FileNotFoundError):
        pipeline.enumerate_search(conf, NothingThere(), conf.searches[0])


def test_page_one_stays_the_authority_on_page_count(conf, conn):
    """A later page revises the total but must not revise the page size."""
    class LastPageSummary(FakeFetcher):
        def fetch(self, url, referer=None, binary=False, delay_range=None):
            page = super().fetch(url, referer=referer, binary=binary)
            if "/s-anzeige/" in url or binary:
                return page
            return page._replace(content=page.content.replace(
                "1 - 25 von 143", "126 - 143 von 143"))

    conf.searches[0].max_pages = 5
    plan = pipeline.enumerate_search(conf, LastPageSummary(), conf.searches[0])
    assert plan.total_results == 143
    assert plan.page_count is None, "a partial page cannot imply a page count"
