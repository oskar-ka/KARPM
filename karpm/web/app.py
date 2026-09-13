"""Flask application factory and routes."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

try:                                    # tomllib is stdlib from Python 3.11
    import tomllib
except ModuleNotFoundError:             # pragma: no cover - 3.10 (Raspberry Pi OS)
    import tomli as tomllib

from .. import db
from ..config import Config, load_config

log = logging.getLogger(__name__)

HEARTBEAT_STALE_AFTER = timedelta(minutes=5)


def create_app(config_path: str = "config.toml") -> Flask:
    app = Flask(__name__)
    app.config["KARPM_CONFIG_PATH"] = config_path
    # Only used for flash messages; this app has no login and no user data.
    app.secret_key = "karpm-local"

    @app.template_filter("fromjson")
    def _fromjson(value):
        try:
            return json.loads(value or "[]")
        except (TypeError, ValueError):
            return []

    def conf() -> Config:
        return load_config(app.config["KARPM_CONFIG_PATH"])

    def connect():
        return db.connect(conf().db_path)

    # Every CLI command applies the schema when it opens the database; this
    # process has to as well. Without it, a database written before a schema
    # change is missing the tables the UI reads and every page is a 500.
    try:
        startup = connect()
        try:
            db.init_db(startup)
            db.sync_searches(startup, conf().searches)
        finally:
            startup.close()
    except Exception as exc:
        # Keep serving: /config reads the file directly, so the UI is still the
        # place to fix a config that is the reason this failed.
        log.error("could not open the database at startup: %s", exc)

    # --- status ---------------------------------------------------------

    @app.route("/")
    def dashboard():
        conn = connect()
        try:
            return render_template("dashboard.html", **_status(conn, conf()))
        finally:
            conn.close()

    @app.route("/status-fragment")
    def status_fragment():
        """The page's poller asks for this; it re-renders the status panel alone."""
        conn = connect()
        try:
            return render_template("_status.html", **_status(conn, conf()))
        finally:
            conn.close()

    # --- listings -------------------------------------------------------

    @app.route("/listings")
    def listings():
        conn = connect()
        try:
            filters = _filters_from(request.args)
            rows = _query_listings(conn, filters)
            return render_template("listings.html", rows=rows, filters=filters,
                                   total=_count_listings(conn, filters),
                                   columns=LIST_COLUMNS)
        finally:
            conn.close()

    @app.route("/listing/<listing_id>")
    def listing(listing_id: str):
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM listing_current WHERE id = ?",
                               (listing_id,)).fetchone()
            if row is None:
                abort(404)
            images = db.listing_images(conn, listing_id)
            history = conn.execute(
                "SELECT * FROM listing_history WHERE listing_id = ? ORDER BY id DESC",
                (listing_id,)).fetchall()
            scores = conn.execute(
                "SELECT * FROM scores WHERE listing_id = ? ORDER BY scored_at DESC",
                (listing_id,)).fetchall()
            return render_template("listing.html", row=row, images=images,
                                   history=history, scores=scores,
                                   attributes=json.loads(row["attributes_json"] or "{}"))
        finally:
            conn.close()

    @app.route("/photo/<listing_id>/<int:position>")
    def photo(listing_id: str, position: int):
        conn = connect()
        try:
            row = conn.execute(
                "SELECT local_path FROM images WHERE listing_id = ? AND position = ?",
                (listing_id, position)).fetchone()
        finally:
            conn.close()
        if row is None or not row["local_path"]:
            abort(404)
        path = Path(row["local_path"]).resolve()
        # The path comes from our own database, but serving files by name is
        # worth being strict about regardless.
        root = Path(conf().images.dir).resolve()
        if root not in path.parents or not path.exists():
            abort(404)
        return send_file(path)

    # --- daemon control -------------------------------------------------

    @app.post("/control/<action>")
    def control(action: str):
        conn = connect()
        try:
            if action in db.COMMANDS:
                params = {"all": request.form.get("all") == "1"} if action == "rescore" else {}
                db.queue_command(conn, action, params)
                flash(f"{action} queued - the daemon picks it up within a minute", "ok")
            elif action in ("pause", "resume"):
                db.set_state(conn, "paused", "1" if action == "pause" else "0")
                flash("schedule paused" if action == "pause" else "schedule resumed", "ok")
            else:
                abort(404)
        finally:
            conn.close()
        return redirect(request.referrer or url_for("dashboard"))

    # --- searches -------------------------------------------------------

    @app.route("/searches")
    def searches():
        return render_template("searches.html", searches=conf().searches)

    @app.post("/searches/save")
    def save_searches():
        """Rewrite the [[searches]] tables from the submitted form."""
        entries = []
        for index in sorted({int(k.split("-")[1]) for k in request.form
                             if k.startswith("name-")}):
            name = (request.form.get(f"name-{index}") or "").strip()
            url = (request.form.get(f"url-{index}") or "").strip()
            if not name or not url:
                continue
            entry = {"name": name, "url": url,
                     "enabled": request.form.get(f"enabled-{index}") == "1"}
            for field in ("make", "model"):
                value = (request.form.get(f"{field}-{index}") or "").strip()
                if value:
                    entry[field] = value
            max_ads = (request.form.get(f"max_ads-{index}") or "").strip()
            if max_ads.isdigit():
                entry["max_ads"] = int(max_ads)
            entries.append(entry)

        try:
            _rewrite_searches(app.config["KARPM_CONFIG_PATH"], entries)
        except (OSError, ValueError) as exc:
            flash(f"could not save: {exc}", "error")
            return redirect(url_for("searches"))
        conn = connect()
        try:
            db.sync_searches(conn, conf().searches)
        finally:
            conn.close()
        flash(f"saved {len(entries)} search(es) - the daemon rereads the config "
              "on its next cycle", "ok")
        return redirect(url_for("searches"))

    # --- preferences and config -----------------------------------------

    @app.route("/preferences")
    def preferences():
        path = Path(conf().scoring.preferences_file)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return render_template("preferences.html", text=text, path=path)

    @app.post("/preferences")
    def save_preferences():
        path = Path(conf().scoring.preferences_file)
        _backup(path)
        path.write_text(request.form.get("text", ""), encoding="utf-8")
        flash("preferences saved - bump scoring.prompt_version to re-score "
              "everything against them", "ok")
        return redirect(url_for("preferences"))

    @app.route("/config")
    def config_page():
        path = Path(app.config["KARPM_CONFIG_PATH"])
        return render_template("config.html", text=path.read_text(encoding="utf-8"),
                               path=path)

    @app.post("/config")
    def save_config():
        path = Path(app.config["KARPM_CONFIG_PATH"])
        text = request.form.get("text", "")
        try:
            tomllib.loads(text)             # valid TOML...
        except tomllib.TOMLDecodeError as exc:
            flash(f"not valid TOML, nothing was written: {exc}", "error")
            return render_template("config.html", text=text, path=path)

        _backup(path)
        path.write_text(text, encoding="utf-8")
        try:
            load_config(path)               # ...and a config KARPM can use
        except Exception as exc:
            _restore(path)
            flash(f"config rejected and rolled back: {exc}", "error")
            return render_template("config.html", text=text, path=path)
        flash("config saved - the daemon rereads it on its next cycle", "ok")
        return redirect(url_for("config_page"))

    return app


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def _restore(path: Path) -> None:
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.exists():
        shutil.copy2(backup, path)


