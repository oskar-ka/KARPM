"""Email rendering and scoring-prompt construction, without sending or calling."""

import json

import pytest

from karpm import db, mailer, pipeline, scoring
from karpm.config import Config, SearchConfig
from tests.test_pipeline import FakeFetcher, SEARCH_URL


@pytest.fixture
def populated(tmp_path):
    conf = Config(db_path=str(tmp_path / "test.db"))
    conf.searches = [SearchConfig(name="mt07", url=SEARCH_URL, max_pages=2)]
    conf.images.dir = str(tmp_path / "images")
    conf.scoring.enabled = False
    conf.email.enabled = False
    conn = db.connect(conf.db_path)
    db.init_db(conn)
    db.sync_searches(conn, conf.searches)
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.add_score(conn, "2847612345", {
        "model": "claude-opus-5", "prompt_version": "v1", "content_hash": "abc",
        "overall": 5, "fit": 5, "value": 4, "fair_price_eur": 6800,
        "headline": "Clean, low-mileage MT-07 with full service history",
        "reasoning": "Well below comparable asking prices for the mileage.",
        "pros": ["Scheckheft lückenlos", "Fresh HU until 06/2027"],
        "cons": ["No mention of chain condition"],
        "red_flags": [],
    })
    conn.commit()
    yield conf, conn
    conn.close()


def test_digest_candidates_respects_cutoff(populated):
    conf, conn = populated
    conf.email.digest_min_score = 3
    assert [r["id"] for r in mailer.digest_candidates(conn, conf.email)] == ["2847612345"]

    conf.email.digest_min_score = 5
    assert len(mailer.digest_candidates(conn, conf.email)) == 1
    conf.email.digest_min_score = 6
    assert mailer.digest_candidates(conn, conf.email) == []


def test_digest_html_contains_the_listing(populated, monkeypatch):
    conf, conn = populated
    conf.email.enabled = True
    conf.email.to_addresses = ["me@example.com"]
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"id": "email_123"}

        return Response()

    monkeypatch.setattr(mailer.requests, "post", fake_post)
    provider_id = mailer.send_digest(conn, conf.email, "re_test")

    assert provider_id == "email_123"
    assert "5/5" in captured["html"]
    assert "Yamaha MT-07 ABS" in captured["html"]
    assert "5.900 €" in captured["html"]
    assert "900 € under estimated fair price" in captured["html"]
    assert "Scheckheft lückenlos" in captured["html"]
    assert "kleinanzeigen.de/s-anzeige" in captured["html"]
    assert "Yamaha MT-07 ABS" in captured["text"], "a plain-text part is always included"


def test_digest_is_not_sent_twice_for_the_same_listing(populated, monkeypatch):
    conf, conn = populated
    conf.email.enabled = True
    conf.email.to_addresses = ["me@example.com"]
    monkeypatch.setattr(mailer, "send", lambda *a, **k: "email_1")

    mailer.send_digest(conn, conf.email, "re_test")
    assert mailer.digest_candidates(conn, conf.email) == []


def test_instant_alert_threshold():
    cfg = Config().email
    cfg.instant_min_score = 5
    cfg.instant_min_bargain_pct = 20

    assert mailer.qualifies_for_instant(cfg, {"overall": 5, "fair_price_eur": None}, 5000)
    assert not mailer.qualifies_for_instant(cfg, {"overall": 3, "fair_price_eur": 9000}, 5000)
    # A 4 that is 30% under the fair price still earns an interrupt.
    assert mailer.qualifies_for_instant(cfg, {"overall": 4, "fair_price_eur": 7000}, 4900)
    assert not mailer.qualifies_for_instant(cfg, {"overall": 4, "fair_price_eur": 7000}, 6500)


def test_scoring_prompt_includes_the_facts_that_matter(populated):
    conf, conn = populated
    row = db.get_listing(conn, "2847612345")
    text = scoring.listing_to_text(row, comparables=None)

    assert "Yamaha MT-07 ABS" in text
    assert "5.900 EUR (VB - negotiable)" in text
    assert "18.400 km" in text
    assert "75 PS" in text
    assert "2019-05-01" in text
    assert "Scheckheft lückenlos" in text, "the full description must be sent verbatim"
    assert "No comparable listings" in text


def test_scoring_prompt_includes_comparables_when_available(populated):
    conf, conn = populated
    row = db.get_listing(conn, "2847612345")
    text = scoring.listing_to_text(row, comparables={
        "sample_size": 42, "price_p25": 5200, "price_median": 6100,
        "price_p75": 6900, "km_median": 21000,
    })
    assert "n=42" in text
    assert "median 6100 EUR" in text
    assert "median mileage 21000 km" in text


def test_score_row_roundtrips_through_the_database(populated):
    conf, conn = populated
    row = conn.execute("SELECT * FROM listing_current WHERE id = ?", ("2847612345",)).fetchone()
    assert row["overall"] == 5
    assert json.loads(row["pros_json"])[0] == "Scheckheft lückenlos"
