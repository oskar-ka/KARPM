"""The three passes: what they are asked, what gets stored, and when they rerun.

Nothing here talks to an API. A fake provider stands in for one, which is the
point of the seam - a pass that could only be tested against Anthropic would be
a pass nobody could put another model behind.
"""

from pathlib import Path

import pytest

from karpm import db, pipeline
from karpm.ai import extract, passes, provider
from karpm.config import Config

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


class FakeProvider:
    """Answers whatever it was told to, and remembers what it was asked."""

    name = "fake"

    def __init__(self, answers=None, vision=True, fail=False):
        self.answers = answers or {}
        self.vision = vision
        self.fail = fail
        self.seen: list[provider.Request] = []

    def supports_images(self) -> bool:
        return self.vision

    def complete(self, request):
        self.seen.append(request)
        if self.fail:
            raise provider.ProviderError("no")
        kind = "photos" if request.has_images else "text"
        return provider.Reply(data=dict(self.answers.get(kind, {})),
                              model=request.model, input_tokens=10, output_tokens=5)


TEXT_ANSWER = {
    "summary": "A tidy GS with a fresh service.",
    "recent_work": [{"what": "chain and sprockets", "when": "40000 km",
                     "quote": "Kette und Ritzel bei 40tkm neu"}],
    "known_faults": ["a dent in the left pannier"],
    "red_flags": [],
    "selling_reason": "buying a bigger bike",
    "negotiable": True,
    "modifications": [],
    "included_extras": ["topcase"],
    "questions_to_ask": ["has the final drive been serviced?"],
}

PHOTO_ANSWER = {
    "condition_summary": "Clean, photographed in a garage.",
    "visible_issues": ["surface rust on the downpipe"],
    "positives": ["tyres look barely worn"],
    "photo_notes": [{"position": 0, "shows": "left side", "concern": None}],
    "shortlist": [0, 2],
    "coverage_gaps": ["nothing of the chain"],
    "photo_quality": "good enough",
}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    return c


def listing(conn, listing_id="111", description="Kette und Ritzel bei 40tkm neu."):
    db.upsert_listing(conn, {"id": listing_id, "url": f"https://x/{listing_id}",
                             "title": "BMW R 1200 GS", "description": description,
                             "price_eur": 6900, "search_name": "gs"})
    conn.commit()
    return db.get_listing(conn, listing_id)


def with_photos(conn, tmp_path, listing_id="111", count=3):
    for position in range(count):
        db.add_image(conn, listing_id, position, f"https://cdn/{listing_id}/{position}.jpg")
    conn.commit()
    for image in db.listing_images(conn, listing_id):
        path = tmp_path / f"{listing_id}-{image['position']}.png"
        path.write_bytes(PNG)
        db.record_image_download(conn, image["id"], str(path), "sha", len(PNG))
    conn.commit()


# --- what a pass is asked -------------------------------------------------

def test_the_text_pass_is_given_the_ad_and_nothing_else(conn):
    listing(conn)
    fake = FakeProvider({"text": TEXT_ANSWER})
    extract.run_pass(conn, "text", Config().extract_text, fake)

    assert len(fake.seen) == 1
    request = fake.seen[0]
    assert not request.has_images, "pass 1 is text-only, and may run on a model with no vision"
    assert "Kette und Ritzel" in request.blocks[0]["text"]
    assert "6900" in request.blocks[0]["text"]


def test_the_photo_pass_sends_the_photos_with_their_positions(conn, tmp_path):
    listing(conn)
    with_photos(conn, tmp_path)
    fake = FakeProvider({"photos": PHOTO_ANSWER})
    extract.run_pass(conn, "photos", Config().extract_photos, fake)

    request = fake.seen[0]
    images = [b for b in request.blocks if b["type"] == "image"]
    assert len(images) == 3
    # Without the positions in the text, a shortlist of numbers means nothing.
    assert "position 2" in "".join(b.get("text", "") for b in request.blocks)


