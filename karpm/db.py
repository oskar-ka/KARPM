"""SQLite access layer. Plain sqlite3 - the schema is small and the Pi is slow."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCHEMA_VERSION = 8

# Bump when a parser change means a stored row would now come out differently
# and cannot be corrected in place. Every listing written by an older version is
# then marked for re-fetching. See "Keeping stored rows honest" in CLAUDE.md.
PARSER_VERSION = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(title: str | None, description: str | None, price: int | None) -> str:
    blob = f"{title or ''}\x00{description or ''}\x00{price if price is not None else ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # survives power loss better
    conn.execute("PRAGMA synchronous=NORMAL")     # easier on an SD card
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after v1. CREATE TABLE IF NOT EXISTS will not add a column to a
# table that already exists, so existing databases need an explicit ALTER.
MIGRATIONS = {
    "listings": [
        ("delisted_reason", "TEXT"),
        ("missing_since", "TEXT"),
        ("missing_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_verified_at", "TEXT"),
        ("parser_version", "INTEGER NOT NULL DEFAULT 0"),
        ("needs_refetch", "INTEGER NOT NULL DEFAULT 0"),
        ("needs_rescore", "INTEGER NOT NULL DEFAULT 0"),
        ("ignored", "INTEGER NOT NULL DEFAULT 0"),
        ("color", "TEXT"),
        ("fuel_type", "TEXT"),
        ("drive_type", "TEXT"),
        ("transmission", "TEXT"),
        ("equipment_json", "TEXT"),
    ],
}


def init_db(conn: sqlite3.Connection) -> None:
    schema = resources.files("karpm").joinpath("schema.sql").read_text(encoding="utf-8")
    was = conn.execute("PRAGMA user_version").fetchone()[0]
    # Migrate first: schema.sql recreates the listing_current view, which can
    # only reference columns that already exist. On a fresh database every
    # PRAGMA below returns nothing and this is a no-op.
    _migrate(conn)
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()

    if 0 < was < 4:
        repaired = repair_descriptions(conn)
        if repaired:
            log.warning(
                "cleaned the stored markup out of %s description(s). Their scores "
                "were made from the old text, so they will be scored again on the "
                "next scoring run - which costs API credits.", repaired)

    if 0 < was < 7:
        _drop_columns(conn, "listings", ("plate", "plate_season"))

    if was:
        # A database that predates the flags has rows at parser_version 0. They
        # were parsed by whatever the code was then, which is exactly what this
        # is for - but marking every row on a first upgrade would re-fetch the
        # lot, so an existing database starts level with the current version.
        if was < 5:
            conn.execute("UPDATE listings SET parser_version = ?", (PARSER_VERSION,))
            conn.commit()
        outdated = mark_outdated_parses(conn)
        if outdated:
            log.warning("%s listing(s) were parsed by an older version of the "
                        "scraper and will be read again on the next scrape.", outdated)


def repair_descriptions(conn: sqlite3.Connection) -> int:
    """Rewrite descriptions that were stored as HTML rather than as text.

    Before schema v4 the description was taken straight from the JSON-LD block
    or the Astro payload, both of which carry it as markup, so rows scraped
    from the newer ad pages hold `<br />` and `&#x2F;` instead of line breaks
    and slashes. The text went into the scoring prompt that way too.

    content_hash is recomputed to match, which is what makes the next scoring
    run redo them. Leaving it stale would only postpone that: the next refresh
    of the ad would compute a hash from the clean text, record an "edited"
    event that never happened, and re-score anyway.
    """
    from .parse.fields import html_to_text     # local: parse does not import db

    rows = conn.execute(
        "SELECT id, title, description, price_eur FROM listings "
        "WHERE description LIKE '%<%' OR description LIKE '%&%'"
    ).fetchall()
    fixed = 0
    for row in rows:
        cleaned = html_to_text(row["description"])
        if cleaned == row["description"]:
            continue                    # a bare < or & in ordinary prose
        conn.execute(
            "UPDATE listings SET description = ?, content_hash = ? WHERE id = ?",
            (cleaned, content_hash(row["title"], cleaned, row["price_eur"]), row["id"]),
        )
        fixed += 1
    conn.commit()
    return fixed


# --- staleness flags --------------------------------------------------------
#
# Three things can make a stored row wrong in a way the ad itself never reveals:
# the parser changed, preferences.md changed, or you decided a listing is not
# for you. None of those show up as a price drop or an edit, so each is recorded
# against the listing rather than left to be noticed.


def mark_outdated_parses(conn: sqlite3.Connection) -> int:
    """Flag listings written by an older parser. Returns how many."""
    cur = conn.execute(
        "UPDATE listings SET needs_refetch = 1, needs_rescore = 1 "
        "WHERE is_active = 1 AND parser_version < ? AND needs_refetch = 0",
        (PARSER_VERSION,),
    )
    conn.commit()
    return cur.rowcount


def mark_for_refetch(conn: sqlite3.Connection, listing_id: str) -> None:
    """Read this ad's page again on the next scrape, whatever its age."""
    conn.execute(
        "UPDATE listings SET needs_refetch = 1, needs_rescore = 1 WHERE id = ?",
        (listing_id,))
    conn.commit()


