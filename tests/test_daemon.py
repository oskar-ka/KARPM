"""The daemon's side of the web UI: the command queue, pause, and the heartbeat."""

import pathlib
import threading
import time
from datetime import datetime, timedelta

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


def _one_scored_listing(conf, conn):
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.add_score(conn, "2847612345", {
        "model": "claude-opus-5", "prompt_version": "v1", "content_hash": "abc",
        "overall": 5, "fit": 5, "value": 4, "fair_price_eur": 6800,
        "headline": "h", "reasoning": "r", "pros": [], "cons": [], "red_flags": [],
    })
    conn.commit()
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 1


def test_rescore_all_forgets_the_old_verdicts(ready, monkeypatch):
    conf, conn = ready
    conf.scoring.enabled = True
    monkeypatch.setattr(pipeline, "run_scoring_and_alerts", lambda *a, **k: {})
    _one_scored_listing(conf, conn)

    db.queue_command(conn, "rescore", {"all": True})
    daemon._handle_pending_command(conf, conn)

    # The slate is wiped, which is what makes the re-score actually happen.
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 0


def test_rescore_all_deletes_nothing_when_scoring_is_off(ready):
    """The verdicts would be gone and nothing could rebuild them - so the check
    happens before the delete, not after it."""
    conf, conn = ready
    conf.scoring.enabled = False
    _one_scored_listing(conf, conn)

    db.queue_command(conn, "rescore", {"all": True})
    daemon._handle_pending_command(conf, conn)

    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 1
    failed = db.recent_commands(conn, 1)[0]
    assert failed["status"] == "failed"
    assert "disabled" in failed["result"], "and it says why, rather than looking done"


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


# --- an empty schedule is a setting, not a mistake ------------------------

def _run_briefly(conf, conn, seconds=2.5, poll_seconds=1):
    """Run the loop on this thread - signals need it - and stop it on a timer."""
    threading.Timer(seconds, lambda: setattr(daemon, "_stop", True)).start()
    daemon._stop = False
    try:
        daemon.run_forever(conf, conn, poll_seconds=poll_seconds)
    finally:
        daemon._stop = False


def test_no_scrape_slots_is_not_an_error(ready):
    """It means "never": the daemon runs as usual with nothing to fire, which
    is how you drive it from the web UI alone."""
    conf, conn = ready
    conf.schedule.scrape_at = []
    conf.schedule.digest_at = []

    _run_briefly(conf, conn)            # must not raise

    assert db.get_state(conn, "next_scrape") == "not scheduled"
    assert conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"] == 0
    assert db.get_state(conn, "heartbeat"), "still alive, just idle"


def test_queued_commands_still_run_with_no_schedule(ready):
    """The whole point of an empty schedule: the buttons still work."""
    conf, conn = ready
    conf.schedule.scrape_at = []
    conf.schedule.digest_at = []
    db.queue_command(conn, "digest")

    _run_briefly(conf, conn)

    assert db.recent_commands(conn, 1)[0]["status"] in ("done", "failed")


def test_next_fire_of_nothing_is_nothing():
    assert daemon._next_fire([], datetime.now()) is None


# --- scoring slots --------------------------------------------------------

def test_without_score_slots_a_scrape_scores_too(ready, monkeypatch):
    """The default: you want to hear about a good listing quickly."""
    conf, conn = ready
    conf.scoring.enabled = True
    conf.schedule.score_at = []
    scored = []
    monkeypatch.setattr(pipeline, "run_scrape", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "run_scoring_and_alerts",
                        lambda *a, **k: scored.append(1) or {})

    pipeline.run_once(conf, conn)
    assert scored == [1]


def test_with_score_slots_a_scrape_leaves_scoring_alone(ready, monkeypatch):
    """Setting times was a decision about when the money is spent."""
    conf, conn = ready
    conf.scoring.enabled = True
    conf.schedule.score_at = ["08:00"]
    scored = []
    monkeypatch.setattr(pipeline, "run_scrape", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "run_scoring_and_alerts",
                        lambda *a, **k: scored.append(1) or {})

    pipeline.run_once(conf, conn)
    assert scored == []


