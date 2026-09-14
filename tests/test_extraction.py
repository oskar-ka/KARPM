"""The three passes: what they are asked, what gets stored, and when they rerun.

Nothing here talks to an API. A fake provider stands in for one, which is the
point of the seam - a pass that could only be tested against Anthropic would be
a pass nobody could put another model behind.
"""

from pathlib import Path

import pytest

from karpm import db, pipeline
from karpm.ai import extract, provider
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
    assert len(rows) == 1 and rows[0]["prompt_version"] == "v2"


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

    assert db.extraction_backlog(conn, "text", conf.extract_text.prompt_version) == 1
    assert db.extraction_backlog(conn, "photos", conf.extract_photos.prompt_version) == 0


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

    assert db.extraction_backlog(conn, "photos", conf.extract_photos.prompt_version) == 1
    assert db.extraction_backlog(conn, "text", conf.extract_text.prompt_version) == 0


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
    # And it says how many photos ride along, which the text alone cannot show.
    assert "photo(s) would be attached" in text


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
