"""The daemon's side of the web UI: the command queue, pause, and the heartbeat."""

import threading
import time

import pytest

from karpm import daemon, db, pipeline
from karpm.config import Config, SearchConfig
from tests.test_pipeline import FakeFetcher, SEARCH_URL


@pytest.fixture
def ready(tmp_path):
    conf = Config(db_path=str(tmp_path / "test.db"))
    conf.searches = [SearchConfig(name="mt07", url=SEARCH_URL)]
    conf.images.dir = str(tmp_path / "images")
    conf.scrape.dump_dir = str(tmp_path / "debug")
    conf.scoring.enabled = False
    conf.email.enabled = False
    conn = db.connect(conf.db_path)
    db.init_db(conn)
    db.sync_searches(conn, conf.searches)
    yield conf, conn
    conn.close()


def test_a_claimed_command_is_not_claimed_twice(ready):
    _, conn = ready
    db.queue_command(conn, "digest")
    first = db.claim_command(conn)
    assert first is not None and first["status"] == "running"
    assert db.claim_command(conn) is None


def test_commands_are_claimed_oldest_first(ready):
    _, conn = ready
    db.queue_command(conn, "scrape")
    db.queue_command(conn, "digest")
    assert db.claim_command(conn)["command"] == "scrape"


def test_a_failing_command_is_recorded_not_swallowed(ready, monkeypatch):
    """A command that blew up must not look like one that succeeded."""
    conf, conn = ready
    monkeypatch.setattr(pipeline, "run_digest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no api key")))
    db.queue_command(conn, "digest")
    assert daemon._handle_pending_command(conf, conn) is True

    row = db.recent_commands(conn, 1)[0]
    assert row["status"] == "failed"
    assert "no api key" in row["result"]


def test_a_successful_command_records_its_result(ready):
    conf, conn = ready
    monkeypatched = {"count": 0}

    def fake_digest(*_args, **_kwargs):
        monkeypatched["count"] += 1
        return "resend-123"

    import karpm.pipeline as pl
    original, pl.run_digest = pl.run_digest, fake_digest
    try:
        db.queue_command(conn, "digest")
        assert daemon._handle_pending_command(conf, conn) is True
    finally:
        pl.run_digest = original

    row = db.recent_commands(conn, 1)[0]
    assert row["status"] == "done"
    assert "resend-123" in row["result"]
    assert monkeypatched["count"] == 1


def test_nothing_queued_is_not_an_error(ready):
    conf, conn = ready
    assert daemon._handle_pending_command(conf, conn) is False


def test_rescore_all_forgets_the_old_verdicts(ready):
    conf, conn = ready
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.add_score(conn, "2847612345", {
        "model": "claude-opus-5", "prompt_version": "v1", "content_hash": "abc",
        "overall": 5, "fit": 5, "value": 4, "fair_price_eur": 6800,
        "headline": "h", "reasoning": "r", "pros": [], "cons": [], "red_flags": [],
    })
    conn.commit()
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 1

    db.queue_command(conn, "rescore", {"all": True})
    daemon._handle_pending_command(conf, conn)
    # scoring.enabled is off, so nothing is scored again - the point is that the
    # slate was wiped, which is what makes the re-score actually happen.
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 0


def test_plain_rescore_keeps_the_old_verdicts(ready):
    conf, conn = ready
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.add_score(conn, "2847612345", {
        "model": "claude-opus-5", "prompt_version": "v1", "content_hash": "abc",
        "overall": 5, "fit": 5, "value": 4, "fair_price_eur": 6800,
        "headline": "h", "reasoning": "r", "pros": [], "cons": [], "red_flags": [],
    })
    conn.commit()
    db.queue_command(conn, "rescore")
    daemon._handle_pending_command(conf, conn)
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 1


def test_an_unknown_command_fails_rather_than_being_ignored(ready):
    conf, conn = ready
    conn.execute("INSERT INTO commands (command, requested_at, status) "
                 "VALUES ('explode', ?, 'pending')", (db.utcnow(),))
    conn.commit()
    assert daemon._handle_pending_command(conf, conn) is True
    assert db.recent_commands(conn, 1)[0]["status"] == "failed"


def test_a_command_interrupted_by_a_crash_is_reset_at_startup(ready):
    """Otherwise it stays 'running' forever and blocks nothing but confuses everyone."""
    _, conn = ready
    db.queue_command(conn, "scrape")
    db.claim_command(conn)
    assert db.reset_stale_commands(conn) == 1
    row = db.recent_commands(conn, 1)[0]
    assert row["status"] == "failed"
    assert db.claim_command(conn) is None


def test_pause_is_off_by_default_and_round_trips(ready):
    _, conn = ready
    assert db.is_paused(conn) is False
    db.set_state(conn, "paused", "1")
    assert db.is_paused(conn) is True
    db.set_state(conn, "paused", "0")
    assert db.is_paused(conn) is False


def test_the_heartbeat_keeps_beating_on_its_own(ready):
    """It runs in a thread precisely so a long scrape cannot stop it."""
    conf, conn = ready
    stop = threading.Event()
    thread = threading.Thread(target=daemon._beat, args=(conf.db_path, stop, 1),
                              daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while db.get_state(conn, "heartbeat") is None and time.time() < deadline:
            time.sleep(0.05)
        first = db.get_state(conn, "heartbeat")
        assert first is not None

        while db.get_state(conn, "heartbeat") == first and time.time() < deadline:
            time.sleep(0.05)
        assert db.get_state(conn, "heartbeat") != first
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_search_the_table_has_never_seen_still_records_its_run(ready):
    """The daemon rereads its config every cycle, so a search added in the web
    UI arrives at a scrape with no row to update. It would be scraped and then
    look like it had never run."""
    conf, conn = ready
    conn.execute("DELETE FROM searches")
    conn.commit()

    pipeline.run_scrape(conf, conn, FakeFetcher())

    row = conn.execute("SELECT * FROM searches WHERE name = 'mt07'").fetchone()
    assert row is not None, "the scrape should have registered the search"
    assert row["last_run_at"], "and recorded when it ran"
    assert "seen" in row["last_status"]