def test_a_score_slot_that_has_passed_fires(ready, monkeypatch):
    conf, conn = ready
    scored = []
    monkeypatch.setattr(pipeline, "run_scoring_and_alerts",
                        lambda *a, **k: scored.append(1) or {})
    fired = daemon._fire_due(conn, "score", [(0, 1)],
                             lambda: pipeline.run_scoring_and_alerts(conf, conn))
    assert fired is True and scored == [1]


def test_a_slot_already_run_today_does_not_fire_again(ready):
    conf, conn = ready
    calls = []
    daemon._fire_due(conn, "score", [(0, 1)], lambda: calls.append(1) or {})
    # _fire_due records nothing itself; the run row is what marks it done.
    run_id = db.start_run(conn, "score")
    db.finish_run(conn, run_id, True)
    daemon._fire_due(conn, "score", [(0, 1)], lambda: calls.append(1) or {})
    assert len(calls) == 1


def test_no_score_slots_means_nothing_is_due(ready):
    _, conn = ready
    assert daemon._fire_due(conn, "score", [], lambda: 1 / 0) is False


# --- clearing the database ------------------------------------------------

def test_reset_removes_everything_collected(ready, tmp_path):
    conf, conn = ready
    pipeline.run_scrape(conf, conn, FakeFetcher())
    assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] > 0

    removed = db.reset_everything(conn, conf.images.dir)

    assert removed["listings"] > 0
    for table in db.COLLECTED_TABLES:
        assert conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] == 0, table


def test_reset_keeps_the_pause_you_set(ready):
    """Silently un-pausing would let a scrape start that you had stopped."""
    _, conn = ready
    db.set_state(conn, "paused", "1")
    db.set_state(conn, "heartbeat", db.utcnow())
    db.reset_everything(conn, None)
    assert db.is_paused(conn) is True
    assert db.get_state(conn, "heartbeat") is None


def test_reset_deletes_the_photos_too(ready, tmp_path):
    """Otherwise a fresh start leaves orphans that all get fetched again."""
    conf, conn = ready
    folder = pathlib.Path(conf.images.dir) / "2847612345"
    folder.mkdir(parents=True)
    (folder / "0.jpg").write_bytes(b"\xff\xd8\xff")
    (folder / "ad.txt").write_text("https://x/1", encoding="utf-8")

    removed = db.reset_everything(conn, conf.images.dir)

    assert removed["images_deleted"] == 2
    assert list(pathlib.Path(conf.images.dir).iterdir()) == []


def test_reset_survives_a_missing_image_directory(ready):
    conf, conn = ready
    assert db.reset_everything(conn, "/nowhere/at/all")["images_deleted"] == 0
    assert db.reset_everything(conn, None)["images_deleted"] == 0


def test_the_database_still_works_after_a_reset(ready):
    """It has to be usable, not just empty."""
    conf, conn = ready
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.reset_everything(conn, conf.images.dir)

    db.sync_searches(conn, conf.searches)
    pipeline.run_scrape(conf, conn, FakeFetcher())
    assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] > 0


def test_reset_reports_the_commands_it_drops(ready):
    """A queued command is a click that is about to vanish. It goes - it would
    run against an empty database otherwise - but it is not dropped quietly."""
    _, conn = ready
    db.queue_command(conn, "scrape")
    db.queue_command(conn, "digest")

    removed = db.reset_everything(conn, None)

    assert removed["pending_commands"] == 2
    assert db.recent_commands(conn, 5) == []


def test_reset_of_an_idle_queue_reports_nothing_dropped(ready):
    _, conn = ready
    assert db.reset_everything(conn, None)["pending_commands"] == 0


# --- switching scoring off ------------------------------------------------

def test_a_run_with_scoring_off_only_scrapes(ready, monkeypatch, caplog):
    conf, conn = ready
    conf.scoring.enabled = False
    scored = []
    monkeypatch.setattr(pipeline, "run_scrape", lambda *a, **k: {"seen": 1})
    monkeypatch.setattr(pipeline, "run_scoring_and_alerts",
                        lambda *a, **k: scored.append(1) or {})

    with caplog.at_level("INFO"):
        result = pipeline.run_once(conf, conn)

    assert scored == []
    assert result == {"seen": 1}
    # Silence would leave you wondering whether it had scored or failed to.
    assert "scoring is disabled" in caplog.text