def test_the_photo_pass_looks_at_no_more_than_it_was_told_to(conn, tmp_path):
    listing(conn)
    with_photos(conn, tmp_path, count=9)
    cfg = Config().extract_photos
    cfg.max_photos_in = 4
    fake = FakeProvider({"photos": PHOTO_ANSWER})
    extract.run_pass(conn, "photos", cfg, fake)

    assert len([b for b in fake.seen[0].blocks if b["type"] == "image"]) == 4


def test_an_ad_with_no_downloaded_photos_is_not_asked_about(conn):
    listing(conn)
    db.add_image(conn, "111", 0, "https://cdn/111/0.jpg")     # never fetched
    conn.commit()
    fake = FakeProvider({"photos": PHOTO_ANSWER})
    result = extract.run_pass(conn, "photos", Config().extract_photos, fake)

    assert fake.seen == [] and result["done"] == 0


# --- what gets stored -----------------------------------------------------

def test_findings_are_stored_beside_the_listing_not_in_it(conn):
    row = listing(conn)
    extract.run_pass(conn, "text", Config().extract_text,
                     FakeProvider({"text": TEXT_ANSWER}))

    after = db.get_listing(conn, "111")
    assert after["description"] == row["description"], "the ad's own text is untouched"
    found = db.get_extraction(conn, "111", "text")
    assert found["data"]["summary"] == TEXT_ANSWER["summary"]
    assert found["provider"] == "fake" and found["input_tokens"] == 10


def test_a_second_run_replaces_rather_than_piles_up(conn):
    listing(conn)
    cfg = Config().extract_text
    for _ in range(2):
        cfg.prompt_version = f"v{_ + 1}"
        extract.run_pass(conn, "text", cfg, FakeProvider({"text": TEXT_ANSWER}))

    rows = conn.execute("SELECT * FROM extractions WHERE listing_id = '111'").fetchall()
    # The stored version carries the prompt's own hash after the configured
    # one, which is what makes editing a prompt re-read everything.
    assert len(rows) == 1 and rows[0]["prompt_version"].startswith("v2+")


def test_a_failed_pass_stores_nothing(conn):
    """A row saying "read, found nothing" would stop it ever being read again."""
    listing(conn)
    result = extract.run_pass(conn, "text", Config().extract_text,
                              FakeProvider(fail=True))

    assert result["failed"] == 1 and result["done"] == 0
    assert db.get_extraction(conn, "111", "text") is None


# --- when a pass runs again ------------------------------------------------

def test_a_listing_already_read_is_not_read_again(conn):
    listing(conn)
    cfg = Config().extract_text
    extract.run_pass(conn, "text", cfg, FakeProvider({"text": TEXT_ANSWER}))
    fake = FakeProvider({"text": TEXT_ANSWER})
    assert extract.run_pass(conn, "text", cfg, fake)["done"] == 0
    assert fake.seen == []


def test_an_edited_ad_is_read_again_but_its_photos_are_not(conn, tmp_path):
    """The two passes go stale for different reasons; that is why they are two."""
    listing(conn)
    with_photos(conn, tmp_path)
    conf = Config()
    extract.run_pass(conn, "text", conf.extract_text, FakeProvider({"text": TEXT_ANSWER}))
    extract.run_pass(conn, "photos", conf.extract_photos,
                     FakeProvider({"photos": PHOTO_ANSWER}))

    listing(conn, description="Now with a new exhaust.")

    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", conf.extract_text)) == 1
    assert db.extraction_backlog(conn, "photos", passes.prompt_version("photos", conf.extract_photos)) == 0


