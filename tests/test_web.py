"""The web UI: routes, the command queue, and editing the files on disk.

Everything here runs against a real database built by the fake-fetcher
pipeline, so the templates are rendered with the shapes they get in practice.
"""

import json
import re

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

@pytest.mark.parametrize("action", ["scrape", "digest", "extract", "rescore"])
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


def form(client, overrides=None):
    """The config form as the page would submit it, with a few values changed.

    Posting a partial form is not how a browser behaves, and a field left out
    reads as blank, so tests build the whole thing from what the page shows.
    """
    from karpm.web import fields
    page = client.get("/config").get_data(as_text=True)
    data = {}
    for section in fields.SECTIONS:
        for spec in section.fields:
            name = fields.input_name(section.name, spec.key)
            if spec.kind == "bool":
                if re.search(rf'id="{re.escape(name)}"[^>]*checked', page):
                    data[name] = "1"
            else:
                data[name] = _shown(page, name)
    data.update(overrides or {})
    return data


def _shown(page, name):
    """The value the rendered page is carrying for one field."""
    for pattern in (rf'id="{re.escape(name)}"[^>]*value="([^"]*)"',
                    rf'id="{re.escape(name)}"[^>]*>([^<]*)</textarea>',
                    rf'id="{re.escape(name)}".*?<option value="([^"]*)" selected'):
        found = re.search(pattern, page, re.S)
        if found:
            return found.group(1).replace("&#34;", '"').replace("&amp;", "&")
    return ""


def test_the_config_page_has_a_field_for_every_setting(client):
    from karpm.web import fields
    page = client.get("/config").get_data(as_text=True)
    for section in fields.SECTIONS:
        for spec in section.fields:
            assert f'id="{fields.input_name(section.name, spec.key)}"' in page, spec.key


def test_the_config_page_shows_the_current_values(client):
    page = client.get("/config").get_data(as_text=True)
    assert _shown(page, "web__port") == "8080"
    assert _shown(page, "scoring__effort") == "medium"


def test_saving_changes_one_value(client, app):
    _, config_path, _ = app
    assert client.post("/config", data=form(client, {"web__port": "9090"})
                       ).status_code == 302
    conf = load_config(config_path)
    assert conf.web.port == 9090
    # And nothing else moved.
    assert conf.scrape.min_delay_s == 4.0
    assert conf.searches[0].name == "mt07"


def test_saving_keeps_the_comments_in_the_file(client, app):
    """config.toml is a file people write in; a save must not flatten it."""
    _, config_path, _ = app
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[web]", "# how the web UI binds\n[web]"), encoding="utf-8")
    client.post("/config", data=form(client, {"web__port": "9090"}))
    assert "# how the web UI binds" in config_path.read_text(encoding="utf-8")


def test_a_checkbox_turns_a_setting_off(client, app):
    _, config_path, _ = app
    data = form(client)
    assert data.get("images__enabled") == "1"
    data.pop("images__enabled")          # an unchecked box sends nothing at all
    client.post("/config", data=data)
    assert load_config(config_path).images.enabled is False


def test_a_number_field_given_words_is_rejected_beside_the_field(client, app):
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/config", data=form(client, {"scrape__min_delay_s": "soon"}))
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "is not a number" in body
    assert "nothing was saved" in body
    assert "scrape.min_delay_s" in body
    assert config_path.read_text(encoding="utf-8") == before


def test_a_rejected_save_comes_back_with_what_was_typed(client):
    """Losing a page of edits to one typo would be its own bug."""
    resp = client.post("/config", data=form(client, {"scrape__min_delay_s": "soon",
                                                     "web__host": "0.0.0.0"}))
    page = resp.get_data(as_text=True)
    assert _shown(page, "web__host") == "0.0.0.0"
    assert _shown(page, "scrape__min_delay_s") == "soon"


def test_a_required_field_cannot_be_emptied(client, app):
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/config", data=form(client, {"db_path": ""}))
    assert b"cannot be empty" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


