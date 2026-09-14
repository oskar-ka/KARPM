"""Flask application factory and routes."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from . import fields, tomledit
from .. import db, derived
from ..parse import fields as parse_fields
from ..config import Config, load_config

log = logging.getLogger(__name__)

HEARTBEAT_STALE_AFTER = timedelta(minutes=5)


def create_app(config_path: str = "config.toml") -> Flask:
    app = Flask(__name__)
    app.config["KARPM_CONFIG_PATH"] = config_path
    # Only used for flash messages; this app has no login and no user data.
    app.secret_key = "karpm-local"

    # The template builds field names the same way the parser reads them.
    app.jinja_env.globals["input_name"] = fields.input_name

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
            return render_template(
                "listing.html", row=row, images=images, history=history,
                scores=scores, specifications=_specifications(row),
                figures=_derived_rows(
                    derived.summarise(conn, row, conf().home_plz), conf().home_plz),
                misc=_miscellaneous(row))
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

    @app.post("/listing/<listing_id>/<action>")
    def listing_action(listing_id: str, action: str):
        conn = connect()
        try:
            if conn.execute("SELECT 1 FROM listings WHERE id = ?",
                            (listing_id,)).fetchone() is None:
                abort(404)
            if action in ("ignore", "unignore"):
                db.set_ignored(conn, listing_id, action == "ignore")
                flash("ignored - it will not appear in any email" if action == "ignore"
                      else "no longer ignored", "ok")
            elif action == "refetch":
                db.mark_for_refetch(conn, listing_id)
                flash("marked - its page is read again on the next scrape", "ok")
            elif action == "rescore":
                db.mark_for_rescore(conn, listing_id)
                flash("marked - it is scored again on the next scoring run, "
                      "which costs credits", "ok")
            else:
                abort(404)
        finally:
            conn.close()
        return redirect(request.referrer or url_for("listing", listing_id=listing_id))

    # --- daemon control -------------------------------------------------

    @app.post("/control/<action>")
    def control(action: str):
        conn = connect()
        try:
            if action in db.COMMANDS:
                params = {"all": request.form.get("all") == "1"} if action == "rescore" else {}
                db.queue_command(conn, action, params)
                flash(f"{action} queued - the daemon picks it up within a minute", "ok")
            elif action == "reset":
                # A wipe while the daemon is mid-scrape would be undone by the
                # rows it is about to write, so refuse rather than half-work.
                busy = conn.execute(
                    "SELECT command FROM commands WHERE status = 'running' "
                    "LIMIT 1").fetchone()
                if busy:
                    flash(f"the daemon is running {busy['command']} - nothing was "
                          "deleted. Try again when it has finished.", "error")
                else:
                    removed = db.reset_everything(conn, conf().images.dir)
                    flash("database cleared: "
                          f"{removed['listings']} listing(s), {removed['scores']} score(s), "
                          f"{removed['images_deleted']} photo(s). Your config and "
                          "preferences are untouched.", "ok")
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
        return render_template("searches.html", fields=fields.SEARCH_FIELDS,
                               rows=[_search_row(s) for s in conf().searches],
                               errors={})

    @app.post("/searches/save")
    def save_searches():
        """Rewrite the [[searches]] tables from the submitted form."""
        rows, errors = _parse_search_form(request.form)
        if errors:
            flash(_needs_fixing(errors), "error")
            return render_template("searches.html", fields=fields.SEARCH_FIELDS,
                                   rows=rows, errors=errors)

        path = Path(app.config["KARPM_CONFIG_PATH"])
        entries = [{spec.key: row[spec.key] for spec in fields.SEARCH_FIELDS
                    if row[spec.key] not in (None, "")} for row in rows]
        try:
            _write_checked(path, tomledit.set_searches(
                path.read_text(encoding="utf-8"), entries))
        except (ValueError, OSError) as exc:
            flash(f"rejected, nothing was changed: {exc}", "error")
            return render_template("searches.html", fields=fields.SEARCH_FIELDS,
                                   rows=rows, errors={})
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
        text = request.form.get("text", "")
        _backup(path)
        path.write_text(text, encoding="utf-8")
        # Nothing about a listing changes when you rewrite what you want, so
        # the verdicts have to be marked here rather than noticed later.
        conn = connect()
        try:
            marked = db.note_preferences(conn, text)
        finally:
            conn.close()
        if marked:
            flash(f"preferences saved - {marked} listing(s) marked for re-scoring, "
                  "which happens on the next scoring run and costs credits", "ok")
        else:
            flash("preferences saved - unchanged, so nothing was marked", "ok")
        return redirect(url_for("preferences"))

    @app.route("/config")
    def config_page():
        path = Path(app.config["KARPM_CONFIG_PATH"])
        return render_template("config.html", path=path,
                               sections=fields.SECTIONS,
                               values=_current_values(conf()), errors={})

    @app.post("/config")
    def save_config():
        path = Path(app.config["KARPM_CONFIG_PATH"])
        parsed, errors, blanks = _parse_config_form(request.form)
        if errors:
            # Nothing is written; the page comes back with what was typed and
            # the problem beside the field it is in.
            flash(_needs_fixing(errors), "error")
            return render_template("config.html", path=path, sections=fields.SECTIONS,
                                   values=_submitted_values(request.form), errors=errors)

        updates, removals = _only_changed(parsed, blanks, conf())
        text = path.read_text(encoding="utf-8")
        text = tomledit.remove_keys(text, removals)
        text = tomledit.set_values(text, updates)
        try:
            _write_checked(path, text)
        except (ValueError, OSError) as exc:
            flash(f"rejected, nothing was changed: {exc}", "error")
            return render_template("config.html", path=path, sections=fields.SECTIONS,
                                   values=_submitted_values(request.form), errors={})
        flash("config saved - the daemon rereads it on its next cycle", "ok")
        return redirect(url_for("config_page"))

    return app


def _needs_fixing(errors: dict) -> str:
    """Name the fields, so nothing has to be hunted for down a long page."""
    names = [_readable(name) for name in errors]
    shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
    return f"nothing was saved - check {shown}"


def _readable(name: str) -> str:
    """"scrape__min_delay_s" -> "scrape.min_delay_s"; "url-0" -> "search 1: url"."""
    key, _, index = name.rpartition("-")
    if key and index.isdigit():
        return f"search {int(index) + 1}: {key}"
    return name.replace("__", ".")


def _write_checked(path: Path, text: str) -> None:
    """Write config.toml only if the result is a config KARPM can actually use.

    The candidate is loaded from a temporary file first, so a rejected save
    never touches the real one - there is nothing to roll back.
    """
    candidate = path.with_suffix(path.suffix + ".candidate")
    candidate.write_text(text, encoding="utf-8")
    try:
        load_config(candidate)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    finally:
        candidate.unlink(missing_ok=True)
    _backup(path)
    path.write_text(text, encoding="utf-8")


def _current_values(conf: Config) -> dict:
    """The value of every form field, as it is now, keyed "section.key"."""
    values = {}
    for section in fields.SECTIONS:
        holder = conf if section.name == "" else getattr(conf, section.name)
        for spec in section.fields:
            values[fields.input_name(section.name, spec.key)] = _as_form_text(
                spec, getattr(holder, spec.key, None))
    return values


def _submitted_values(form) -> dict:
    """What the user typed, so a rejected save comes back with their edits."""
    values = {}
    for section in fields.SECTIONS:
        for spec in section.fields:
            name = fields.input_name(section.name, spec.key)
            values[name] = (form.get(name) == "1" if spec.kind == "bool"
                            else form.get(name, ""))
    return values


def _as_form_text(spec, value):
    if spec.kind == "bool":
        return bool(value)
    if value is None:
        return ""
    if spec.kind == "lines":
        return "\n".join(str(v) for v in value)
    if spec.kind == "numbers":
        return ", ".join(_trim_float(v) for v in value)
    if spec.kind == "float":
        return _trim_float(value)
    return str(value)


def _trim_float(value) -> str:
    """4.0 reads better as "4" in a form field; 0.5 has to stay 0.5."""
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def _parse_config_form(form) -> tuple[dict, dict, dict]:
    """Read the form into TOML-ready values.

    Returns the values to write, the per-field errors, and the optional fields
    left blank - those have their key removed so the built-in default applies,
    which is not the same as writing an empty string.
    """
    parsed: dict[str, dict] = {}
    errors: dict[str, str] = {}
    blanks: dict[str, set] = {}

    for section in fields.SECTIONS:
        for spec in section.fields:
            name = fields.input_name(section.name, spec.key)
            if spec.kind == "bool":
                parsed.setdefault(section.name, {})[spec.key] = form.get(name) == "1"
                continue
            raw = (form.get(name) or "").strip()
            if not raw and spec.kind in ("lines", "numbers"):
                # An empty list is a value in its own right: no recipients, no
                # scheduled runs. Writing [] says that; removing the key would
                # silently restore the default instead.
                parsed.setdefault(section.name, {})[spec.key] = []
                continue
            if not raw:
                if spec.optional:
                    blanks.setdefault(section.name, set()).add(spec.key)
                else:
                    errors[name] = "this cannot be empty"
                continue
            try:
                parsed.setdefault(section.name, {})[spec.key] = _coerce(spec, raw)
            except ValueError as exc:
                errors[name] = str(exc)
    return parsed, errors, blanks


def _only_changed(parsed: dict, blanks: dict, conf: Config) -> tuple[dict, dict]:
    """Narrow a whole submitted form down to the values that actually differ.

    Without this, every save rewrites every line: a `4` typed into a float field
    comes back as `4.0`, and a setting left at its default gets written out
    explicitly the first time the form is submitted. Both are correct and both
    are noise in a file the owner reads.
    """
    updates: dict[str, dict] = {}
    removals: dict[str, set] = {}
    for section in fields.SECTIONS:
        holder = conf if section.name == "" else getattr(conf, section.name)
        for spec in section.fields:
            now = getattr(holder, spec.key, None)
            if spec.key in blanks.get(section.name, ()):
                if now is not None:
                    removals.setdefault(section.name, set()).add(spec.key)
                continue
            new = parsed.get(section.name, {}).get(spec.key)
            # 30 and 30.0 are the same setting; bool must not equal 1.
            same = (now == new and isinstance(now, bool) == isinstance(new, bool))
            if not same:
                updates.setdefault(section.name, {})[spec.key] = new
    return updates, removals


def _coerce(spec, raw: str):
    if spec.kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{raw!r} is not a whole number") from None
    if spec.kind == "float":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{raw!r} is not a number") from None
    if spec.kind == "numbers":
        out = []
        for part in (p.strip() for p in raw.replace("\n", ",").split(",")):
            if not part:
                continue
            try:
                out.append(float(part))
            except ValueError:
                raise ValueError(f"{part!r} is not a number") from None
        return out
    if spec.kind == "lines":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if spec.kind == "choice":
        if raw not in spec.choices:
            raise ValueError(f"must be one of {', '.join(spec.choices)}")
        return raw
    return raw


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


# Every listing shows the same rows in the same order, whether or not the ad
# filled them in. A field that silently disappears when it is empty makes two
# listings impossible to compare, and hides the fact that the ad never said.
#
# (label, column, format, what to say when it is not there)
SPECIFICATIONS = (
    ("price", "price_eur", "{:,} €", "unknown"),
    ("mileage", "km", "{:,} km", "unknown"),
    ("first registration", "first_reg_date", None, "unknown"),
    ("power", "hp", "{} PS", "unknown"),
    ("displacement", "ccm", "{} ccm", "unknown"),
    ("type", "bike_type", None, "unknown"),
    ("final drive", "drive_type", None, "unknown"),
    ("transmission", "transmission", None, "unknown"),
    ("previous owners", "owners", None, "unknown"),
    ("condition", "condition", None, "unknown"),
    ("HU until", "inspection_until", None, "unknown"),
    ("equipment", "equipment_json", None, "not listed"),
)

MISCELLANEOUS = (
    ("make", "make", None, "unknown"),
    ("model", "model", None, "unknown"),
    ("model year", "model_year", None, "unknown"),
    ("colour", "color", None, "unknown"),
    ("fuel", "fuel_type", None, "unknown"),
    ("service history", "full_service_hist", None, "unknown"),
    ("damaged", "damaged", None, "unknown"),
    ("seller", "seller_type", None, "unknown"),
    ("seller name", "seller_name", None, "unknown"),
    ("location", "location", None, "unknown"),
    ("posted", "posted_at", None, "unknown"),
    ("views", "view_count", None, "unknown"),
    ("first seen", "first_seen_at", None, "unknown"),
    ("last seen", "last_seen_at", None, "unknown"),
    ("search", "search_name", None, "unknown"),
)

YES_NO = {"full_service_hist", "damaged"}


def _format(row, column, template):
    value = row[column] if column in row.keys() else None
    if value is None or value == "":
        return None
    if column in YES_NO:
        return "yes" if value else "no"
    if template:
        return template.format(value).replace(",", ".")
    return value


def _unparsed(row) -> dict[str, str]:
    """Raw attributes whose column stayed empty, keyed by that column.

    An ad that says "HU: Neu" has said something about the HU. Reporting the
    column as "unknown" would be a second way of losing it - so the row shows
    what the ad actually said, and that it was not a date.
    """
    raw = json.loads(row["attributes_json"] or "{}")
    columns = set(row.keys())
    out = {}
    for label, value in raw.items():
        column = parse_fields.mapped_column(label)
        if column and column in columns and row[column] in (None, ""):
            out.setdefault(column, value)
    return out


def _rows(row, spec) -> list[tuple[str, str, bool]]:
    """(label, what to show, whether that is a value or an apology)."""
    unparsed = _unparsed(row)
    out = []
    for label, column, template, missing in spec:
        value = _format(row, column, template)
        if value is None and column in unparsed:
            out.append((label, unparsed[column], True))
            continue
        out.append((label, value if value is not None else missing, value is None))
    return out


def _specifications(row) -> list:
    rows = _rows(row, SPECIFICATIONS)
    if row["price_kind"] == "vb" and not rows[0][2]:
        label, text, absent = rows[0]
        rows[0] = (label, f"{text} VB", absent)      # Verhandlungsbasis: negotiable
    equipment = derived.equipment(row)
    if equipment:
        rows[-1] = ("equipment", ", ".join(equipment), False)
    return rows


def _derived_rows(worked_out: dict, home_plz: str | None) -> list:
    """The same figures for every listing, and why one is missing when it is.

    "cannot be worked out" rather than "unknown": the ad may well have said
    it, we just have not got what it takes to compute the number.
    """
    months = worked_out["hu_months_left"]
    drop = worked_out["price_drop"]
    days = worked_out["days_on_market"]
    km = worked_out["km_per_year"]
    distance = worked_out["distance_km"]

    return [
        ("km per year", f"{km:,}".replace(",", ".") if km is not None else "unknown",
         km is None),
        ("age", f"{worked_out['age_years']} years" if worked_out["age_years"] is not None
         else "unknown", worked_out["age_years"] is None),
        ("HU remaining",
         (f"expired {-months} month(s) ago" if months < 0 else f"{months} month(s)")
         if months is not None else "unknown", months is None),
        # The one worth explaining: nothing about the listing is wrong, the
        # config simply has not been told where you are.
        ("distance from home", f"≈ {distance} km" if distance is not None
         else ("no home_plz set" if not home_plz else "unknown"), distance is None),
        ("on the market", f"{days} day(s)" if days is not None else "unknown", days is None),
        ("price change", f"-{drop[0]} € ({drop[1]}%)" if drop else "none", not drop),
        ("photos", str(worked_out["photo_count"]), False),
    ]


def _miscellaneous(row) -> tuple[list, list]:
    """The rest of the typed fields, and whatever the page said that we could
    not use - either because the label is unknown or the value would not parse.

    That second list is the point of keeping the raw attributes at all: a label
    turning up there is one the parser does not understand yet.
    """
    known = _rows(row, MISCELLANEOUS)
    raw = json.loads(row["attributes_json"] or "{}")
    # Only labels with no field anywhere. One that maps to a column is already
    # in a fixed row above - showing what the ad said, if it would not parse -
    # and listing it again here is the duplication this panel was split to end.
    unused = [(label, value) for label, value in raw.items()
              if parse_fields.mapped_column(label) is None]
    return known, unused


def _search_row(search) -> dict:
    return {spec.key: _as_form_text(spec, getattr(search, spec.key, None))
            for spec in fields.SEARCH_FIELDS}


def _parse_search_form(form) -> tuple[list[dict], dict]:
    """Read the repeated search blocks. A row with no name and no url is gone."""
    rows, errors = [], {}
    indexes = sorted({int(key.rsplit("-", 1)[1]) for key in form
                      if key.startswith("name-") and key.rsplit("-", 1)[1].isdigit()})
    position = 0
    for index in indexes:
        raw = {spec.key: (form.get(f"{spec.key}-{index}") or "").strip()
               for spec in fields.SEARCH_FIELDS if spec.kind != "bool"}
        if not raw["name"] and not raw["url"]:
            continue                    # an empty row is how you delete one
        row = {"enabled": form.get(f"enabled-{index}") == "1"}
        for spec in fields.SEARCH_FIELDS:
            if spec.kind == "bool":
                continue
            value = raw[spec.key]
            if not value:
                if not spec.optional:
                    errors[f"{spec.key}-{position}"] = "this cannot be empty"
                row[spec.key] = None
                continue
            try:
                row[spec.key] = _coerce(spec, value)
            except ValueError as exc:
                errors[f"{spec.key}-{position}"] = str(exc)
                row[spec.key] = value
        rows.append(row)
        position += 1

    names = [row["name"] for row in rows if row["name"]]
    for position, row in enumerate(rows):
        if row["name"] and names.count(row["name"]) > 1:
            errors[f"name-{position}"] = "two searches cannot share a name"
    return rows, errors


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
        "next_score": db.get_state(conn, "next_score"),
        "counts": counts,
        "runs": conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8").fetchall(),
        "commands": db.recent_commands(conn, 8),
        "pending": db.pending_counts(conn),
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
    state = filters["state"]
    if state == "active":
        # Ignored listings are still listings; they are just never mailed. They
        # stay out of the default view and have a filter of their own.
        clauses.append("is_active = 1 AND ignored = 0")
    elif state == "delisted":
        clauses.append("is_active = 0")
    elif state == "unscored":
        clauses.append("overall IS NULL AND is_active = 1")
    elif state == "ignored":
        clauses.append("ignored = 1")
    elif state == "needs_refetch":
        clauses.append("needs_refetch = 1")
    elif state == "needs_rescore":
        clauses.append("needs_rescore = 1")
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