def test_a_new_photo_sends_the_photos_back_but_not_the_text(conn, tmp_path):
    listing(conn)
    with_photos(conn, tmp_path)
    conf = Config()
    extract.run_pass(conn, "text", conf.extract_text, FakeProvider({"text": TEXT_ANSWER}))
    extract.run_pass(conn, "photos", conf.extract_photos,
                     FakeProvider({"photos": PHOTO_ANSWER}))

    db.add_image(conn, "111", 7, "https://cdn/111/7.jpg")
    conn.commit()
    image = db.listing_images(conn, "111")[-1]
    path = tmp_path / "extra.png"
    path.write_bytes(PNG)
    db.record_image_download(conn, image["id"], str(path), "sha", len(PNG))
    conn.commit()

    assert db.extraction_backlog(conn, "photos", passes.prompt_version("photos", conf.extract_photos)) == 1
    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", conf.extract_text)) == 0


def test_a_listing_waiting_to_be_refetched_is_left_alone(conn):
    """Its stored text is known to be out of date; reading it now buys an
    answer about words that are about to be replaced."""
    listing(conn)
    db.mark_for_refetch(conn, "111")
    fake = FakeProvider({"text": TEXT_ANSWER})
    extract.run_pass(conn, "text", Config().extract_text, fake)
    assert fake.seen == []


# --- the shortlist ---------------------------------------------------------

def test_a_shortlist_naming_photos_that_do_not_exist_is_dropped(conn, tmp_path):
    listing(conn)
    with_photos(conn, tmp_path, count=2)
    answer = {**PHOTO_ANSWER, "shortlist": [0, 44]}
    extract.run_pass(conn, "photos", Config().extract_photos,
                     FakeProvider({"photos": answer}))

    assert db.get_extraction(conn, "111", "photos")["data"]["shortlist"] == [0]


def test_a_shortlist_longer_than_asked_for_is_cut(conn, tmp_path):
    listing(conn)
    with_photos(conn, tmp_path, count=6)
    cfg = Config().extract_photos
    cfg.shortlist = 2
    answer = {**PHOTO_ANSWER, "shortlist": [5, 4, 3, 2]}
    extract.run_pass(conn, "photos", cfg, FakeProvider({"photos": answer}))

    assert db.get_extraction(conn, "111", "photos")["data"]["shortlist"] == [5, 4]


def test_picking_none_falls_back_to_the_first_few(conn, tmp_path):
    """Sending pass 3 no photographs at all is worse than sending it arbitrary
    ones, which is what it had before any of this existed."""
    listing(conn)
    with_photos(conn, tmp_path, count=5)
    cfg = Config().extract_photos
    cfg.shortlist = 3
    extract.run_pass(conn, "photos", cfg,
                     FakeProvider({"photos": {**PHOTO_ANSWER, "shortlist": []}}))

    assert db.get_extraction(conn, "111", "photos")["data"]["shortlist"] == [0, 1, 2]


# --- switches --------------------------------------------------------------

def test_a_disabled_pass_does_not_call_anything(conn, caplog):
    listing(conn)
    cfg = Config().extract_text
    cfg.enabled = False
    fake = FakeProvider({"text": TEXT_ANSWER})
    with caplog.at_level("INFO"):
        result = extract.run_pass(conn, "text", cfg, fake)

    assert result["skipped"] == "disabled" and fake.seen == []
    assert "disabled" in caplog.text          # silence reads as "it ran and found nothing"


def test_a_pass_switched_off_mid_run_stops_at_the_next_listing(conn, caplog):
    """Every listing is an API call, so stopping now rather than at the end of
    the queue is the difference between one more and two hundred more."""
    for n in range(4):
        listing(conn, listing_id=str(n), description=f"ad number {n}")
    fake = FakeProvider({"text": TEXT_ANSWER})
    calls = []

    def still_enabled():
        calls.append(1)
        return len(calls) <= 2

    with caplog.at_level("WARNING"):
        result = extract.run_pass(conn, "text", Config().extract_text, fake,
                                  still_enabled=still_enabled)

    assert result["done"] == 2 and len(fake.seen) == 2
    assert "switched off mid-run" in caplog.text