def test_an_optional_field_left_blank_falls_back_to_the_default(client, app):
    """Blank is not an empty string - the key is removed, so the default applies."""
    _, config_path, _ = app
    client.post("/config", data=form(client, {"images__max_per_listing": ""}))
    text = config_path.read_text(encoding="utf-8")
    assert "max_per_listing" not in text
    assert load_config(config_path).images.max_per_listing == 12


def test_a_list_field_is_edited_a_line_at_a_time(client, app):
    _, config_path, _ = app
    client.post("/config", data=form(client, {
        "email__to_addresses": "me@example.com\nyou@example.com",
        "schedule__scrape_at": "06:00\n18:00"}))
    conf = load_config(config_path)
    assert conf.email.to_addresses == ["me@example.com", "you@example.com"]
    assert conf.schedule.scrape_at == ["06:00", "18:00"]


def test_retry_delays_are_edited_as_a_comma_separated_list(client, app):
    _, config_path, _ = app
    client.post("/config", data=form(client, {"scrape__retry_delays_s": "1, 2, 4.5"}))
    assert load_config(config_path).scrape.retry_delays_s == [1.0, 2.0, 4.5]


def test_a_choice_outside_its_options_is_refused(client, app):
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/config", data=form(client, {"scoring__effort": "maximum"}))
    assert b"must be one of" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


def test_a_file_that_would_not_load_is_never_written(app):
    """The last line of defence, tested directly: the candidate is loaded from a
    temporary file, so a rejected save never touches the real one."""
    from karpm.web.app import _write_checked
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        _write_checked(config_path, before.replace("port = 8080", 'port = "wrong"'))

    assert config_path.read_text(encoding="utf-8") == before
    assert not config_path.with_suffix(".toml.candidate").exists()


def test_a_file_that_loads_is_written_with_a_backup(app):
    from karpm.web.app import _write_checked
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    _write_checked(config_path, before.replace("port = 8080", "port = 9191"))
    assert load_config(config_path).web.port == 9191
    assert config_path.with_suffix(".toml.bak").read_text(encoding="utf-8") == before


def test_quotes_in_a_value_do_not_break_the_file(client, app):
    _, config_path, _ = app
    client.post("/config", data=form(client, {"email__subject_prefix": '[a "b" c]'}))
    assert load_config(config_path).email.subject_prefix == '[a "b" c]'



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


# --- the searches page ---------------------------------------------------

def search_form(client, overrides=None):
    """The searches form as the page would submit it."""
    from karpm.web import fields
    page = client.get("/searches").get_data(as_text=True)
    data = {}
    index = 0
    while f'id="name-{index}"' in page:
        for spec in fields.SEARCH_FIELDS:
            name = f"{spec.key}-{index}"
            if spec.kind == "bool":
                if re.search(rf'id="{name}"[^>]*checked', page):
                    data[name] = "1"
            else:
                data[name] = _shown(page, name)
        index += 1
    data.update(overrides or {})
    return data


def test_the_searches_page_has_a_field_for_every_search_setting(client):
    from karpm.web import fields
    page = client.get("/searches").get_data(as_text=True)
    for spec in fields.SEARCH_FIELDS:
        assert f'id="{spec.key}-0"' in page, spec.key
    # And the template the "add search" button clones.
    assert 'id="search-template"' in page
    assert "name-INDEX" in page


def test_editing_a_search(client, app):
    _, config_path, _ = app
    client.post("/searches/save", data=search_form(client, {"model-0": "MT-09"}))
    conf = load_config(config_path)
    assert conf.searches[0].model == "MT-09"
    assert conf.searches[0].name == "mt07"


def test_adding_a_search(client, app):
    """What the add button produces: one more block, numbered after the rest."""
    _, config_path, _ = app
    client.post("/searches/save", data=search_form(client, {
        "name-1": "z900", "url-1": "https://example.com/z900", "enabled-1": "1",
        "make-1": "Kawasaki", "model-1": "Z900", "max_ads-1": "50"}))
    conf = load_config(config_path)
    assert [s.name for s in conf.searches] == ["mt07", "z900"]
    assert conf.searches[1].max_ads == 50


