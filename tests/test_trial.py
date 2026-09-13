"""Tests for `karpm trial` - the dry run that stops before scoring."""

import re
from pathlib import Path

import pytest

from karpm import db, scoring, trial
from karpm.config import Config, SearchConfig
from karpm.http import Page

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

    def get(self, url, referer=None, binary=False, delay_range=None):
        return self.fetch(url, referer=referer, binary=binary).content

    def fetch(self, url, referer=None, binary=False, delay_range=None):
        self.requested.append(url)
        if binary:
            return Page(b"\xff\xd8\xff" + b"0" * 64, url, 200)
        match = re.search(r"(\d{9,})", url)
        listing_id = match.group(1) if match else None
        if listing_id in DETAILS:
            if listing_id in self.broken:
                return Page("<html><body>nothing</body></html>", url, 200)
            return Page((FIXTURES / DETAILS[listing_id]).read_text(encoding="utf-8"), url, 200)
        return Page(self.search_html, url, 200)


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
    defaults = dict(name="trial", url=SEARCH_URL, max_ads=2,
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
    report = trial.run_trial(conf, conn, search(max_ads=1), RealPageFetcher())
    assert report.counts["seen"] == 1
    assert report.counts["capped"] is True
    assert len(report.rows) == 1


def test_capped_run_never_marks_anything_delisted(conf, conn):
    """It only looked at part of the search, so absence proves nothing."""
    trial.run_trial(conf, conn, search(), RealPageFetcher())
    trial.run_trial(conf, conn, search(max_ads=1), RealPageFetcher())

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


def test_images_use_the_cdn_pace_not_the_search_pace(conf, conn):
    """Images are ~90% of a run's requests; pacing them like search queries
    turned a five-listing trial into a seven-minute silent wait."""
    seen = []

    class PaceRecordingFetcher(RealPageFetcher):
        def get(self, url, referer=None, binary=False, delay_range=None):
            seen.append((url, delay_range))
            return super().get(url, referer=referer, binary=binary)

    trial.run_trial(conf, conn, search(), PaceRecordingFetcher())

    image_calls = [d for u, d in seen if "img.kleinanzeigen.de" in u]
    assert image_calls, "no images were fetched"
    assert all(d == conf.scrape.image_delay_range for d in image_calls)
    assert conf.scrape.image_delay_range[1] < conf.scrape.min_delay_s


# --- image renditions ----------------------------------------------------------

def test_candidate_urls_covers_the_other_renditions():
    from karpm.images import candidate_urls
    got = candidate_urls("https://img.kleinanzeigen.de/x/abc?rule=$_59.AUTO")
    assert got[0] == "https://img.kleinanzeigen.de/x/abc?rule=$_59.AUTO", "the linked one first"
    assert "https://img.kleinanzeigen.de/x/abc?rule=$_59.JPG" in got
    assert got[-1] == "https://img.kleinanzeigen.de/x/abc", "bare URL is the last resort"
    assert len(got) == len(set(got)), "no repeats"


def test_candidate_urls_leaves_a_ruleless_url_alone():
    from karpm.images import candidate_urls
    assert candidate_urls("https://img.kleinanzeigen.de/x/abc") == \
        ["https://img.kleinanzeigen.de/x/abc"]


def test_a_missing_rendition_falls_back_instead_of_losing_the_photo(conf, conn):
    """The gallery links $_59.AUTO but that rendition does not always exist;
    the same photo is usually there as $_59.JPG."""
    class AutoRenditionGone(RealPageFetcher):
        def __init__(self):
            super().__init__()
            self.image_requests = []

        def get(self, url, referer=None, binary=False, delay_range=None):
            if binary:
                self.image_requests.append(url)
                if "$_59.AUTO" in url:
                    raise FileNotFoundError(f"404 for {url}")
                return b"\xff\xd8\xff" + b"0" * 64
            return super().get(url, referer=referer, binary=binary)

    fetcher = AutoRenditionGone()
    report = trial.run_trial(conf, conn, search(), fetcher)

    assert report.image_stats["failed"] == 0, "every photo should have been recovered"
    assert report.image_stats["downloaded"] == 20
    assert any("$_59.AUTO" in u for u in fetcher.image_requests)
    assert any("$_59.JPG" in u for u in fetcher.image_requests)


def test_a_photo_with_no_working_rendition_is_reported_with_its_ad(conf, conn, caplog):
    import logging

    class EveryRenditionGone(RealPageFetcher):
        def get(self, url, referer=None, binary=False, delay_range=None):
            if binary:
                raise FileNotFoundError(f"404 for {url}")
            return super().get(url, referer=referer, binary=binary)

    with caplog.at_level(logging.WARNING):
        report = trial.run_trial(conf, conn, search(), EveryRenditionGone())

    assert report.image_stats["failed"] == 20
    assert "no rendition of this photo exists" in caplog.text
    assert "kleinanzeigen.de/s-anzeige/" in caplog.text, "the ad URL must be named"


def test_image_requests_ask_for_images(conf, conn):
    """.AUTO renditions negotiate on Accept, so the header has to say image."""
    from karpm.config import ScrapeConfig
    from karpm.http import Fetcher

    sent = {}

    class Recorder:
        def get(self, url, headers=None, timeout=None, allow_redirects=True):
            sent.update(headers or {})
            class R:
                status_code = 200
                content = b"\xff\xd8\xff"
                url = "https://img.example/x"
                headers = {"Content-Type": "image/jpeg"}
                encoding = None
            return R()

    fetcher = Fetcher(ScrapeConfig(min_delay_s=0, max_delay_s=0))
    fetcher.session = Recorder()
    fetcher.get("https://img.example/x", binary=True)
    assert sent["Accept"].startswith("image/")


def test_a_working_rendition_is_remembered_for_the_rest_of_the_run(conf, conn):
    """Otherwise a CDN that has dropped one rendition costs a wasted request on
    every single photo."""
    class AutoAlwaysGone(RealPageFetcher):
        def __init__(self):
            super().__init__()
            self.image_requests = []

        def get(self, url, referer=None, binary=False, delay_range=None):
            if binary:
                self.image_requests.append(url)
                if "$_59.AUTO" in url:
                    raise FileNotFoundError(f"404 for {url}")
                return b"\xff\xd8\xff" + b"0" * 64
            return super().get(url, referer=referer, binary=binary)

    fetcher = AutoAlwaysGone()
    report = trial.run_trial(conf, conn, search(), fetcher)

    assert report.image_stats["downloaded"] == 20
    wasted = [u for u in fetcher.image_requests if "$_59.AUTO" in u]
    assert len(wasted) == 1, f"the dead rendition was retried {len(wasted)} times"


def test_preferred_rendition_is_tried_first():
    from karpm.images import candidate_urls
    got = candidate_urls("https://img.example/x?rule=$_59.AUTO", preferred="$_59.JPG")
    assert got[0] == "https://img.example/x?rule=$_59.JPG"
    assert "https://img.example/x?rule=$_59.AUTO" in got
    assert len(got) == len(set(got))


# --- broken ads must not cost a fortune ----------------------------------------

def test_a_permanent_error_is_not_retried(conf, conn):
    """A 400 is the server saying the request is wrong; it will say so again.
    Retrying it four times with backoff cost 75 seconds per photo."""
    import time

    from karpm.config import ScrapeConfig
    from karpm.http import Fetcher, PermanentError

    class AlwaysBadRequest:
        calls = 0

        def get(self, url, headers=None, timeout=None, allow_redirects=True):
            AlwaysBadRequest.calls += 1
            class R:
                status_code = 400
                headers: dict = {}
                url = "u"
                content = b""
                encoding = None
            return R()

    fetcher = Fetcher(ScrapeConfig(min_delay_s=0, max_delay_s=0,
                                   retry_delays_s=[30, 30, 30, 30]))
    fetcher.session = AlwaysBadRequest()
    started = time.monotonic()
    with pytest.raises(PermanentError):
        fetcher.get("https://img.example/broken", binary=True)
    assert time.monotonic() - started < 1, "a permanent error must not sleep"
    assert AlwaysBadRequest.calls == 1, "and must not be retried"


def test_an_ad_with_broken_photos_is_abandoned_early(conf, conn, caplog):
    """All twenty photos of a broken ad are broken; grinding through them
    costs five requests each and returns nothing."""
    import logging

    class EveryPhotoBroken(RealPageFetcher):
        def __init__(self):
            super().__init__()
            self.image_requests = []

        def get(self, url, referer=None, binary=False, delay_range=None):
            if binary:
                self.image_requests.append(url)
                from karpm.http import PermanentError
                raise PermanentError(400, url)
            return super().get(url, referer=referer, binary=binary)

    conf.images.give_up_after_failures = 3
    fetcher = EveryPhotoBroken()
    with caplog.at_level(logging.WARNING):
        report = trial.run_trial(conf, conn, search(), fetcher)

    # 12 photos on the first ad, 8 on the second; each abandoned after 3
    assert report.image_stats["downloaded"] == 0
    attempts_per_photo = len(candidate_urls_for_a_photo())
    assert len(fetcher.image_requests) <= 6 * attempts_per_photo, \
        f"kept trying: {len(fetcher.image_requests)} requests"
    assert "giving up on this ad's photos" in caplog.text


def candidate_urls_for_a_photo():
    from karpm.images import candidate_urls
    return candidate_urls("https://img.kleinanzeigen.de/x/abc?rule=$_59.AUTO")


def test_every_pending_image_is_downloaded_not_just_the_first_500(conf, conn):
    """The 500 default was invisible and silently truncated big runs."""
    from karpm import db as dbmod
    pipeline_rows = 0
    trial.run_trial(conf, conn, search(), RealPageFetcher())
    # queue more images than the old default allowed
    for n in range(600):
        dbmod.add_image(conn, "3422210980", 100 + n, f"https://img.example/{n}.jpg")
        pipeline_rows += 1
    conn.commit()
    assert len(dbmod.pending_images(conn)) == 600, "no implicit cap on the query"
    assert len(dbmod.pending_images(conn, limit=10)) == 10


def test_image_folders_carry_a_note_naming_their_ad(conf, conn):
    report = trial.run_trial(conf, conn, search(), RealPageFetcher())
    assert report.image_stats["downloaded"] > 0

    folder = Path(conf.images.dir) / "3422210980"
    note = folder / "ad.txt"
    assert note.exists(), "an image folder should say which ad it belongs to"
    text = note.read_text(encoding="utf-8")
    assert "3422210980" in text
    assert "kleinanzeigen.de/s-anzeige/" in text