def test_the_photo_pass_refuses_a_provider_that_cannot_see(conn, tmp_path, caplog):
    listing(conn)
    with_photos(conn, tmp_path)
    fake = FakeProvider({"photos": PHOTO_ANSWER}, vision=False)
    with caplog.at_level("ERROR"):
        result = extract.run_pass(conn, "photos", Config().extract_photos, fake)

    assert result["done"] == 0 and fake.seen == []
    assert "cannot read images" in caplog.text


def test_nothing_due_asks_for_no_provider_at_all(conn, monkeypatch):
    """An empty queue must not need an API key: a run that only scrapes would
    otherwise fail on a machine that has none."""
    monkeypatch.setattr(provider, "get", lambda name: pytest.fail("built a provider"))
    assert extract.run_pass(conn, "text", Config().extract_text)["done"] == 0


# --- the three of them together -------------------------------------------

def test_run_once_reads_before_it_scores(conn, tmp_path, monkeypatch):
    """Pass 3 scoring on findings pass 1 never made would be the worst of both:
    the cost of three models and the evidence of one."""
    from karpm import scoring
    listing(conn)
    with_photos(conn, tmp_path)
    conf = Config(db_path=str(tmp_path / "t.db"))
    conf.scoring.enabled = True
    order = []

    monkeypatch.setattr(pipeline, "run_scrape", lambda *a, **k: order.append("scrape") or {})

    def fake_score_pending(conn_, cfg, home_plz=None, still_enabled=None):
        order.append("score")
        # By the time pass 3 runs, what the first two found is on the row.
        assert set(db.extractions_for(conn_, "111")) == {"text", "photos"}
        return []

    monkeypatch.setattr(scoring, "score_pending", fake_score_pending)
    pipeline.run_once(conf, conn, client=FakeProvider(
        {"text": TEXT_ANSWER, "photos": PHOTO_ANSWER}))

    assert order == ["scrape", "score"]


def test_scoring_slots_hold_back_the_reading_too(conn, tmp_path, monkeypatch):
    """Setting times was a decision about when the money is spent, and passes 1
    and 2 spend it as surely as pass 3 does."""
    listing(conn)
    conf = Config(db_path=str(tmp_path / "t.db"))
    conf.schedule.score_at = ["08:00"]
    monkeypatch.setattr(pipeline, "run_scrape", lambda *a, **k: {})
    fake = FakeProvider({"text": TEXT_ANSWER})

    pipeline.run_once(conf, conn, client=fake)
    assert fake.seen == []


def test_reading_happens_even_with_scoring_off(conn, tmp_path, monkeypatch):
    """Each pass owns its own switch. The findings are worth having on the
    listing page whether or not anything is being scored."""
    listing(conn)
    conf = Config(db_path=str(tmp_path / "t.db"))
    conf.scoring.enabled = False
    monkeypatch.setattr(pipeline, "run_scrape", lambda *a, **k: {})
    fake = FakeProvider({"text": TEXT_ANSWER})

    pipeline.run_once(conf, conn, client=fake)
    assert db.get_extraction(conn, "111", "text") is not None


def test_show_prompt_includes_what_the_passes_found(conn, tmp_path, capsys):
    """--show-prompt exists to show what will really be sent. Assembled
    separately from the real prompt, it would drift from it and say so
    confidently."""
    from karpm import cli, scoring
    listing(conn)
    with_photos(conn, tmp_path)
    conf = Config()
    extract.run_pass(conn, "text", conf.extract_text, FakeProvider({"text": TEXT_ANSWER}))
    extract.run_pass(conn, "photos", conf.extract_photos,
                     FakeProvider({"photos": PHOTO_ANSWER}))

    scorer = scoring.Scorer(conf.scoring, "a cheap GS", client=FakeProvider())
    text = cli._prompt_text(scorer, conn, db.get_listing(conn, "111"))

    assert "A tidy GS with a fresh service." in text
    assert "Clean, photographed in a garage." in text