def test_removing_a_search_leaves_a_gap_in_the_numbering(client, app):
    """The browser drops the block, so the indexes that arrive are not contiguous."""
    _, config_path, _ = app
    data = search_form(client, {"name-1": "z900", "url-1": "https://example.com/z900"})
    client.post("/searches/save", data=data)
    assert len(load_config(config_path).searches) == 2

    remaining = {k: v for k, v in search_form(client).items() if not k.endswith("-0")}
    client.post("/searches/save", data=remaining)
    assert [s.name for s in load_config(config_path).searches] == ["z900"]


def test_a_search_without_a_url_is_refused_rather_than_dropped(client, app):
    """Half-filled is a mistake to point at, not a row to quietly discard."""
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/searches/save", data=search_form(client, {"name-1": "half"}))
    assert b"cannot be empty" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


def test_an_entirely_empty_block_is_just_ignored(client, app):
    _, config_path, _ = app
    resp = client.post("/searches/save", data=search_form(client, {
        "name-1": "", "url-1": "", "make-1": "", "model-1": "", "max_ads-1": ""}))
    assert resp.status_code == 302
    assert [s.name for s in load_config(config_path).searches] == ["mt07"]


def test_two_searches_cannot_share_a_name(client, app):
    """They are keyed by name in the database, so a duplicate would merge them."""
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/searches/save", data=search_form(client, {
        "name-1": "mt07", "url-1": "https://example.com/other"}))
    assert b"cannot share a name" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


def test_max_ads_given_words_is_refused(client, app):
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    resp = client.post("/searches/save", data=search_form(client, {"max_ads-0": "lots"}))
    assert b"not a whole number" in resp.get_data()
    assert config_path.read_text(encoding="utf-8") == before


def test_saving_searches_leaves_the_rest_of_the_config_alone(client, app):
    _, config_path, _ = app
    client.post("/searches/save", data=search_form(client, {"model-0": "MT-09"}))
    conf = load_config(config_path)
    assert conf.web.port == 8080
    assert conf.scoring.enabled is False
    assert str(conf.images.dir).endswith("images")


# --- a save touches only what changed ------------------------------------

def test_saving_an_untouched_form_changes_nothing(client, app):
    """Submitting the form without editing anything must be a no-op on disk,
    not a rewrite of every line into its canonical spelling."""
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8")
    assert client.post("/config", data=form(client)).status_code == 302
    assert config_path.read_text(encoding="utf-8") == before


def test_a_setting_left_at_its_default_is_not_written_out(client, app):
    """The form shows defaults for keys the file does not set. Saving must not
    turn all of them into explicit lines."""
    _, config_path, _ = app
    assert "user_agent" not in config_path.read_text(encoding="utf-8")
    client.post("/config", data=form(client, {"web__port": "9090"}))
    text = config_path.read_text(encoding="utf-8")
    assert "user_agent" not in text
    assert "port = 9090" in text


def test_an_integer_typed_into_a_float_field_is_left_alone(client, app):
    """30 and 30.0 are the same setting; rewriting one as the other is churn."""
    _, config_path, _ = app
    config_path.write_text(config_path.read_text(encoding="utf-8")
                           + "\ntimeout_s = 30\n", encoding="utf-8")
    # (appended to the last table, which is [web] - fine, we only care that the
    # value is unchanged by a save elsewhere)
    before = config_path.read_text(encoding="utf-8")
    client.post("/config", data=form(client))
    assert config_path.read_text(encoding="utf-8") == before


def test_only_the_edited_line_moves(client, app):
    """Editing a key the file already has rewrites that line and no other."""
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8").splitlines()
    client.post("/config", data=form(client, {"web__port": "9090"}))
    after = config_path.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [("port = 8080", "port = 9090")]


def test_a_setting_the_file_lacks_is_added_as_one_line(client, app):
    """And a key that is not in the file yet costs exactly one new line."""
    _, config_path, _ = app
    before = config_path.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("max_images") for line in before)
    client.post("/config", data=form(client, {"scoring__max_images": "5"}))
    after = config_path.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before) + 1
    assert set(after) - set(before) == {"max_images = 5"}


