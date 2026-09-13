"""Tests for `karpm trial` - the dry run that stops before scoring."""

import re
from pathlib import Path

import pytest

from karpm import db, scoring, trial
from karpm.config import Config, SearchConfig

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.kleinanzeigen.de/s-motorraeder-roller/bmw/k0c305"

DETAILS = {
    "3422210980": "live_detail_bmw_fixed.html",
    "3511013874": "live_detail_bmw_vb.html",
}


class RealPageFetcher:
    """Serves the captured search page, with its first two ads rewired to the
    two captured ad pages."""

    def __init__(self, broken: set[str] | None = None) -> None:
        self.broken = broken or set()
        self.requested: list[str] = []
        search = (FIXTURES / "live_search_astro.html").read_text(encoding="utf-8")
        for old, new in zip(
            re.findall(r'data-adid="(\d+)"', search)[:2], DETAILS
        ):
            search = search.replace(old, new)
        self.search_html = search

    def get(self, url, referer=None, binary=False):
        self.requested.append(url)
        if binary:
            return b"\xff\xd8\xff" + b"0" * 64
        match = re.search(r"(\d{9,})", url)
        listing_id = match.group(1) if match else None
        if listing_id in DETAILS:
            if listing_id in self.broken:
                return "<html><body>nothing</body></html>"
            return (FIXTURES / DETAILS[listing_id]).read_text(encoding="utf-8")
        return self.search_html


@pytest.fixture
def conf(tmp_path):
    cfg = Config(db_path=str(tmp_path / "trial.db"))
    cfg.images.dir = str(tmp_path / "images")
    cfg.scoring.enabled = False
    cfg.email.enabled = False
    return cfg


@pytest.fixture
def conn(conf):
    connection = db.connect(conf.db_path)
    db.init_db(connection)
    yield connection
    connection.close()


def search(**kwargs):
    defaults = dict(name="trial", url=SEARCH_URL, max_pages=1, max_listings=2,
                    make="BMW", model="R 1200 GS")
    return SearchConfig(**{**defaults, **kwargs})


def test_trial_parses_real_pages_and_reports_success(conf, conn):
    report = trial.run_trial(conf, conn, search(), RealPageFetcher())

    assert report.counts["seen"] == 2
    assert report.counts["new"] == 2
    assert report.ok, report.coverage

    by_id = {r["id"]: r for r in report.rows}
    assert by_id["3422210980"]["price_eur"] == 4000
    assert by_id["3422210980"]["km"] == 66976
    assert by_id["3422210980"]["inspection_until"] == "2028-09-01"
    assert by_id["3511013874"]["price_kind"] == "vb"
    # model cannot be parsed off the page, so the search must supply it
    assert by_id["3422210980"]["model"] == "R 1200 GS"


def test_limit_caps_the_run(conf, conn):
    report = trial.run_trial(conf, conn, search(max_listings=1), RealPageFetcher())
    assert report.counts["seen"] == 1
    assert report.counts["capped"] is True
    assert len(report.rows) == 1


def test_capped_run_never_marks_anything_delisted(conf, conn):
    """It only looked at part of the search, so absence proves nothing."""
    trial.run_trial(conf, conn, search(), RealPageFetcher())
    trial.run_trial(conf, conn, search(max_listings=1), RealPageFetcher())

    delisted = conn.execute("SELECT COUNT(*) n FROM listings WHERE is_active = 0").fetchone()["n"]
    assert delisted == 0
    events = [r["event"] for r in conn.execute("SELECT event FROM listing_history")]
    assert "delisted" not in events


def test_images_are_downloaded_to_disk(conf, conn):
    report = trial.run_trial(conf, conn, search(), RealPageFetcher())

    assert report.image_stats["urls"] == 20        # 12 (capped) + 8
    assert report.image_stats["downloaded"] == 20
    assert report.image_stats["failed"] == 0
    files = list(Path(conf.images.dir).rglob("*.jpg"))
    assert len(files) == 20
    assert all(f.stat().st_size > 0 for f in files)


def test_no_images_flag_skips_downloads(conf, conn):
    report = trial.run_trial(conf, conn, search(), RealPageFetcher(), download_images=False)
    assert report.image_stats["downloaded"] == 0
    assert report.image_stats["failed"] == 0, "skipping is not failing"
    assert not Path(conf.images.dir).exists() or not list(Path(conf.images.dir).rglob("*.jpg"))


def test_a_broken_detail_page_fails_the_trial(conf, conn):
    report = trial.run_trial(conf, conn, search(), RealPageFetcher(broken={"3511013874"}))

    assert not report.ok
    assert report.coverage["km"]["missing"] == 1
    assert report.coverage["km"]["missing_ids"] == ["3511013874"]
    assert any(w["id"] == "3511013874" for w in report.warnings)

    rendered = trial.render(report)
    assert "MISSING" in rendered
    assert "VERDICT: problems above" in rendered


def test_missing_optional_field_does_not_fail_the_trial(conf, conn):
    """Motorcycle ads never state previous owners; that is not a parser fault."""
    report = trial.run_trial(conf, conn, search(), RealPageFetcher())
    assert report.coverage["owners"]["missing"] == 2
    assert report.coverage["owners"]["required"] is False
    assert report.ok


def test_trial_never_scores_or_mails(conf, conn, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a trial run must not score or send mail")

    monkeypatch.setattr(scoring, "score_pending", explode)
    monkeypatch.setattr("karpm.mailer.send", explode)

    report = trial.run_trial(conf, conn, search(), RealPageFetcher())
    assert report.ok
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"] == 0


def test_render_includes_the_headline_numbers(conf, conn):
    report = trial.run_trial(conf, conn, search(), RealPageFetcher())
    rendered = trial.render(report)

    assert "3422210980" in rendered
    assert "66976" in rendered
    assert "FIELD COVERAGE" in rendered
    assert "Scoring and email were not run" in rendered
    assert "VERDICT: looks good" in rendered
