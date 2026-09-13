"""The three staleness flags.

A listing can go out of date in ways the ad itself never reveals: the parser
changed, preferences.md changed, or you looked at it and decided it is not for
you. None of those show up as a price drop or an edit, so each is recorded
against the listing instead of being left to be noticed.
"""

import pytest

from karpm import db, mailer, pipeline
from karpm.config import Config, EmailConfig, SearchConfig
from tests.test_pipeline import FakeFetcher, SEARCH_URL

SCORE = {"model": "claude-opus-5", "prompt_version": "v1", "overall": 5, "fit": 5,
         "value": 5, "fair_price_eur": 9000, "headline": "h", "reasoning": "r",
         "pros": [], "cons": [], "red_flags": []}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    db.init_db(connection)
    db.upsert_listing(connection, {
        "id": "111", "url": "https://x/111", "title": "BMW R 1200 GS",
        "description": "clean", "price_eur": 5900, "search_name": "gs"})
    connection.commit()
    yield connection
    connection.close()


def flags(conn, listing_id="111"):
    row = conn.execute("SELECT needs_refetch, needs_rescore, ignored, parser_version "
                       "FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return dict(row)


def score_it(conn, listing_id="111"):
    row = conn.execute("SELECT content_hash FROM listings WHERE id = ?",
                       (listing_id,)).fetchone()
    db.add_score(conn, listing_id, {**SCORE, "content_hash": row["content_hash"]})
    conn.commit()


def queued(conn):
    return [r["id"] for r in db.unscored_listings(conn, True, "v1", 50)]


# --- a fresh row ---------------------------------------------------------

def test_a_new_listing_is_stamped_with_the_parser_version(conn):
    assert flags(conn)["parser_version"] == db.PARSER_VERSION
    assert flags(conn)["needs_refetch"] == 0
    assert flags(conn)["ignored"] == 0


# --- the parser changed --------------------------------------------------

def test_an_older_parser_version_marks_the_row(conn):
    conn.execute("UPDATE listings SET parser_version = ?", (db.PARSER_VERSION - 1,))
    conn.commit()
    assert db.mark_outdated_parses(conn) == 1
    assert flags(conn)["needs_refetch"] == 1


def test_marking_for_refetch_also_marks_the_verdict(conn):
    """A verdict reached from text that is about to be replaced is not worth keeping."""
    db.mark_for_refetch(conn, "111")
    assert flags(conn)["needs_rescore"] == 1


def test_a_current_row_is_not_marked(conn):
    assert db.mark_outdated_parses(conn) == 0
    assert flags(conn)["needs_refetch"] == 0


def test_marking_is_idempotent(conn):
    conn.execute("UPDATE listings SET parser_version = 0")
    conn.commit()
    assert db.mark_outdated_parses(conn) == 1
    assert db.mark_outdated_parses(conn) == 0, "already marked, not marked twice"


def test_a_delisted_listing_is_not_queued_for_refetching(conn):
    """There is no page left to read."""
    conn.execute("UPDATE listings SET parser_version = 0, is_active = 0")
    conn.commit()
    assert db.mark_outdated_parses(conn) == 0


def test_re_reading_the_page_clears_the_flag(tmp_path):
    conf = Config(db_path=str(tmp_path / "s.db"))
    conf.searches = [SearchConfig(name="mt07", url=SEARCH_URL)]
    conf.images.dir, conf.scrape.dump_dir = str(tmp_path / "i"), str(tmp_path / "d")
    conf.scoring.enabled = conf.email.enabled = False
    connection = db.connect(conf.db_path)
    db.init_db(connection)
    db.sync_searches(connection, conf.searches)
    pipeline.run_scrape(conf, connection, FakeFetcher())

    listing_id = connection.execute("SELECT id FROM listings LIMIT 1").fetchone()["id"]
    connection.execute("UPDATE listings SET parser_version = 0")
    connection.commit()
    db.mark_outdated_parses(connection)
    assert flags(connection, listing_id)["needs_refetch"] == 1

    # A flagged listing is re-read whatever refresh_after_hours says.
    pipeline.run_scrape(conf, connection, FakeFetcher())
    after = flags(connection, listing_id)
    assert after["needs_refetch"] == 0
    assert after["parser_version"] == db.PARSER_VERSION
    connection.close()


def test_a_flagged_listing_is_refreshed_whatever_its_age(conn):
    row = dict(conn.execute("SELECT * FROM listings WHERE id = '111'").fetchone())
    row["needs_refetch"] = 0
    assert pipeline._needs_refresh(row, 24) is False, "just seen, so normally skipped"
    row["needs_refetch"] = 1
    assert pipeline._needs_refresh(row, 24) is True


# --- preferences changed -------------------------------------------------

def test_the_first_sight_of_preferences_is_not_a_change(conn):
    """Otherwise every new database would re-score everything at once."""
    assert db.note_preferences(conn, "# Want\n\nA GS.") == 0
    assert flags(conn)["needs_rescore"] == 0


def test_editing_preferences_marks_every_listing(conn):
    db.note_preferences(conn, "# Want\n\nA GS.")
    assert db.note_preferences(conn, "# Want\n\nA GS under 6000 EUR.") == 1
    assert flags(conn)["needs_rescore"] == 1


def test_saving_preferences_unchanged_marks_nothing(conn):
    db.note_preferences(conn, "# Want\n\nA GS.")
    assert db.note_preferences(conn, "# Want\n\nA GS.") == 0


def test_whitespace_counts_as_a_change_but_only_once(conn):
    db.note_preferences(conn, "a")
    assert db.note_preferences(conn, "a\n") == 1
    assert db.note_preferences(conn, "a\n") == 0


# --- what gets scored ----------------------------------------------------

def test_a_marked_listing_is_queued_even_though_nothing_about_it_changed(conn):
    score_it(conn)
    assert queued(conn) == []
    db.mark_for_rescore(conn, "111")
    assert queued(conn) == ["111"]


def test_scoring_clears_the_flag(conn):
    db.mark_for_rescore(conn, "111")
    score_it(conn)
    assert flags(conn)["needs_rescore"] == 0
    assert queued(conn) == []


def test_a_listing_awaiting_a_refetch_is_not_scored_yet(conn):
    """Its stored text is known to be out of date, so a verdict on it now buys
    an answer about text that is about to be replaced."""
    db.mark_for_refetch(conn, "111")
    assert queued(conn) == []

    db.upsert_listing(conn, {"id": "111", "url": "https://x/111", "title": "BMW R 1200 GS",
                             "description": "clean, and now correctly parsed",
                             "price_eur": 5900, "search_name": "gs"})
    conn.commit()
    assert queued(conn) == ["111"], "re-read, so now it can be scored"


def test_the_flag_works_with_rescore_on_change_off(conn):
    """Turning off automatic re-scoring must not disable an explicit request."""
    score_it(conn)
    db.mark_for_rescore(conn, "111")
    assert [r["id"] for r in db.unscored_listings(conn, False, "v1", 50)] == ["111"]


def test_marking_everything_returns_the_count(conn):
    db.upsert_listing(conn, {"id": "222", "url": "https://x/222", "title": "t",
                             "description": "d", "price_eur": 1, "search_name": "gs"})
    conn.commit()
    assert db.mark_for_rescore(conn) == 2


# --- ignoring a listing --------------------------------------------------

def test_ignoring_keeps_it_out_of_the_digest(conn):
    score_it(conn)
    cfg = EmailConfig(digest_min_score=3)
    assert [r["id"] for r in mailer.digest_candidates(conn, cfg)] == ["111"]

    db.set_ignored(conn, "111")
    assert mailer.digest_candidates(conn, cfg) == []


def test_ignoring_is_reversible(conn):
    score_it(conn)
    cfg = EmailConfig(digest_min_score=3)
    db.set_ignored(conn, "111")
    db.set_ignored(conn, "111", False)
    assert [r["id"] for r in mailer.digest_candidates(conn, cfg)] == ["111"]


def test_an_ignored_listing_keeps_its_score_and_its_row(conn):
    """It is dismissed, not deleted - you can still see why it scored well."""
    score_it(conn)
    db.set_ignored(conn, "111")
    row = conn.execute("SELECT * FROM listing_current WHERE id = '111'").fetchone()
    assert row["overall"] == 5
    assert row["ignored"] == 1


def test_an_ignored_listing_is_still_scored(conn):
    """Only the email is suppressed; the verdict stays current so the page can
    show what it thinks."""
    db.set_ignored(conn, "111")
    assert queued(conn) == ["111"]


def test_pending_counts_report_each_flag(conn):
    db.mark_for_refetch(conn, "111")
    db.set_ignored(conn, "111")
    counts = db.pending_counts(conn)
    assert counts == {"refetch": 1, "rescore": 1, "ignored": 1}


def test_pending_counts_on_an_empty_database(tmp_path):
    """SUM() over no rows is NULL, which must not reach the page."""
    connection = db.connect(tmp_path / "empty.db")
    db.init_db(connection)
    assert db.pending_counts(connection) == {"refetch": 0, "rescore": 0, "ignored": 0}
    connection.close()


def test_an_ignored_listing_raises_no_instant_alert(conn, monkeypatch, tmp_path):
    """The digest is one path to your inbox; the 5/5 interrupt is the other."""
    from karpm import scoring
    conf = Config(db_path=str(tmp_path / "t.db"))
    conf.email.enabled = True
    conf.email.to_addresses = ["me@example.com"]
    sent = []
    db.upsert_listing(conn, {"id": "222", "url": "https://x/222", "title": "another GS",
                             "description": "d", "price_eur": 5900, "search_name": "gs"})
    conn.commit()

    monkeypatch.setattr(scoring, "score_pending", lambda *_a: [
        {**SCORE, "listing_id": "111", "title": "t", "url": "u",
         "price_eur": 5900, "ignored": True},
        {**SCORE, "listing_id": "222", "title": "t", "url": "u",
         "price_eur": 5900, "ignored": False},
    ])
    monkeypatch.setattr(pipeline.mailer, "send_instant_alert",
                        lambda _c, _cfg, _k, listing_id: sent.append(listing_id))

    pipeline.run_scoring_and_alerts(conf, conn)
    assert sent == ["222"], "the ignored 5/5 was skipped, the other was not"


# --- upgrading a database that predates the flags ------------------------

def test_an_existing_database_is_not_wholesale_marked_on_upgrade(tmp_path, caplog):
    """Rows written before parser_version existed are at 0, which would read as
    "parsed by something ancient" and re-fetch the entire database on the first
    run after an update. They start level with the current version instead."""
    path = tmp_path / "old.db"
    connection = db.connect(path)
    db.init_db(connection)
    db.upsert_listing(connection, {"id": "111", "url": "https://x/111", "title": "t",
                                   "description": "d", "price_eur": 1, "search_name": "s"})
    # Wind it back to a v4 database: the columns exist but nothing set them.
    connection.execute("UPDATE listings SET parser_version = 0")
    connection.execute("PRAGMA user_version=4")
    connection.commit()

    with caplog.at_level("WARNING"):
        db.init_db(connection)

    assert flags(connection)["needs_refetch"] == 0
    assert flags(connection)["parser_version"] == db.PARSER_VERSION
    assert "read again" not in caplog.text
    connection.close()


def test_a_parser_bump_after_that_does_mark_them(tmp_path, monkeypatch):
    """And once it is level, the next bump is what the mechanism is for."""
    connection = db.connect(tmp_path / "t.db")
    db.init_db(connection)
    db.upsert_listing(connection, {"id": "111", "url": "https://x/111", "title": "t",
                                   "description": "d", "price_eur": 1, "search_name": "s"})
    connection.commit()

    monkeypatch.setattr(db, "PARSER_VERSION", db.PARSER_VERSION + 1)
    assert db.mark_outdated_parses(connection) == 1
    assert flags(connection)["needs_refetch"] == 1
    connection.close()