def test_the_error_banner_names_the_fields(client):
    """A long page of settings needs to say which one is wrong."""
    resp = client.post("/config", data=form(client, {"web__port": "eighty",
                                                     "scoring__max_images": "two"}))
    banner = resp.get_data(as_text=True)
    assert "check scoring.max_images, web.port" in banner


def test_the_searches_banner_counts_from_one(client):
    """"url-0" is how the form is wired; it is not what to show a person."""
    resp = client.post("/searches/save", data=search_form(client, {"url-0": ""}))
    assert "search 1: url" in resp.get_data(as_text=True)


# --- the flags, from the page --------------------------------------------

def test_ignoring_a_listing_from_its_page(client, app):
    _, config_path, _ = app
    assert client.post("/listing/2847612345/ignore").status_code == 302
    with opened(config_path) as conn:
        assert conn.execute("SELECT ignored FROM listings WHERE id = '2847612345'"
                            ).fetchone()["ignored"] == 1
    assert b"not appear in any email" in client.get("/listing/2847612345").get_data()


def test_un_ignoring_a_listing(client, app):
    _, config_path, _ = app
    client.post("/listing/2847612345/ignore")
    client.post("/listing/2847612345/unignore")
    with opened(config_path) as conn:
        assert conn.execute("SELECT ignored FROM listings WHERE id = '2847612345'"
                            ).fetchone()["ignored"] == 0


@pytest.mark.parametrize("action, column", [("refetch", "needs_refetch"),
                                            ("rescore", "needs_rescore")])
def test_queueing_a_listing_by_hand(client, app, action, column):
    _, config_path, _ = app
    assert client.post(f"/listing/2847612345/{action}").status_code == 302
    with opened(config_path) as conn:
        assert conn.execute(f"SELECT {column} FROM listings WHERE id = '2847612345'"
                            ).fetchone()[column] == 1


def test_an_unknown_action_is_a_404(client):
    assert client.post("/listing/2847612345/delete").status_code == 404


def test_an_action_on_a_listing_that_does_not_exist_is_a_404(client):
    assert client.post("/listing/9999999999/ignore").status_code == 404


def test_ignored_listings_leave_the_default_view(client, app):
    assert b"2847612345" in client.get("/listings").get_data()
    client.post("/listing/2847612345/ignore")
    assert b"2847612345" not in client.get("/listings").get_data()
    # but they are still findable
    assert b"2847612345" in client.get("/listings?state=ignored").get_data()


@pytest.mark.parametrize("state", ["ignored", "needs_refetch", "needs_rescore"])
def test_each_flag_has_a_filter(client, app, state):
    _, config_path, _ = app
    assert client.get(f"/listings?state={state}").status_code == 200
    action = {"ignored": "ignore", "needs_refetch": "refetch",
              "needs_rescore": "rescore"}[state]
    client.post(f"/listing/2847612345/{action}")
    assert b"2847612345" in client.get(f"/listings?state={state}").get_data()


def test_the_dashboard_counts_what_is_queued(client, app):
    assert b"to re-fetch" not in client.get("/status-fragment").get_data()
    client.post("/listing/2847612345/refetch")
    body = client.get("/status-fragment").get_data(as_text=True)
    assert "1 to re-fetch" in body
    assert "1 to re-score" in body