def test_the_scoring_pass_is_shown_the_shortlist_not_the_first_few(conn, tmp_path):
    """The whole point of pass 2: without it pass 3 sees whatever the seller
    happened to upload first."""
    from karpm import scoring
    listing(conn)
    with_photos(conn, tmp_path, count=6)
    conf = Config()
    conf.scoring.max_images = 2
    extract.run_pass(conn, "photos", conf.extract_photos,
                     FakeProvider({"photos": {**PHOTO_ANSWER, "shortlist": [4, 5]}}))

    scorer = scoring.Scorer(conf.scoring, "a cheap GS", client=FakeProvider())
    request = scorer.build_request(conn, db.get_listing(conn, "111"))
    chosen = [b["path"] for b in request.blocks if b["type"] == "image"]

    assert [Path(p).name for p in chosen] == ["111-4.png", "111-5.png"]


# --- extract-one -----------------------------------------------------------

def _config_for(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'db_path = "{tmp_path / "t.db"}"\n', encoding="utf-8")
    return config


@pytest.fixture
def one(conn, tmp_path):
    """A database with one listing and its photos, and a config pointing at it."""
    listing(conn)
    with_photos(conn, tmp_path)
    conn.commit()
    return _config_for(tmp_path)


@pytest.fixture
def run_cli(monkeypatch):
    """`karpm ...` with the fake provider behind whatever it asks for."""
    def run(config, *argv, fake=None):
        from karpm import cli
        if fake is not None:
            monkeypatch.setattr(provider, "get", lambda name: fake)
        return cli.main(["-c", str(config), *argv])
    return run


def test_extract_one_reads_the_listing_you_name(one, capsys, run_cli):
    fake = FakeProvider({"text": TEXT_ANSWER, "photos": PHOTO_ANSWER})
    assert run_cli(one, "extract-one", "111", fake=fake) == 0

    out = capsys.readouterr().out
    assert "A tidy GS with a fresh service." in out
    assert "Clean, photographed in a garage." in out
    assert len(fake.seen) == 2


def test_extract_one_stores_nothing_unless_you_ask(one, conn, run_cli):
    """The point of it is trying a prompt change, and a trial that overwrites
    the stored reading is not a trial."""
    run_cli(one, "extract-one", "111", "--text-only",
         fake=FakeProvider({"text": TEXT_ANSWER}))
    assert db.get_extraction(conn, "111", "text") is None

    run_cli(one, "extract-one", "111", "--text-only", "--save",
         fake=FakeProvider({"text": TEXT_ANSWER}))
    assert db.get_extraction(conn, "111", "text")["data"]["summary"] == TEXT_ANSWER["summary"]


def test_extract_one_re_reads_something_already_read(one, conn, run_cli):
    """`extract` skips a listing that is up to date. Naming one by id is the
    explicit instruction, and re-reading is most of the point when you are
    editing a prompt."""
    conf = Config()
    extract.run_pass(conn, "text", conf.extract_text, FakeProvider({"text": TEXT_ANSWER}))
    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", conf.extract_text)) == 0

    fake = FakeProvider({"text": TEXT_ANSWER})
    run_cli(one, "extract-one", "111", "--text-only", fake=fake)
    assert len(fake.seen) == 1


def test_extract_one_show_prompt_costs_nothing(one, capsys, run_cli):
    fake = FakeProvider({"text": TEXT_ANSWER, "photos": PHOTO_ANSWER})
    assert run_cli(one, "extract-one", "111", "--show-prompt", fake=fake) == 0

    assert fake.seen == [], "--show-prompt must not call the API"
    out = capsys.readouterr().out
    assert "Kette und Ritzel" in out                 # the ad, as pass 1 gets it
    # The instructions, which are most of what is sent and were once left out
    # entirely - a prompt missing them looks like the whole thing.
    assert "You read German motorcycle classified ads" in out
    assert "You look at photographs of a used motorcycle" in out
    # Every photo named, so a shortlist can be checked against the files.
    assert out.count("[IMAGE: ") == 3
    # And the schema, which shapes the answer as surely as the words do.
    assert "RESPONSE SCHEMA" in out and '"known_faults"' in out


