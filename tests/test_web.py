"""The web UI: routes, the command queue, and editing the files on disk.

Everything here runs against a real database built by the fake-fetcher
pipeline, so the templates are rendered with the shapes they get in practice.
"""

import json

import pytest

from karpm import db, pipeline
from karpm.config import load_config
from karpm.web import create_app
from tests.test_pipeline import FakeFetcher, SEARCH_URL

CONFIG = """db_path = "{db}"

[[searches]]
name = "mt07"
url = "{url}"
make = "Yamaha"
model = "MT-07"

[images]
dir = "{images}"

[scoring]
enabled = false
preferences_file = "{prefs}"

[email]
enabled = false

[scrape]
dump_dir = "{debug}"

[web]
host = "127.0.0.1"
port = 8080
"""


@pytest.fixture
def app(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG.format(
        db=tmp_path / "test.db", url=SEARCH_URL, images=tmp_path / "images",
        prefs=tmp_path / "preferences.md", debug=tmp_path / "debug",
    ), encoding="utf-8")
    (tmp_path / "preferences.md").write_text("# What I want\n\nA cheap MT-07.\n",
                                             encoding="utf-8")

    conf = load_config(config_path)
    conn = db.connect(conf.db_path)
    db.init_db(conn)
    db.sync_searches(conn, conf.searches)
    pipeline.run_scrape(conf, conn, FakeFetcher())
    db.add_score(conn, "2847612345", {
        "model": "claude-opus-5", "prompt_version": "v1", "content_hash": "abc",
        "overall": 5, "fit": 5, "value": 4, "fair_price_eur": 6800,
        "headline": "Clean, low-mileage MT-07", "reasoning": "Under comparables.",
        "pros": ["Scheckheft"], "cons": [], "red_flags": [],
    })
    conn.commit()
    conn.close()

    application = create_app(str(config_path))
    application.config["TESTING"] = True
    yield application, config_path, tmp_path


@pytest.fixture
def client(app):
    application, _, _ = app
    return application.test_client()


def opened(conf_path):
    """A fresh connection to the database the app is using."""
    return db.connect(load_config(conf_path).db_path)


# --- pages ---------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/status-fragment", "/listings", "/listing/2847612345",
    "/searches", "/preferences", "/config",
])
def test_every_page_renders(client, path):
    assert client.get(path).status_code == 200


def test_a_listing_that_does_not_exist_is_a_404(client):
    assert client.get("/listing/9999999999").status_code == 404


def test_listings_page_shows_the_scraped_ads(client):
    body = client.get("/listings").get_data(as_text=True)
    assert "2847612345" in body


def test_filters_narrow_the_table(client):
    """A filter that excludes everything must show nothing, not everything."""
    assert "2847612345" in client.get("/listings?min_score=5").get_data(as_text=True)
    assert "2847612345" not in client.get("/listings?max_price=1").get_data(as_text=True)


def test_search_text_matches_the_title(client):
    assert "2847612345" in client.get("/listings?q=MT-07").get_data(as_text=True)
    assert "2847612345" not in client.get(
        "/listings?q=zzzznotinanytitle").get_data(as_text=True)


def test_sort_column_must_be_one_we_know(client):
    """The sort key is interpolated into SQL, so anything unknown falls back."""
    injection = "/listings?sort=price_eur);DROP TABLE listings;--"
    assert client.get(injection).status_code == 200


def test_sort_column_survives_the_round_trip(app, client):
    _, config_path, _ = app
    assert client.get("/listings?sort=price_eur&dir=asc").status_code == 200
    with opened(config_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) n FROM listings").fetchone()["n"] > 0


# --- photos --------------------------------------------------------------