def _rewrite_searches(config_path, entries: list[dict]) -> None:
    """Replace the [[searches]] tables, leaving the rest of the file untouched.

    The file is edited as text rather than re-serialised, so comments and
    formatting elsewhere survive.
    """
    path = Path(config_path)
    original = path.read_text(encoding="utf-8")
    lines, kept, skipping = original.splitlines(), [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[[searches]]"):
            skipping = True
            continue
        if skipping and stripped.startswith("[") and not stripped.startswith("[["):
            skipping = False
        if not skipping:
            kept.append(line)

    rendered = []
    for entry in entries:
        rendered.append("[[searches]]")
        rendered.append(f'name = "{_toml_escape(entry["name"])}"')
        rendered.append(f'url = "{_toml_escape(entry["url"])}"')
        rendered.append(f"enabled = {str(entry['enabled']).lower()}")
        for field in ("make", "model"):
            if entry.get(field):
                rendered.append(f'{field} = "{_toml_escape(entry[field])}"')
        if entry.get("max_ads"):
            rendered.append(f"max_ads = {entry['max_ads']}")
        rendered.append("")

    body = "\n".join(kept).rstrip() + "\n\n" + "\n".join(rendered)
    tomllib.loads(body)                     # refuse to write something unreadable
    _backup(path)
    path.write_text(body, encoding="utf-8")


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


LIST_COLUMNS = [
    ("overall", "score"), ("price_eur", "price"), ("km", "km"),
    ("first_reg_year", "EZ"), ("hp", "PS"), ("inspection_until", "HU"),
    ("seller_type", "seller"), ("location", "location"),
    ("first_seen_at", "first seen"), ("title", "title"),
]

SORTABLE = {name for name, _ in LIST_COLUMNS}


def _status(conn, conf) -> dict:
    heartbeat = db.get_state(conn, "heartbeat")
    alive = False
    if heartbeat:
        try:
            last = datetime.fromisoformat(heartbeat)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            alive = datetime.now(timezone.utc) - last < HEARTBEAT_STALE_AFTER
        except ValueError:
            pass
    counts = conn.execute(
        "SELECT COUNT(*) total, SUM(is_active) active, "
        "SUM(CASE WHEN overall IS NULL THEN 1 ELSE 0 END) unscored "
        "FROM listing_current").fetchone()
    running = conn.execute(
        "SELECT * FROM commands WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "alive": alive,
        "running": running,
        "heartbeat": heartbeat,
        "paused": db.is_paused(conn),
        "next_scrape": db.get_state(conn, "next_scrape"),
        "next_digest": db.get_state(conn, "next_digest"),
        "counts": counts,
        "runs": conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8").fetchall(),
        "commands": db.recent_commands(conn, 8),
        "searches": conn.execute("SELECT * FROM searches ORDER BY name").fetchall(),
        "conf": conf,
    }


def _filters_from(args) -> dict:
    return {
        "q": (args.get("q") or "").strip(),
        "min_score": args.get("min_score", type=int),
        "max_price": args.get("max_price", type=int),
        "max_km": args.get("max_km", type=int),
        "state": args.get("state", "active"),
        "sort": args.get("sort") if args.get("sort") in SORTABLE else "first_seen_at",
        "dir": "asc" if args.get("dir") == "asc" else "desc",
        "page": max(args.get("page", type=int) or 1, 1),
    }


def _where(filters) -> tuple[str, list]:
    clauses, params = [], []
    if filters["state"] == "active":
        clauses.append("is_active = 1")
    elif filters["state"] == "delisted":
        clauses.append("is_active = 0")
    if filters["state"] == "unscored":
        clauses.append("overall IS NULL AND is_active = 1")
    if filters["q"]:
        clauses.append("(title LIKE ? OR description LIKE ? OR id = ?)")
        params += [f"%{filters['q']}%", f"%{filters['q']}%", filters["q"]]
    if filters["min_score"]:
        clauses.append("overall >= ?")
        params.append(filters["min_score"])
    if filters["max_price"]:
        clauses.append("price_eur IS NOT NULL AND price_eur <= ?")
        params.append(filters["max_price"])
    if filters["max_km"]:
        clauses.append("km IS NOT NULL AND km <= ?")
        params.append(filters["max_km"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


PAGE_SIZE = 100


def _query_listings(conn, filters) -> list:
    where, params = _where(filters)
    # Column and direction are checked against a fixed set before they get here.
    order = f"{filters['sort']} {filters['dir'].upper()}"
    return conn.execute(
        f"SELECT * FROM listing_current{where} ORDER BY {order} NULLS LAST "
        f"LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (filters["page"] - 1) * PAGE_SIZE],
    ).fetchall()


def _count_listings(conn, filters) -> int:
    where, params = _where(filters)
    return conn.execute(
        f"SELECT COUNT(*) n FROM listing_current{where}", params).fetchone()["n"]
