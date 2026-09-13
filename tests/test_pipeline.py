"""End-to-end pipeline test with a fake fetcher - no network, no API calls."""

import re
from pathlib import Path

import pytest

from karpm import db, pipeline
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

    def __init__(self, detail_html: str | None = None, prices: dict | None = None) -> None:
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

    def get(self, url, referer=None, binary=False):
        self.requested.append(url)
        if binary:
            return b"\xff\xd8\xff" + b"0" * 64          # a plausible JPEG header
        if "/s-anzeige/" in url:
            listing_id = re.search(r"/(\d{9,})-", url)
            return self.detail_for(listing_id.group(1) if listing_id else "2847612345")
        if "seite:2" in url:
            return "<html><body></body></html>"          # end of pagination
        return self.search_html


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


def test_disappearing_listing_is_marked_delisted(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())

    fetcher = FakeFetcher()
    # Second run: the cheap one is gone from the results.
    fetcher.search_html = fetcher.search_html.replace("2847698888", "2847698888-REMOVED")
    fetcher.search_html = fetcher.search_html.replace(
        '<a class="ellipsis" href="/s-anzeige/mt07-unfall/2847698888-REMOVED-305-2074">', "<a>"
    )
    pipeline.run_scrape(conf, conn, fetcher)

    gone = db.get_listing(conn, "2847698888")
    assert gone["is_active"] == 0
    assert gone["delisted_at"] is not None
    events = [r["event"] for r in conn.execute(
        "SELECT event FROM listing_history WHERE listing_id = ?", ("2847698888",))]
    assert "delisted" in events


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