def test_photo_is_served_from_disk(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        row = conn.execute(
            "SELECT listing_id, position FROM images WHERE local_path IS NOT NULL"
        ).fetchone()
    assert row is not None, "the fake fetcher should have saved some photos"
    resp = client.get(f"/photo/{row['listing_id']}/{row['position']}")
    assert resp.status_code == 200
    assert resp.get_data().startswith(b"\xff\xd8\xff")


def test_a_photo_outside_the_image_directory_is_refused(client, app, tmp_path):
    """The path comes from our own database, but it is still a path."""
    _, config_path, _ = app
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    with opened(config_path) as conn:
        conn.execute("UPDATE images SET local_path = ? WHERE listing_id = ? "
                     "AND position = 0", (str(secret), "2847612345"))
        conn.commit()
    assert client.get("/photo/2847612345/0").status_code == 404


def test_a_photo_we_never_downloaded_is_a_404(client):
    assert client.get("/photo/2847612345/999").status_code == 404


# --- daemon control ------------------------------------------------------

@pytest.mark.parametrize("action", ["scrape", "digest", "rescore"])
def test_control_queues_a_command(client, app, action):
    _, config_path, _ = app
    assert client.post(f"/control/{action}").status_code == 302
    with opened(config_path) as conn:
        queued = db.recent_commands(conn, 5)
    assert [r["command"] for r in queued] == [action]
    assert queued[0]["status"] == "pending"


def test_rescore_all_is_carried_in_the_command_params(client, app):
    _, config_path, _ = app
    client.post("/control/rescore", data={"all": "1"})
    with opened(config_path) as conn:
        params = json.loads(db.recent_commands(conn, 1)[0]["params_json"])
    assert params["all"] is True


def test_pause_and_resume(client, app):
    _, config_path, _ = app
    client.post("/control/pause")
    with opened(config_path) as conn:
        assert db.is_paused(conn) is True
    client.post("/control/resume")
    with opened(config_path) as conn:
        assert db.is_paused(conn) is False


def test_an_unknown_control_is_a_404(client):
    assert client.post("/control/rm-rf").status_code == 404


# --- editing the files on disk -------------------------------------------

def test_saving_searches_rewrites_only_the_search_tables(client, app):
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    assert "port = 8080" in before

    client.post("/searches/save", data={
        "name-0": "mt07", "url-0": SEARCH_URL, "enabled-0": "1",
        "make-0": "Yamaha", "model-0": "MT-07", "max_ads-0": "50",
        "name-1": "z900", "url-1": "https://example.com/z900", "enabled-1": "1",
        "make-1": "Kawasaki", "model-1": "Z900",
    })

    conf = load_config(config_path)
    assert [s.name for s in conf.searches] == ["mt07", "z900"]
    assert conf.searches[0].max_ads == 50
    assert conf.searches[1].model == "Z900"
    # Everything outside [[searches]] has to survive the edit.
    assert conf.web.port == 8080
    assert conf.scoring.enabled is False


def test_a_search_row_without_a_url_is_dropped(client, app):
    _, config_path, _ = app
    client.post("/searches/save", data={
        "name-0": "mt07", "url-0": SEARCH_URL, "enabled-0": "1",
        "name-1": "half-filled", "url-1": "",
    })
    assert [s.name for s in load_config(config_path).searches] == ["mt07"]


def test_quotes_in_a_search_name_do_not_break_the_file(client, app):
    _, config_path, _ = app
    client.post("/searches/save", data={
        "name-0": 'the "good" one', "url-0": SEARCH_URL, "enabled-0": "1",
    })
    assert load_config(config_path).searches[0].name == 'the "good" one'


def test_preferences_are_saved_with_a_backup(client, app):
    _, config_path, tmp_path = app
    client.post("/preferences", data={"text": "# New\n\nSomething else.\n"})
    prefs = tmp_path / "preferences.md"
    assert "Something else." in prefs.read_text(encoding="utf-8")
    assert "A cheap MT-07." in prefs.with_suffix(".md.bak").read_text(encoding="utf-8")


def test_config_is_saved(client, app):
    _, config_path, _ = app
    text = config_path.read_text(encoding="utf-8").replace("port = 8080", "port = 9090")
    assert client.post("/config", data={"text": text}).status_code == 302
    assert load_config(config_path).web.port == 9090


def test_broken_toml_is_never_written(client, app):
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/config", data={"text": "this is not [ toml"})
    assert resp.status_code == 200
    assert b"not valid TOML" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


def test_valid_toml_that_is_not_a_usable_config_is_rolled_back(client, app):
    """The dangerous case: it parses, so only loading it catches the mistake."""
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/config", data={
        "text": before.replace('dir = "', 'dir = 42  # "')})
    assert resp.status_code == 200
    assert b"rolled back" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


# --- status --------------------------------------------------------------

def test_a_daemon_that_has_never_run_is_not_reported_as_alive(client):
    assert b"not responding" in client.get("/status-fragment").get_data()


def test_a_fresh_heartbeat_means_alive(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.set_state(conn, "heartbeat", db.utcnow())
    body = client.get("/status-fragment").get_data()
    assert b"daemon running" in body
    assert b"not responding" not in body


def test_an_old_heartbeat_means_dead(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.set_state(conn, "heartbeat", "2020-01-01T00:00:00+00:00")
    assert b"not responding" in client.get("/status-fragment").get_data()


def test_an_unreadable_heartbeat_does_not_break_the_page(client, app):
    """Better a page saying "not responding" than a 500."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.set_state(conn, "heartbeat", "yesterday, ish")
    resp = client.get("/status-fragment")
    assert resp.status_code == 200
    assert b"not responding" in resp.get_data()


def test_a_running_command_is_shown(client, app):
    """A long scrape should read as "busy", not as an idle daemon."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.set_state(conn, "heartbeat", db.utcnow())
        db.queue_command(conn, "scrape")
        db.claim_command(conn)
    body = client.get("/status-fragment").get_data(as_text=True)
    assert "busy" in body and "scrape" in body


def test_pausing_is_visible_on_the_page(client, app):
    _, config_path, _ = app
    client.post("/control/pause")
    assert b"paused" in client.get("/status-fragment").get_data()


# --- opening an older database -------------------------------------------

def test_a_database_from_before_the_web_ui_is_migrated_on_startup(tmp_path):
    """The failure this reproduces: `karpm web` against a database written by an
    older version 500s on every page, because only the CLI applied the schema."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG.format(
        db=tmp_path / "old.db", url=SEARCH_URL, images=tmp_path / "images",
        prefs=tmp_path / "preferences.md", debug=tmp_path / "debug",
    ), encoding="utf-8")

    conn = db.connect(str(tmp_path / "old.db"))
    db.init_db(conn)
    # Wind it back to a v2 database: no commands, no app_state.
    conn.execute("DROP TABLE commands")
    conn.execute("DROP TABLE app_state")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    client = create_app(str(config_path)).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/status-fragment").status_code == 200

    with opened(config_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        # And the queue works, rather than only the pages loading.
        db.queue_command(conn, "scrape")
        assert db.claim_command(conn)["command"] == "scrape"


def test_a_search_added_in_the_ui_appears_on_the_dashboard(client, app):
    """Saving writes the config file; the dashboard reads the searches table."""
    _, config_path, _ = app
    client.post("/searches/save", data={
        "name-0": "mt07", "url-0": SEARCH_URL, "enabled-0": "1",
        "name-1": "z900", "url-1": "https://example.com/z900", "enabled-1": "1",
    })
    assert b"z900" in client.get("/").get_data()