def test_run_scoring_says_so_rather_than_starting_a_run(ready, caplog):
    """It used to open a run row and score nothing, which reads as a failure."""
    conf, conn = ready
    conf.scoring.enabled = False
    with caplog.at_level("INFO"):
        result = pipeline.run_scoring_and_alerts(conf, conn)
    assert result["skipped"] == "disabled"
    assert conn.execute("SELECT COUNT(*) n FROM runs WHERE kind='score'").fetchone()["n"] == 0


def test_scoring_stops_mid_run_when_it_is_switched_off(ready, monkeypatch, caplog,
                                                       tmp_path):
    """Every listing is an API call, so stopping at the next one rather than at
    the end of the queue is the difference between one more and two hundred."""
    from karpm import scoring
    conf, conn = ready
    conf.scoring.enabled = True
    preferences = tmp_path / "preferences.md"
    preferences.write_text("# Want\n\nA GS.\n", encoding="utf-8")
    conf.scoring.preferences_file = str(preferences)
    pipeline.run_scrape(conf, conn, FakeFetcher())

    calls = []
    switch = {"on": True}

    def fake_score(self, conn_, row):
        calls.append(row["id"])
        switch["on"] = False            # someone unticks the box mid-run
        return {"model": "m", "prompt_version": "v1", "content_hash": row["content_hash"],
                "overall": 3, "fit": 3, "value": 3, "fair_price_eur": 1,
                "headline": "h", "reasoning": "r", "pros": [], "cons": [], "red_flags": []}

    monkeypatch.setattr(scoring.Scorer, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(scoring.Scorer, "score_listing", fake_score)

    with caplog.at_level("WARNING"):
        written = scoring.score_pending(conn, conf.scoring,
                                        still_enabled=lambda: switch["on"])

    assert len(calls) == 1, "it stopped before the second listing, not after the last"
    assert len(written) == 1
    assert "switched off mid-run" in caplog.text


def test_the_switch_reads_the_config_file(ready, tmp_path):
    conf, _ = ready
    path = tmp_path / "config.toml"
    path.write_text(f'db_path = "{conf.db_path}"\n[scoring]\nenabled = true\n',
                    encoding="utf-8")
    switch = pipeline.scoring_switch(path)
    assert switch() is True

    path.write_text(f'db_path = "{conf.db_path}"\n[scoring]\nenabled = false\n',
                    encoding="utf-8")
    assert switch() is False, "it re-reads, rather than closing over the old value"


def test_no_config_path_means_no_switch(ready):
    """A caller with no file to re-read has nothing newer to learn."""
    assert pipeline.scoring_switch(None) is None


def test_a_broken_config_mid_run_does_not_stop_scoring(ready, tmp_path):
    """Half a file saved is not a decision to stop."""
    path = tmp_path / "config.toml"
    path.write_text("this is not [ toml", encoding="utf-8")
    assert pipeline.scoring_switch(path)() is True


# --- saying it is alive ---------------------------------------------------

def test_the_heartbeat_names_what_it_is_waiting_for(ready):
    _, conn = ready
    db.set_state(conn, "next_scrape", datetime.now().replace(
        hour=19, minute=30).isoformat())
    db.set_state(conn, "next_digest", "not scheduled")

    line = daemon.status_line(conn)

    assert line.startswith("heartbeat - ")
    assert "next scrape 19:30" in line
    assert "digest never" in line
    assert "score with each scrape" in line


def test_the_heartbeat_says_when_scoring_is_off(ready):
    _, conn = ready
    db.set_state(conn, "scoring", "off")
    assert "scoring off" in daemon.status_line(conn)


def test_the_heartbeat_counts_the_backlog(ready):
    conf, conn = ready
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.mark_for_refetch(conn, "2847612345")
    db.queue_command(conn, "digest")

    line = daemon.status_line(conn)

    assert "1 to re-fetch" in line
    assert "queued command" in line


def test_a_paused_schedule_is_impossible_to_miss(ready):
    """The commonest reason for "why has it not scraped"."""
    _, conn = ready
    db.set_state(conn, "paused", "1")
    assert "SCHEDULE PAUSED" in daemon.status_line(conn)


def test_an_idle_daemon_says_only_what_matters(ready):
    """Nothing queued, nothing paused: no trailing clutter."""
    _, conn = ready
    line = daemon.status_line(conn)
    assert "queued" not in line and "re-fetch" not in line


@pytest.mark.parametrize("delta, expected", [
    (timedelta(hours=2), "%H:%M"),
    (timedelta(days=1), "tomorrow"),
    (timedelta(days=5), "%d %b"),
])
def test_a_time_is_written_the_way_you_would_read_it(delta, expected):
    moment = datetime.now() + delta
    shown = daemon._when(moment.isoformat())
    assert (moment.strftime(expected) in shown) if "%" in expected else (expected in shown)


@pytest.mark.parametrize("stored", [None, "not scheduled"])
def test_nothing_scheduled_reads_as_never(stored):
    assert daemon._when(stored) == "never"


def test_the_first_heartbeat_arrives_at_once(ready, caplog):
    """A fresh terminal should not be silent while it waits for the interval."""
    conf, conn = ready
    conf.schedule.scrape_at = []
    conf.schedule.digest_at = []
    conf.schedule.heartbeat_s = 3600        # far longer than this test runs
    with caplog.at_level("INFO"):
        _run_briefly(conf, conn, seconds=2.0)
    assert "heartbeat - " in caplog.text


def test_the_heartbeat_keeps_reporting_on_its_own(ready, caplog):
    """The point of it being a thread: it proves liveness precisely when the
    main loop is busy with a scrape and cannot say anything itself."""
    conf, conn = ready
    stop = threading.Event()
    thread = threading.Thread(target=daemon._beat, args=(conf.db_path, stop, 1),
                              daemon=True)
    with caplog.at_level("INFO"):
        thread.start()
        time.sleep(2.5)
        stop.set()
        thread.join(timeout=5)

    assert caplog.text.count("heartbeat - ") >= 2, "more than just the first one"
    assert not thread.is_alive()


# --- how often ------------------------------------------------------------

def test_the_interval_comes_from_the_config(ready):
    conf, _ = ready
    conf.schedule.heartbeat_s = 45
    assert daemon.heartbeat_seconds(conf) == 45


@pytest.mark.parametrize("given, expected", [
    (1, 1),
    (5, 5),
    (0, daemon.MIN_HEARTBEAT_S),
    (-30, daemon.MIN_HEARTBEAT_S),
    (999999, daemon.MAX_HEARTBEAT_S),
])
def test_short_intervals_are_allowed(ready, given, expected):
    """A one-second heartbeat is noisy and is a fine way to watch the daemon
    while setting it up. Only zero is refused, because it would spin."""
    conf, _ = ready
    conf.schedule.heartbeat_s = given
    assert daemon.heartbeat_seconds(conf) == expected


def test_a_five_second_heartbeat_actually_beats_every_five_seconds(ready, caplog):
    conf, conn = ready
    stop = threading.Event()
    thread = threading.Thread(target=daemon._beat, args=(conf.db_path, stop, 1),
                              daemon=True)
    with caplog.at_level("INFO"):
        thread.start()
        time.sleep(3.2)
        stop.set()
        thread.join(timeout=5)
    beats = caplog.text.count("heartbeat - ")
    assert 3 <= beats <= 5, f"one a second for three seconds, got {beats}"


def test_the_interval_is_re_read_every_beat(ready, tmp_path, caplog):
    """Changing heartbeat_s takes effect on the next beat, not the next restart -
    which is what made it look as though short intervals did not work."""
    conf, conn = ready
    path = tmp_path / "config.toml"
    path.write_text(f'db_path = "{conf.db_path}"\n[schedule]\nheartbeat_s = 1\n',
                    encoding="utf-8")

    stop = threading.Event()
    thread = threading.Thread(target=daemon._beat,
                              args=(conf.db_path, stop, 1, str(path)), daemon=True)
    with caplog.at_level("INFO"):
        thread.start()
        time.sleep(1.5)
        # Slow it right down while it is running.
        path.write_text(f'db_path = "{conf.db_path}"\n[schedule]\nheartbeat_s = 3600\n',
                        encoding="utf-8")
        time.sleep(2.5)
        stop.set()
        thread.join(timeout=5)

    beats = caplog.text.count("heartbeat - ")
    assert beats <= 3, f"it should have slowed to a crawl, but beat {beats} times"


def test_a_broken_config_mid_beat_keeps_the_last_interval(ready, tmp_path):
    conf, conn = ready
    path = tmp_path / "config.toml"
    path.write_text("this is not [ toml", encoding="utf-8")
    stop = threading.Event()
    thread = threading.Thread(target=daemon._beat,
                              args=(conf.db_path, stop, 1, str(path)), daemon=True)
    thread.start()
    time.sleep(1.5)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "a bad config must not take the heartbeat down"


def test_the_first_heartbeat_already_knows_the_schedule(ready, caplog, monkeypatch):
    """Published before the beat thread starts - otherwise the first line of a
    fresh start says "next scrape never" and sends you looking for a fault."""
    conf, conn = ready
    conf.schedule.scrape_at = ["07:30"]
    conf.schedule.heartbeat_s = 3600
    # A named slot fires for real if the clock is past it, and this test ran
    # against the live site for a minute and a half when the time of day
    # happened to be 07:31. Nothing here is about what a scrape does.
    monkeypatch.setattr(pipeline, "run_once", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "run_digest", lambda *a, **k: None)
    with caplog.at_level("INFO"):
        _run_briefly(conf, conn, seconds=2.0)
    first = [line for line in caplog.text.splitlines() if "heartbeat - " in line][0]
    assert "next scrape 07:30" in first
    assert "next scrape never" not in first


def test_the_loop_never_dozes_longer_than_a_heartbeat(ready):
    """A short heartbeat is someone watching, and a queued command should not
    outlast the interval they chose."""
    conf, conn = ready
    conf.schedule.heartbeat_s = 2
    conf.schedule.scrape_at = []
    conf.schedule.digest_at = []
    db.queue_command(conn, "digest")

    _run_briefly(conf, conn, seconds=3.0)    # far less than the 30s default poll

    assert db.recent_commands(conn, 1)[0]["status"] in ("done", "failed")


def test_a_queued_command_wakes_the_daemon_from_a_long_sleep(ready, monkeypatch):
    """The buttons say "now". Without this the daemon sleeps out its whole poll
    interval first, so "scrape now" meant "some time in the next half hour".

    The command has to be queued while it is already asleep: one queued before
    the loop starts is picked up on the first pass whether it wakes or not, so
    testing that would prove nothing.
    """
    conf, conn = ready
    conf.schedule.heartbeat_s = 3600        # a 30s poll it would otherwise sit out
    conf.schedule.scrape_at = []
    conf.schedule.digest_at = []
    monkeypatch.setattr(pipeline, "run_digest", lambda *a, **k: None)

    def queue_from_elsewhere():
        other = db.connect(conf.db_path)    # the web process, in effect
        db.queue_command(other, "digest")
        other.close()

    threading.Timer(1.5, queue_from_elsewhere).start()
    # A realistic poll: with poll_seconds=1 the loop comes round every second
    # anyway and the wake would never be what picked the command up.
    _run_briefly(conf, conn, seconds=5.0, poll_seconds=30)

    handled = db.recent_commands(conn, 1)[0]
    assert handled["command"] == "digest"
    assert handled["status"] in ("done", "failed"), "still pending: it slept through it"


def test_has_pending_command_sees_only_what_is_waiting(ready):
    _, conn = ready
    assert db.has_pending_command(conn) is False
    db.queue_command(conn, "digest")
    assert db.has_pending_command(conn) is True
    db.claim_command(conn)
    assert db.has_pending_command(conn) is False, "running is not waiting"