def test_editing_preferences_marks_the_listings_and_says_so(client, app):
    _, config_path, tmp_path = app
    # The first save only records the file; it is not a change to it.
    client.post("/preferences", data={"text": "# Want\n\nA GS.\n"})
    resp = client.post("/preferences", data={"text": "# Want\n\nA GS under 6000.\n"},
                       follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "marked for re-scoring" in body
    assert "costs credits" in body
    with opened(config_path) as conn:
        assert conn.execute("SELECT SUM(needs_rescore) n FROM listings").fetchone()["n"] == 2


def test_saving_the_same_preferences_marks_nothing(client, app):
    _, config_path, _ = app
    client.post("/preferences", data={"text": "# Want\n\nA GS.\n"})
    resp = client.post("/preferences", data={"text": "# Want\n\nA GS.\n"},
                       follow_redirects=True)
    assert b"nothing was marked" in resp.get_data()
    with opened(config_path) as conn:
        assert conn.execute("SELECT SUM(needs_rescore) n FROM listings").fetchone()["n"] == 0


# --- the listing page's panels -------------------------------------------

def test_a_fact_is_not_shown_twice(client, app):
    """The reason the panels were rearranged: every attribute used to be listed
    again underneath the typed fields it had already become."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET attributes_json = ? WHERE id = '2847612345'",
                     ('{"Kilometerstand": "18.400 km", "Getriebe": "Manuell"}',))
        conn.commit()
    page = client.get("/listing/2847612345").get_data(as_text=True)
    assert page.count("Kilometerstand") == 0, "already shown as mileage"
    assert "Getriebe" not in page or "Manuell" in page


def test_an_attribute_with_no_field_is_still_shown(client, app):
    """It is how a new Kleinanzeigen attribute gets noticed."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET attributes_json = ? WHERE id = '2847612345'",
                     ('{"Sonderausstattung": "Koffersatz"}',))
        conn.commit()
    page = client.get("/listing/2847612345").get_data(as_text=True)
    assert "Sonderausstattung" in page and "Koffersatz" in page


def test_a_value_that_would_not_parse_is_still_shown(client, app):
    """"HU: Neu" is mapped to a column and is not a date. It appears in the HU
    row itself, rather than being filtered out as "already shown" - which would
    lose the only thing the ad said about the TUEV."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET attributes_json = ?, inspection_until = NULL "
                     "WHERE id = '2847612345'", ('{"HU": "Neu"}',))
        conn.commit()
    assert b"Neu" in client.get("/listing/2847612345").get_data()


def test_the_derived_panel_shows_what_it_can(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET km = 56000, first_reg_date = '2004-08-01', "
                     "postcode = '80331' WHERE id = '2847612345'")
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "km per year" in body
    assert "derived figures" in body


def test_distance_says_when_no_home_is_set(client, app):
    """The row is always there; what changes is whether it holds a number. This
    is the one blank worth explaining - nothing about the listing is wrong."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET postcode = '80331' WHERE id = '2847612345'")
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "distance from home" in body
    assert "no home_plz set" in body

    config_path.write_text('home_plz = "22765"\n' + config_path.read_text(encoding="utf-8"),
                           encoding="utf-8")
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "no home_plz set" not in body
    assert "km" in body


def test_a_listing_with_a_postcode_we_do_not_know_says_so(client, app):
    _, config_path, _ = app
    config_path.write_text('home_plz = "22765"\n' + config_path.read_text(encoding="utf-8"),
                           encoding="utf-8")
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET postcode = NULL, location = NULL "
                     "WHERE id = '2847612345'")
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    figures = body[body.index("<h2>derived figures</h2>"):body.index("<h2>description</h2>")]
    assert "<dt>distance from home</dt>" in figures
    assert "unknown" in figures


def test_every_listing_shows_the_same_rows(client, app):
    """Two listings have to be comparable, so a field the ad never filled in
    says so rather than vanishing."""
    from karpm.web.app import SPECIFICATIONS
    _, config_path, _ = app
    with opened(config_path) as conn:
        # attributes_json too: otherwise the rows show what the ad said, which
        # is the right behaviour and not what this test is about.
        conn.execute("UPDATE listings SET km = NULL, hp = NULL, ccm = NULL, "
                     "owners = NULL, drive_type = NULL, transmission = NULL, "
                     "condition = NULL, inspection_until = NULL, bike_type = NULL, "
                     "first_reg_date = NULL, price_eur = NULL, equipment_json = NULL, "
                     "attributes_json = '{}' WHERE id = '2847612345'")
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    specs = body[body.index("<h2>specifications</h2>"):body.index("<h2>score</h2>")]
    for label, _, _, _ in SPECIFICATIONS:
        assert f"<dt>{label}</dt>" in specs, label
    # Every one of them is marked as not being a value.
    assert specs.count('class="absent"') == len(SPECIFICATIONS)
    # Every one of them says so in the same word, bar the equipment list.
    assert specs.count(">unknown</dd>") == len(SPECIFICATIONS) - 1