def test_show_prompt_fences_what_is_sent_from_what_is_ours(one, capsys, run_cli):
    """A preface is fine; a preface you cannot tell from the prompt is not."""
    fake = FakeProvider({"text": TEXT_ANSWER, "photos": PHOTO_ANSWER})
    run_cli(one, "extract-one", "111", "--text-only", "--show-prompt", fake=fake)
    out = capsys.readouterr().out

    assert "PREFACE - none of this is sent" in out
    for name in ("SYSTEM PROMPT", "USER MESSAGE", "RESPONSE SCHEMA"):
        assert f"*** {name} - SENT" in out
        assert f"*** END OF {name}" in out
    # Opening and closing fence for each of the three blocks.
    assert out.count("*" * 72) == 12


def test_extract_one_on_an_unknown_id_says_so(one, capsys, run_cli):
    assert run_cli(one, "extract-one", "9999999999") == 1
    assert "not found" in capsys.readouterr().err


def test_extract_one_says_when_there_is_nothing_to_look_at(tmp_path, capsys, run_cli):
    """An ad whose photos all failed to download is not an ad with no photos,
    and neither is a silent success."""
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    listing(conn)
    config = _config_for(tmp_path)

    assert run_cli(config, "extract-one", "111", "--photos-only",
                fake=FakeProvider()) == 0
    assert "nothing to look at" in capsys.readouterr().err


def test_extract_one_reports_a_failure_rather_than_exiting_clean(one, capsys, run_cli):
    assert run_cli(one, "extract-one", "111", "--text-only",
                fake=FakeProvider(fail=True)) == 1


# --- what each model is actually sent ---------------------------------------