def mark_for_rescore(conn: sqlite3.Connection, listing_id: str | None = None) -> int:
    """Score this listing again - or every active listing, if none is named."""
    if listing_id is None:
        cur = conn.execute("UPDATE listings SET needs_rescore = 1 WHERE is_active = 1")
    else:
        cur = conn.execute("UPDATE listings SET needs_rescore = 1 WHERE id = ?",
                           (listing_id,))
    conn.commit()
    return cur.rowcount


def note_preferences(conn: sqlite3.Connection, text: str | None) -> int:
    """Notice that preferences.md changed, and mark everything for re-scoring.

    Nothing about a listing changes when you rewrite what you are looking for,
    so without this the old verdicts would stand indefinitely - or wait for
    someone to remember to bump scoring.prompt_version by hand.

    Returns how many listings were marked; 0 when the file is unchanged.
    """
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
    if get_state(conn, "preferences_hash") == digest:
        return 0
    first_time = get_state(conn, "preferences_hash") is None
    set_state(conn, "preferences_hash", digest)
    if first_time:
        # Recording the file for the first time is not a change to it.
        return 0
    return mark_for_rescore(conn)


def set_ignored(conn: sqlite3.Connection, listing_id: str, ignored: bool = True) -> None:
    """Dismiss a listing, or take it back. Ignored listings are never mailed."""
    conn.execute("UPDATE listings SET ignored = ? WHERE id = ?",
                 (1 if ignored else 0, listing_id))
    conn.commit()


# Everything that is collected rather than configured. `searches` is in here
# because its rows are rebuilt from config.toml on the next open; the config
# file itself is never touched by any of this.
COLLECTED_TABLES = ("listing_history", "images", "scores", "extractions",
                    "notifications", "listings", "runs", "commands", "searches")

# Survives a reset: it is a control you set, not something that was collected.
# Silently un-pausing would let a scrape start that you had deliberately stopped.
KEEP_STATE = ("paused",)


def reset_everything(conn: sqlite3.Connection, images_dir: str | Path | None = None) -> dict:
    """Empty the database, and the image directory with it.

    Back to a fresh install: no listings, no history, no scores, no run log.
    The config file and preferences.md are not touched, and the searches in them
    are registered again on the next open.

    Returns what was removed, because a destructive action that reports nothing
    leaves you wondering whether it happened.
    """
    # A command still waiting is a click of yours that is about to disappear.
    # It gets cleared with everything else - it would run against an empty
    # database otherwise - but it is reported rather than silently dropped.
    counts = {"pending_commands": conn.execute(
        "SELECT COUNT(*) n FROM commands WHERE status = 'pending'").fetchone()["n"]}
    for table in COLLECTED_TABLES:
        counts[table] = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        conn.execute(f"DELETE FROM {table}")
    kept = "', '".join(KEEP_STATE)
    conn.execute(f"DELETE FROM app_state WHERE key NOT IN ('{kept}')")
    conn.commit()
    conn.execute("VACUUM")              # give the space back; this is a fresh start

    counts["images_deleted"] = _empty_image_dir(images_dir)
    return counts