def test_what_the_ad_said_is_shown_even_when_it_would_not_parse(client, app):
    """"HU: Neu" is something the ad said. Reporting the column as "not stated"
    would be a second way of losing it."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET inspection_until = NULL, attributes_json = ? "
                     "WHERE id = '2847612345'", ('{"HU": "Neu"}',))
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    specs = body[body.index("<h2>specifications</h2>"):body.index("<h2>score</h2>")]
    assert "<dt>HU until</dt>" in specs
    assert ">Neu</dd>" in specs, "the ad's own word, not 'unknown' and not annotated"


def test_a_missing_derived_figure_reads_unknown(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET km = NULL, inspection_until = NULL, "
                     "posted_at = NULL WHERE id = '2847612345'")
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    figures = body[body.index("<h2>derived figures</h2>"):body.index("<h2>description</h2>")]
    for label in ("km per year", "HU remaining"):
        assert f"<dt>{label}</dt>" in figures
    assert figures.count(">unknown</dd>") >= 2


def test_an_unchanged_price_reads_none(client, app):
    body = client.get("/listing/2847612345").get_data(as_text=True)
    figures = body[body.index("<h2>derived figures</h2>"):body.index("<h2>description</h2>")]
    assert "<dt>price change</dt>" in figures
    assert ">none</dd>" in figures


def test_a_changed_price_is_short(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.add_history(conn, "2847612345", "price_change", price_eur=5400,
                       prev_price_eur=5900)
        conn.commit()
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "-500 € (8%)" in body


def test_there_is_no_licence_plate_field(client, app):
    """Reading a plate out of prose was guesswork; the photos are the AI's job."""
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "licence plate" not in body
    assert "seasonal registration" not in body


def test_the_panels_are_named_as_asked(client):
    body = client.get("/listing/2847612345").get_data(as_text=True)
    for heading in ("specifications", "derived figures", "miscellaneous figures"):
        assert f"<h2>{heading}</h2>" in body


def test_the_price_is_a_specification(client, app):
    _, config_path, _ = app
    body = client.get("/listing/2847612345").get_data(as_text=True)
    specs = body[body.index("<h2>specifications</h2>"):body.index("<h2>score</h2>")]
    assert "<dt>price</dt>" in specs
    assert "5.900" in specs