class FakeAnthropic:
    """Just enough of the SDK to record what a request was built as."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 0})()
        parsed = type("P", (), {"model_dump": lambda self: {"summary": "ok"}})()
        return type("R", (), {"stop_reason": "end_turn", "parsed_output": parsed,
                              "model": kwargs["model"], "usage": usage})()


def _sent(model, effort="medium"):
    client = FakeAnthropic()
    engine = provider.AnthropicProvider(client=client)
    engine.complete(provider.Request(system="s", blocks=[provider.text("hi")],
                                     schema=None, model=model, effort=effort))
    return client.calls[0]


def test_a_model_without_adaptive_thinking_is_not_sent_it():
    """Haiku 4.5 takes neither adaptive thinking nor an effort level, and
    rejects the whole request rather than ignoring what it cannot use - so
    sending them failed every listing in the queue with a 400."""
    sent = _sent("claude-haiku-4-5")
    assert "thinking" not in sent and "output_config" not in sent
    assert sent["model"] == "claude-haiku-4-5"


def test_a_model_with_adaptive_thinking_is_sent_it():
    sent = _sent("claude-opus-5", effort="xhigh")
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "xhigh"}


def test_an_effort_the_model_does_not_have_falls_back_to_the_default():
    """Rather than passing a level the API would reject."""
    sent = _sent("claude-haiku-4-5", effort="max")
    assert "output_config" not in sent


def test_a_model_we_have_never_heard_of_is_assumed_to_be_a_new_one():
    """A model missing from the table is newer than the table, not older.
    Assuming otherwise would silently drop thinking from a model that has it."""
    sent = _sent("claude-opus-9-unreleased")
    assert sent["thinking"] == {"type": "adaptive"}


def test_every_default_model_is_one_the_catalogue_knows():
    """The provider decides what to send from the catalogue, so a default it
    has never heard of would be guessed at rather than known."""
    from karpm.ai import models
    conf = Config()
    for cfg in (conf.extract_text, conf.extract_photos, conf.scoring):
        assert models.get(cfg.model) is not None, cfg.model


def test_every_default_effort_is_one_its_default_model_accepts():
    from karpm.ai import models
    conf = Config()
    for cfg in (conf.extract_text, conf.extract_photos, conf.scoring):
        known = models.get(cfg.model)
        assert not known.effort or cfg.effort in known.effort, cfg.model


# --- editing a prompt -------------------------------------------------------

def test_a_prompt_file_replaces_the_built_in_one(conn, tmp_path):
    cfg = Config().extract_text
    cfg.prompt_file = str(tmp_path / "text.md")
    Path(cfg.prompt_file).write_text("Read it. Say what it claims.", encoding="utf-8")

    listing(conn)
    fake = FakeProvider({"text": TEXT_ANSWER})
    extract.run_pass(conn, "text", cfg, fake)
    assert fake.seen[0].system == "Read it. Say what it claims."


def test_an_empty_prompt_file_falls_back_rather_than_asking_nothing(conn, tmp_path):
    cfg = Config().extract_text
    cfg.prompt_file = str(tmp_path / "text.md")
    Path(cfg.prompt_file).write_text("   \n", encoding="utf-8")

    listing(conn)
    fake = FakeProvider({"text": TEXT_ANSWER})
    extract.run_pass(conn, "text", cfg, fake)
    assert fake.seen[0].system.startswith("You read German motorcycle")


def test_editing_the_prompt_makes_every_reading_stale(conn, tmp_path):
    """Nothing about a listing changes when you rewrite how you ask about it,
    so the findings have to be marked rather than left to be noticed - and
    nobody remembers to bump prompt_version by hand."""
    cfg = Config().extract_text
    cfg.prompt_file = str(tmp_path / "text.md")
    Path(cfg.prompt_file).write_text("Read it.", encoding="utf-8")

    listing(conn)
    extract.run_pass(conn, "text", cfg, FakeProvider({"text": TEXT_ANSWER}))
    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", cfg)) == 0

    Path(cfg.prompt_file).write_text("Read it, and note the tyres.", encoding="utf-8")
    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", cfg)) == 1

    fake = FakeProvider({"text": TEXT_ANSWER})
    assert extract.run_pass(conn, "text", cfg, fake)["done"] == 1
    assert fake.seen[0].system == "Read it, and note the tyres."


def test_putting_the_prompt_back_makes_the_old_reading_current_again(conn, tmp_path):
    """The key is the prompt's text, not a counter - so an edit you undo costs
    nothing rather than a second read of everything."""
    cfg = Config().extract_text
    cfg.prompt_file = str(tmp_path / "text.md")
    Path(cfg.prompt_file).write_text("Read it.", encoding="utf-8")
    listing(conn)
    extract.run_pass(conn, "text", cfg, FakeProvider({"text": TEXT_ANSWER}))

    Path(cfg.prompt_file).write_text("Something else.", encoding="utf-8")
    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", cfg)) == 1
    Path(cfg.prompt_file).write_text("Read it.", encoding="utf-8")
    assert db.extraction_backlog(conn, "text", passes.prompt_version("text", cfg)) == 0


def test_a_missing_prompt_file_is_not_a_complaint(conn, tmp_path, caplog):
    """It is the ordinary state of a fresh install, not a fault."""
    cfg = Config().extract_text
    cfg.prompt_file = str(tmp_path / "never-written.md")
    with caplog.at_level("WARNING"):
        assert passes.load_prompt("text", cfg).startswith("You read German")
    assert caplog.text == ""


def test_score_one_show_prompt_includes_the_preferences(conn, tmp_path, capsys):
    """Pass 3's system prompt carries the whole preferences file. Leaving it out
    of --show-prompt hid the half of the prompt people actually edit."""
    from karpm import cli, scoring
    listing(conn)
    conf = Config()
    scorer = scoring.Scorer(conf.scoring, "## What it needs\n\n- Under 60,000 km",
                            client=FakeProvider())

    text = cli._prompt_text(scorer, conn, db.get_listing(conn, "111"))
    assert "- Under 60,000 km" in text
    assert "*** SYSTEM PROMPT - SENT" in text