def _empty_image_dir(images_dir: str | Path | None) -> int:
    """Remove downloaded photos. Without this a reset leaves orphans on disk
    that nothing references and every one of which would be fetched again."""
    if not images_dir:
        return 0
    root = Path(images_dir)
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_dir():
                removed += sum(1 for f in child.rglob("*") if f.is_file())
                shutil.rmtree(child)
            else:
                child.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not remove %s: %s", child, exc)
    return removed


def pending_counts(conn: sqlite3.Connection) -> dict:
    """How much housekeeping is outstanding, for the dashboard."""
    row = conn.execute(
        "SELECT SUM(needs_refetch) refetch, SUM(needs_rescore) rescore, "
        "SUM(ignored) ignored FROM listings WHERE is_active = 1").fetchone()
    return {"refetch": row["refetch"] or 0, "rescore": row["rescore"] or 0,
            "ignored": row["ignored"] or 0}


def _drop_columns(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> None:
    """Tidy away columns nothing writes any more.

    Best effort: DROP COLUMN needs SQLite 3.35, and a column left in place holds
    NULL and is read by nothing, so it is not worth rebuilding a table over.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column in existing & set(columns):
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except sqlite3.OperationalError as exc:
            log.debug("left %s.%s in place: %s", table, column, exc)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue        # fresh database - schema.sql creates it complete
        for name, spec in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def sync_searches(conn: sqlite3.Connection, searches: Iterable[Any]) -> None:
    for s in searches:
        conn.execute(
            "INSERT INTO searches (name, url, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET url=excluded.url, enabled=excluded.enabled",
            (s.name, s.url, int(s.enabled)),
        )
    conn.commit()


# --- listings ---------------------------------------------------------------

LISTING_COLUMNS = (
    "id url search_name title description price_eur price_kind make model bike_type "
    "model_year first_reg_date first_reg_year km hp ccm owners inspection_until "
    "condition damaged full_service_hist color fuel_type drive_type transmission "
    "equipment_json seller_type seller_name seller_id location "
    "postcode posted_at view_count attributes_json parse_warnings content_hash"
).split()


def get_listing(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def upsert_listing(conn: sqlite3.Connection, data: dict) -> str:
    """Insert or update a listing, recording what changed in listing_history.

    Returns one of: "new", "price_change", "edited", "relisted", "unchanged".
    """
    now = utcnow()
    data = dict(data)
    data["content_hash"] = content_hash(
        data.get("title"), data.get("description"), data.get("price_eur")
    )
    if isinstance(data.get("attributes_json"), (dict, list)):
        data["attributes_json"] = json.dumps(data["attributes_json"], ensure_ascii=False)
    if isinstance(data.get("equipment_json"), (list, tuple)):
        data["equipment_json"] = json.dumps(list(data["equipment_json"]), ensure_ascii=False)
    if isinstance(data.get("parse_warnings"), (list, tuple)):
        data["parse_warnings"] = json.dumps(list(data["parse_warnings"]), ensure_ascii=False)

    existing = get_listing(conn, data["id"])
    values = {k: data.get(k) for k in LISTING_COLUMNS}

    if existing is None:
        cols = LISTING_COLUMNS + ["first_seen_at", "last_seen_at", "is_active",
                                  "parser_version"]
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO listings ({', '.join(cols)}) VALUES ({placeholders})",
            [values[c] for c in LISTING_COLUMNS] + [now, now, 1, PARSER_VERSION],
        )
        add_history(conn, data["id"], "created", price_eur=values["price_eur"])
        return "new"

    # Never overwrite a good value with a None we failed to parse this time.
    merged = {c: (values[c] if values[c] is not None else existing[c]) for c in LISTING_COLUMNS}
    merged["id"] = data["id"]

    outcome = "unchanged"
    old_price, new_price = existing["price_eur"], merged["price_eur"]
    if old_price != new_price:
        add_history(conn, data["id"], "price_change", price_eur=new_price, prev_price_eur=old_price)
        outcome = "price_change"
    elif merged["content_hash"] != existing["content_hash"]:
        add_history(conn, data["id"], "edited", price_eur=new_price)
        outcome = "edited"

    if not existing["is_active"]:
        add_history(conn, data["id"], "relisted", price_eur=new_price)
        outcome = "relisted"

    assignments = ", ".join(f"{c} = ?" for c in LISTING_COLUMNS if c != "id")
    # The page has just been read with the current parser, so whatever was
    # outdated about this row no longer is.
    conn.execute(
        f"UPDATE listings SET {assignments}, last_seen_at = ?, is_active = 1, "
        "delisted_at = NULL, parser_version = ?, needs_refetch = 0 WHERE id = ?",
        [merged[c] for c in LISTING_COLUMNS if c != "id"]
        + [now, PARSER_VERSION, data["id"]],
    )
    return outcome


def touch_listing(conn: sqlite3.Connection, listing_id: str) -> None:
    """Mark a known listing as still present without re-fetching its detail page."""
    conn.execute(
        "UPDATE listings SET last_seen_at = ?, is_active = 1, delisted_at = NULL WHERE id = ?",
        (utcnow(), listing_id),
    )


def add_history(
    conn: sqlite3.Connection,
    listing_id: str,
    event: str,
    price_eur: int | None = None,
    prev_price_eur: int | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO listing_history (listing_id, observed_at, event, price_eur, "
        "prev_price_eur, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            listing_id,
            utcnow(),
            event,
            price_eur,
            prev_price_eur,
            json.dumps(detail, ensure_ascii=False) if detail else None,
        ),
    )


def missing_listings(conn: sqlite3.Connection, seen_ids: set[str],
                     search_name: str) -> list[sqlite3.Row]:
    """Active listings of this search that were not in the results this run."""
    rows = conn.execute(
        "SELECT * FROM listings WHERE is_active = 1 AND search_name = ?", (search_name,)
    ).fetchall()
    return [r for r in rows if r["id"] not in seen_ids]


def record_missing(conn: sqlite3.Connection, listing_id: str) -> None:
    """Note that a listing was absent from the search results this run."""
    conn.execute(
        "UPDATE listings SET missing_count = missing_count + 1, "
        "missing_since = COALESCE(missing_since, ?) WHERE id = ?",
        (utcnow(), listing_id),
    )


def clear_missing(conn: sqlite3.Connection, listing_id: str, verified: bool = False) -> None:
    """The listing is present again - either in the results or on its own page."""
    now = utcnow()
    conn.execute(
        "UPDATE listings SET missing_count = 0, missing_since = NULL, last_seen_at = ?, "
        "last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END WHERE id = ?",
        (now, 1 if verified else 0, now, listing_id),
    )


def record_verification(conn: sqlite3.Connection, listing_id: str) -> None:
    conn.execute(
        "UPDATE listings SET last_verified_at = ? WHERE id = ?", (utcnow(), listing_id)
    )


def mark_delisted(conn: sqlite3.Connection, listing_id: str,
                  reason: str = "verified_gone") -> None:
    """Record that a listing is genuinely gone. Callers must have evidence."""
    now = utcnow()
    conn.execute(
        "UPDATE listings SET is_active = 0, delisted_at = ?, delisted_reason = ?, "
        "last_verified_at = ? WHERE id = ?",
        (now, reason, now, listing_id),
    )
    add_history(conn, listing_id, "delisted", detail={"reason": reason})


# --- images -----------------------------------------------------------------

def add_image(conn: sqlite3.Connection, listing_id: str, position: int, url: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO images (listing_id, position, url) VALUES (?, ?, ?)",
        (listing_id, position, url),
    )


def pending_images(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Images with no local file yet, each carrying its ad's URL.

    The ad URL rides along so a failed download can name the listing it belongs
    to instead of just an opaque CDN path.
    """
    return conn.execute(
        "SELECT i.*, l.url AS listing_url, l.title AS listing_title "
        "FROM images i LEFT JOIN listings l ON l.id = i.listing_id "
        "WHERE i.local_path IS NULL ORDER BY i.listing_id, i.position LIMIT ?",
        (-1 if limit is None else limit,),      # SQLite reads -1 as no limit
    ).fetchall()


def record_image_download(
    conn: sqlite3.Connection, image_id: int, local_path: str, sha: str, size: int
) -> None:
    conn.execute(
        "UPDATE images SET local_path = ?, sha256 = ?, bytes = ?, downloaded_at = ? WHERE id = ?",
        (local_path, sha, size, utcnow(), image_id),
    )


def listing_images(conn: sqlite3.Connection, listing_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM images WHERE listing_id = ? ORDER BY position", (listing_id,)
    ).fetchall()


def average_images_per_listing(conn: sqlite3.Connection, min_listings: int = 5) -> float | None:
    """Mean photos stored per listing, or None if there is not enough to say.

    Used to estimate how long a run will take. Measuring beats guessing: the
    three real ads on hand had 8, 13 and 35 photos, which is an anecdote rather
    than an average. Note this counts what was *stored*, so it already reflects
    images.max_per_listing - which is the number a duration estimate wants.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT listing_id) AS listings, COUNT(*) AS images FROM images"
    ).fetchone()
    if not row or (row["listings"] or 0) < min_listings:
        return None
    return row["images"] / row["listings"]


# --- scores -----------------------------------------------------------------

def unscored_listings(
    conn: sqlite3.Connection, rescore_on_change: bool, prompt_version: str, limit: int
) -> list[sqlite3.Row]:
    """Listings with no score, or whose content changed since the last score."""
    clause = "s.id IS NULL OR l.needs_rescore = 1" if not rescore_on_change else (
        "s.id IS NULL OR l.needs_rescore = 1 "
        "OR s.content_hash IS NOT l.content_hash OR s.prompt_version IS NOT ?"
    )
    params: list[Any] = [] if not rescore_on_change else [prompt_version]
    # needs_refetch means the stored text is known to be out of date, so scoring
    # it now would buy a verdict on text we are about to replace.
    return conn.execute(
        f"""
        SELECT l.* FROM listings l
        LEFT JOIN scores s ON s.id = (
            SELECT id FROM scores WHERE listing_id = l.id ORDER BY scored_at DESC LIMIT 1
        )
        WHERE l.is_active = 1 AND l.needs_refetch = 0 AND ({clause})
        ORDER BY l.first_seen_at DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()


# --- what the extraction passes found ---------------------------------------


def _hash_image_set(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def image_set_hash(conn: sqlite3.Connection, listing_id: str) -> str | None:
    """A key for "the photos as they now stand", or None if there are none.

    Built from what was actually downloaded, so an ad whose photos failed to
    fetch does not look like one whose photos were read.
    """
    rows = conn.execute(
        "SELECT position, url FROM images WHERE listing_id = ? AND local_path IS NOT NULL "
        "ORDER BY position", (listing_id,)).fetchall()
    if not rows:
        return None
    return _hash_image_set([f"{r['position']}:{r['url']}" for r in rows])


def image_set_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    """The same key for every listing at once.

    Deciding which listings the photo pass owes work to means one of these per
    listing; asked one at a time that is a query each, and the question gets
    asked on a timer.
    """
    parts: dict[str, list[str]] = {}
    for row in conn.execute(
            "SELECT listing_id, position, url FROM images WHERE local_path IS NOT NULL "
            "ORDER BY listing_id, position"):
        parts.setdefault(row["listing_id"], []).append(f"{row['position']}:{row['url']}")
    return {listing_id: _hash_image_set(these) for listing_id, these in parts.items()}


def save_extraction(conn: sqlite3.Connection, listing_id: str, kind: str, data: dict,
                    *, provider: str, model: str, prompt_version: str,
                    source_hash: str | None, input_tokens: int = 0,
                    output_tokens: int = 0) -> None:
    """Record what a pass found, replacing whatever it found last time."""
    conn.execute(
        """
        INSERT INTO extractions (listing_id, kind, created_at, provider, model,
            prompt_version, source_hash, data_json, input_tokens, output_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_id, kind) DO UPDATE SET
            created_at=excluded.created_at, provider=excluded.provider,
            model=excluded.model, prompt_version=excluded.prompt_version,
            source_hash=excluded.source_hash, data_json=excluded.data_json,
            input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens
        """,
        (listing_id, kind, utcnow(), provider, model, prompt_version, source_hash,
         json.dumps(data, ensure_ascii=False), input_tokens, output_tokens),
    )
    conn.commit()


def get_extraction(conn: sqlite3.Connection, listing_id: str, kind: str) -> dict | None:
    """What a pass found, or None. The row carries what it was computed from."""
    row = conn.execute(
        "SELECT * FROM extractions WHERE listing_id = ? AND kind = ?",
        (listing_id, kind)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["data_json"])
    except ValueError:
        return None
    return {**dict(row), "data": data}


def extractions_for(conn: sqlite3.Connection, listing_id: str) -> dict:
    """Every pass's findings for one listing, keyed by kind."""
    out = {}
    for kind in ("text", "photos"):
        found = get_extraction(conn, listing_id, kind)
        if found:
            out[kind] = found
    return out


def listings_needing_extraction(conn: sqlite3.Connection, kind: str,
                                prompt_version: str, limit: int) -> list[sqlite3.Row]:
    """Active listings whose extraction is missing, stale, or from an older prompt.

    Stale means different things per pass, which is the point of having them
    separate: the text pass goes stale when the seller edits the ad, the photo
    pass when the photos change. One can be redone without the other.

    A listing waiting to be re-fetched is left alone either way - its stored text
    is known to be out of date, so reading it now buys an answer about text we
    are about to replace.
    """
    due = []
    for row, current in _extraction_candidates(conn, kind):
        if (row["extraction_id"] is None
                or row["done_version"] != prompt_version
                or row["done_hash"] != current):
            due.append(row)
        if len(due) >= limit:
            break
    return due


def _extraction_candidates(conn: sqlite3.Connection, kind: str):
    """Every listing the pass could look at, with the key it would be read at."""
    rows = conn.execute(
        """
        SELECT l.*, e.id AS extraction_id, e.prompt_version AS done_version,
               e.source_hash AS done_hash
        FROM listings l
        LEFT JOIN extractions e ON e.listing_id = l.id AND e.kind = ?
        WHERE l.is_active = 1 AND l.needs_refetch = 0
        ORDER BY l.first_seen_at DESC
        """,
        (kind,),
    ).fetchall()
    photos = image_set_hashes(conn) if kind == "photos" else {}
    for row in rows:
        current = photos.get(row["id"]) if kind == "photos" else row["content_hash"]
        if kind == "photos" and current is None:
            continue                    # nothing downloaded, so nothing to look at
        yield row, current


def extraction_backlog(conn: sqlite3.Connection, kind: str, prompt_version: str) -> int:
    """How many listings that pass still owes a reading, for the status line."""
    return sum(1 for row, current in _extraction_candidates(conn, kind)
               if row["extraction_id"] is None
               or row["done_version"] != prompt_version
               or row["done_hash"] != current)


def _source_hash(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str | None:
    """What this pass's answer was computed from."""
    return row["content_hash"] if kind == "text" else image_set_hash(conn, row["id"])


def add_score(conn: sqlite3.Connection, listing_id: str, score: dict) -> int:
    conn.execute("UPDATE listings SET needs_rescore = 0 WHERE id = ?", (listing_id,))
    cur = conn.execute(
        """
        INSERT INTO scores (listing_id, scored_at, model, prompt_version, content_hash,
            overall, fit, value, fair_price_eur, headline, reasoning,
            pros_json, cons_json, red_flags_json, input_tokens, output_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing_id,
            utcnow(),
            score["model"],
            score["prompt_version"],
            score.get("content_hash"),
            score["overall"],
            score.get("fit"),
            score.get("value"),
            score.get("fair_price_eur"),
            score.get("headline"),
            score.get("reasoning"),
            json.dumps(score.get("pros", []), ensure_ascii=False),
            json.dumps(score.get("cons", []), ensure_ascii=False),
            json.dumps(score.get("red_flags", []), ensure_ascii=False),
            score.get("input_tokens"),
            score.get("output_tokens"),
        ),
    )
    return int(cur.lastrowid)


# --- notifications ----------------------------------------------------------

def already_notified(conn: sqlite3.Connection, listing_id: str, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE listing_id = ? AND kind = ?", (listing_id, kind)
    ).fetchone()
    return row is not None


def record_notification(
    conn: sqlite3.Connection, listing_id: str, kind: str, provider_id: str | None = None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO notifications (listing_id, kind, sent_at, provider_id) "
        "VALUES (?, ?, ?, ?)",
        (listing_id, kind, utcnow(), provider_id),
    )


# --- runs -------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, kind) VALUES (?, ?)", (utcnow(), kind)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, ok: bool, **counts) -> None:
    fields = ", ".join(f"{k} = ?" for k in counts)
    sql = "UPDATE runs SET finished_at = ?, ok = ?"
    params: list[Any] = [utcnow(), int(ok)]
    if fields:
        sql += ", " + fields
        params += list(counts.values())
    sql += " WHERE id = ?"
    params.append(run_id)
    conn.execute(sql, params)
    conn.commit()


# --- daemon control ---------------------------------------------------------

COMMANDS = ("scrape", "digest", "extract", "rescore")


def queue_command(conn: sqlite3.Connection, command: str, params: dict | None = None) -> int:
    """Ask the daemon to do something. It picks this up on its next poll."""
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    cur = conn.execute(
        "INSERT INTO commands (command, params_json, requested_at) VALUES (?, ?, ?)",
        (command, json.dumps(params or {}, ensure_ascii=False), utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def has_pending_command(conn: sqlite3.Connection) -> bool:
    """Is anything waiting? Asked once a second by the daemon's sleep, so it is
    a one-row lookup on an indexed-enough table rather than a full read."""
    return conn.execute(
        "SELECT 1 FROM commands WHERE status = 'pending' LIMIT 1").fetchone() is not None


def claim_command(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest pending command and mark it running.

    The update is conditional on the row still being pending, so a second
    claimer gets nothing rather than the same work.
    """
    row = conn.execute(
        "SELECT id FROM commands WHERE status = 'pending' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE commands SET status = 'running', started_at = ? "
        "WHERE id = ? AND status = 'pending'", (utcnow(), row["id"]))
    conn.commit()
    if cur.rowcount == 0:
        return None
    # Re-read, so the caller gets the row as it now stands rather than a
    # snapshot that still says "pending".
    return conn.execute("SELECT * FROM commands WHERE id = ?", (row["id"],)).fetchone()


def finish_command(conn: sqlite3.Connection, command_id: int, ok: bool, result: str = "") -> None:
    conn.execute(
        "UPDATE commands SET status = ?, finished_at = ?, result = ? WHERE id = ?",
        ("done" if ok else "failed", utcnow(), result[:2000], command_id),
    )
    conn.commit()


def recent_commands(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def reset_stale_commands(conn: sqlite3.Connection) -> int:
    """A command left "running" means the daemon died mid-command."""
    cur = conn.execute(
        "UPDATE commands SET status = 'failed', finished_at = ?, "
        "result = 'the daemon stopped while this was running' "
        "WHERE status = 'running'", (utcnow(),))
    conn.commit()
    return cur.rowcount


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value, set_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
        (key, value, utcnow()),
    )
    conn.commit()


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_state(conn, "paused", "0") == "1"


# --- market comparisons -----------------------------------------------------

def comparable_stats(conn: sqlite3.Connection, listing: sqlite3.Row) -> dict | None:
    """Price context from listings of the same model, for the scoring prompt.

    Without this the model is guessing at market value from training data alone;
    with it, "good value" is measured against what you are actually seeing.
    """
    if not listing["model"]:
        return None
    rows = conn.execute(
        """
        SELECT price_eur, km, first_reg_year FROM listings
        WHERE model = ? AND price_eur IS NOT NULL AND price_eur > 0 AND id != ?
        ORDER BY first_seen_at DESC LIMIT 200
        """,
        (listing["model"], listing["id"]),
    ).fetchall()
    prices = sorted(r["price_eur"] for r in rows)
    if len(prices) < 5:
        return None

    def pct(p: float) -> int:
        return prices[min(len(prices) - 1, int(len(prices) * p))]

    kms = sorted(r["km"] for r in rows if r["km"] is not None)
    return {
        "sample_size": len(prices),
        "price_p25": pct(0.25),
        "price_median": pct(0.50),
        "price_p75": pct(0.75),
        "km_median": kms[len(kms) // 2] if kms else None,
    }