def test_a_negotiable_price_is_marked(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        conn.execute("UPDATE listings SET price_kind = 'vb' WHERE id = '2847612345'")
        conn.commit()
    assert "5.900 € VB" in client.get("/listing/2847612345").get_data(as_text=True)


# --- clearing the database from the dashboard ----------------------------

def test_reset_clears_the_listings(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] > 0

    assert client.post("/control/reset").status_code == 302

    with opened(config_path) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 0


def test_reset_says_what_it_removed(client, app):
    """A destructive action that reports nothing leaves you unsure it happened."""
    body = client.post("/control/reset", follow_redirects=True).get_data(as_text=True)
    assert "database cleared" in body
    assert "listing(s)" in body and "photo(s)" in body


def test_reset_leaves_the_config_alone(client, app):
    _, config_path, tmp_path = app
    before = config_path.read_text(encoding="utf-8")
    preferences = (tmp_path / "preferences.md").read_text(encoding="utf-8")
    client.post("/control/reset")
    assert config_path.read_text(encoding="utf-8") == before
    assert (tmp_path / "preferences.md").read_text(encoding="utf-8") == preferences


def test_reset_refuses_while_the_daemon_is_working(client, app):
    """A wipe mid-scrape would be undone by the rows it is about to write."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.queue_command(conn, "scrape")
        db.claim_command(conn)
        db.set_state(conn, "heartbeat", db.utcnow())    # genuinely at work

    body = client.post("/control/reset", follow_redirects=True).get_data(as_text=True)
    assert "nothing was deleted" in body
    with opened(config_path) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] > 0


def test_a_command_left_running_by_a_dead_daemon_does_not_block_the_reset(client, app):
    """Otherwise one crash mid-command locks the button for good."""
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.queue_command(conn, "scrape")
        db.claim_command(conn)
        db.set_state(conn, "heartbeat", "2020-01-01T00:00:00+00:00")   # long gone

    body = client.post("/control/reset", follow_redirects=True).get_data(as_text=True)
    assert "database cleared" in body
    with opened(config_path) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == 0


def test_a_stale_command_is_marked_failed_not_left_running(client, app):
    _, config_path, _ = app
    with opened(config_path) as conn:
        db.queue_command(conn, "scrape")
        db.claim_command(conn)
        db.set_state(conn, "heartbeat", "2020-01-01T00:00:00+00:00")
    client.post("/control/reset")
    # The reset clears the table, so what matters is that it got that far.
    with opened(config_path) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"] == 0


def test_the_reset_button_is_red_and_asks_first(client):
    body = client.get("/").get_data(as_text=True)
    panel = body[body.index("<h2>start over</h2>"):]
    assert 'class="danger"' in panel
    assert "onsubmit=\"return confirm(" in panel
    assert "no undo" in panel.lower() or "There is no undo" in panel


def test_the_status_panel_shows_the_scoring_slot(client, app):
    _, config_path, _ = app
    body = client.get("/status-fragment").get_data(as_text=True)
    assert "next score" in body
    assert "with each scrape" in body, "no slots set, so scoring rides along"

    with opened(config_path) as conn:
        db.set_state(conn, "next_score", "2026-09-15T08:00:00")
    assert "2026-09-15T08:00" in client.get("/status-fragment").get_data(as_text=True)


def test_heartbeat_is_the_first_field_in_the_schedule_panel(client):
    from karpm.web import fields
    schedule = fields.section("schedule")
    assert schedule.fields[0].key == "heartbeat_s"
    body = client.get("/config").get_data(as_text=True)
    panel = body[body.index("<h2>schedule</h2>"):]
    assert panel.index("schedule__heartbeat_s") < panel.index("schedule__scrape_at")


# --- what the reading passes found ---------------------------------------

def _store_findings(conf_path, listing_id="2847612345"):
    """As a real pass would leave it: the findings, and the keys they were made
    from. A fabricated key reads as stale, which is its own kind of correct."""
    conn = opened(conf_path)
    row = db.get_listing(conn, listing_id)
    db.save_extraction(
        conn, listing_id, "text",
        {"summary": "A tidy MT-07 with a fresh service.",
         "known_faults": ["scratched left mirror"],
         "recent_work": [{"what": "chain and sprockets", "when": "40000 km",
                          "quote": "Kette und Ritzel bei 40tkm neu"}],
         "selling_reason": "buying a bigger bike", "negotiable": True},
        provider="anthropic", model="claude-haiku-4-5", prompt_version="v1",
        source_hash=row["content_hash"])
    db.save_extraction(
        conn, listing_id, "photos",
        {"condition_summary": "Clean, photographed in a garage.",
         "visible_issues": ["surface rust on the downpipe"],
         "photo_notes": [{"position": 0, "shows": "left side",
                          "concern": "scuffed bar end"}],
         "shortlist": [0]},
        provider="anthropic", model="claude-haiku-4-5", prompt_version="v1",
        source_hash=db.image_set_hash(conn, listing_id))
    conn.commit()
    conn.close()


def test_a_listing_page_shows_what_the_passes_found(client, app):
    _, config_path, _ = app
    _store_findings(config_path)
    body = client.get("/listing/2847612345").get_data(as_text=True)

    assert "A tidy MT-07 with a fresh service." in body
    assert "scratched left mirror" in body
    assert "Kette und Ritzel bei 40tkm neu" in body      # the quote to check it against
    assert "surface rust on the downpipe" in body
    # Which model said it and when: a finding with no provenance is a rumour.
    assert "claude-haiku-4-5" in body


def test_findings_are_not_dressed_up_as_facts(client, app):
    """They are one model's reading of what a seller wrote. The page has to say
    so, or they are indistinguishable from the mileage off the ad."""
    _, config_path, _ = app
    _store_findings(config_path)
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "Claims, not checked facts." in body


def test_the_shortlisted_photos_are_marked_in_the_gallery(client, app):
    _, config_path, _ = app
    _store_findings(config_path)
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert "shortlisted" in body
    assert "scuffed bar end" in body        # the per-photo note, beside its photo


def test_a_listing_nothing_has_read_yet_renders_without_the_panels(client):
    """Every listing predates these passes, and most will be waiting for them."""
    body = client.get("/listing/2847612345").get_data(as_text=True)
    assert body.count("read from the description") == 0
    assert "specifications" in body


def test_the_dashboard_says_how_much_is_still_unread(client, app):
    """A pass that has quietly stopped and one that has read everything look
    the same from the outside unless the page says which."""
    _, config_path, _ = app
    body = client.get("/").get_data(as_text=True)
    assert "2 description" in body and "2 photos" in body

    _store_findings(config_path)                     # one of the two, read
    after = client.get("/status-fragment").get_data(as_text=True)
    assert "1 description" in after and "1 photos" in after


def test_a_pass_switched_off_says_so_rather_than_reading_as_done(client, app):
    _, config_path, _ = app
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[extract_photos]\nenabled = false\n",
        encoding="utf-8")
    body = client.get("/status-fragment").get_data(as_text=True)
    assert "photos pass off" in body


# --- picking a model -----------------------------------------------------

def test_the_model_is_a_dropdown_of_what_we_know(client):
    """Typed by hand it was a way to name a model the code cannot reason about
    - and one of them rejects the parameters every request was sending."""
    body = client.get("/config").get_data(as_text=True)
    assert 'name="scoring__model"' in body and "<select" in body
    for model_id in ("claude-haiku-4-5", "claude-opus-5"):
        assert f'value="{model_id}"' in body
    # Priced, because the id alone does not say what the choice costs.
    assert "$1/$5 per Mtok" in body and "$5/$25 per Mtok" in body


def test_effort_says_which_models_it_applies_to(client):
    """Haiku takes no effort level and errors when sent one, so the row is
    hidden for it rather than offered as a way to break every call."""
    body = client.get("/config").get_data(as_text=True)
    assert 'data-shown-for="claude-sonnet-5 claude-opus-5 claude-fable-5-1"' in body
    assert "claude-haiku-4-5" not in re.search(
        r'data-shown-for="([^"]*)"', body).group(1)


def test_a_model_set_by_hand_is_offered_back_rather_than_replaced(client, app):
    """The dropdown suggests; it does not decide. Dropping an unknown value
    would swap a deliberate choice for whichever option came first, on a save
    that was about something else entirely."""
    _, config_path, _ = app
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[scoring]", '[scoring]\nmodel = "claude-opus-9-unreleased"'),
        encoding="utf-8")

    body = client.get("/config").get_data(as_text=True)
    assert 'value="claude-opus-9-unreleased" selected' in body
    assert "not in the list" in body


def test_saving_keeps_a_model_the_dropdown_has_never_heard_of(client, app):
    _, config_path, _ = app
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[scoring]", '[scoring]\nmodel = "claude-opus-9-unreleased"'),
        encoding="utf-8")

    data = form(client, {"scoring__max_per_run": "42"})   # a save about something else
    assert client.post("/config", data=data).status_code == 302

    conf = load_config(config_path)
    assert conf.scoring.model == "claude-opus-9-unreleased"
    assert conf.scoring.max_per_run == 42


def test_a_blank_model_is_refused_rather_than_written(client, app):
    _, config_path, _ = app
    response = client.post("/config", data=form(client, {"scoring__model": ""}))
    assert response.status_code == 200          # back with the error, not saved
    assert "this cannot be empty" in response.get_data(as_text=True)
    assert load_config(config_path).scoring.model == "claude-opus-5"
