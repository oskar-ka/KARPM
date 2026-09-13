"""SQLite access layer. Plain sqlite3 - the schema is small and the Pi is slow."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2


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
    ],
}


def init_db(conn: sqlite3.Connection) -> None:
    schema = resources.files("karpm").joinpath("schema.sql").read_text(encoding="utf-8")
    # Migrate first: schema.sql recreates the listing_current view, which can
    # only reference columns that already exist. On a fresh database every
    # PRAGMA below returns nothing and this is a no-op.
    _migrate(conn)
    conn.executescript(schema)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
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
    "condition damaged full_service_hist seller_type seller_name seller_id location "
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
    if isinstance(data.get("parse_warnings"), (list, tuple)):
        data["parse_warnings"] = json.dumps(list(data["parse_warnings"]), ensure_ascii=False)

    existing = get_listing(conn, data["id"])
    values = {k: data.get(k) for k in LISTING_COLUMNS}

    if existing is None:
        cols = LISTING_COLUMNS + ["first_seen_at", "last_seen_at", "is_active"]
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO listings ({', '.join(cols)}) VALUES ({placeholders})",
            [values[c] for c in LISTING_COLUMNS] + [now, now, 1],
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
    conn.execute(
        f"UPDATE listings SET {assignments}, last_seen_at = ?, is_active = 1, "
        "delisted_at = NULL WHERE id = ?",
        [merged[c] for c in LISTING_COLUMNS if c != "id"] + [now, data["id"]],
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


def pending_images(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM images WHERE local_path IS NULL ORDER BY listing_id, position LIMIT ?",
        (limit,),
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


# --- scores -----------------------------------------------------------------

def unscored_listings(
    conn: sqlite3.Connection, rescore_on_change: bool, prompt_version: str, limit: int
) -> list[sqlite3.Row]:
    """Listings with no score, or whose content changed since the last score."""
    clause = "s.id IS NULL" if not rescore_on_change else (
        "s.id IS NULL OR s.content_hash IS NOT l.content_hash OR s.prompt_version IS NOT ?"
    )
    params: list[Any] = [] if not rescore_on_change else [prompt_version]
    return conn.execute(
        f"""
        SELECT l.* FROM listings l
        LEFT JOIN scores s ON s.id = (
            SELECT id FROM scores WHERE listing_id = l.id ORDER BY scored_at DESC LIMIT 1
        )
        WHERE l.is_active = 1 AND ({clause})
        ORDER BY l.first_seen_at DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()


def add_score(conn: sqlite3.Connection, listing_id: str, score: dict) -> int:
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
