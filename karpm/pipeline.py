"""The end-to-end run: scrape -> store -> download images -> score -> alert."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db, images, mailer, scoring
from .http import Blocked, Fetcher
from .parse.detail import parse_detail_page
from .parse.search import parse_search_page

log = logging.getLogger(__name__)


def _needs_refresh(row, refresh_after_hours: int) -> bool:
    """Re-fetch a known ad's detail page only occasionally - the search page
    already tells us it still exists, and the price shown there catches most
    changes."""
    try:
        last = datetime.fromisoformat(row["last_seen_at"])
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > timedelta(hours=refresh_after_hours)


def scrape_search(conn, cfg, fetcher: Fetcher, search, mark_missing: bool = True) -> dict:
    """Walk one saved search, storing every listing it returns.

    `mark_missing` flags listings that were not seen this run as delisted. A
    capped run (`search.max_listings`) must not do that - it never looked at
    the rest of the search, so their absence means nothing.
    """
    counts = {"seen": 0, "new": 0, "changed": 0, "pages": 0, "delisted": 0,
              "skipped_wanted": 0, "listed": 0, "capped": False}
    seen_ids: set[str] = set()
    url: str | None = search.url
    page = 0

    while url and page < search.max_pages:
        log.info("[%s] page %s: %s", search.name, page + 1, url)
        html = fetcher.get(url)
        result = parse_search_page(html, base_url=url)

        if not result["items"]:
            log.warning(
                "[%s] no listings parsed from %s - the page markup may have changed. "
                "Run `karpm probe --url <url> --save` and check the saved HTML.",
                search.name, url,
            )
            break

        counts["listed"] += len(result["items"])
        counts["selector"] = result["selector"]

        for item in result["items"]:
            # "Gesuch" ads are people wanting to buy, not sell. Storing them
            # would skew the price comparables and waste scoring calls.
            if item.get("is_wanted"):
                counts["skipped_wanted"] += 1
                continue

            seen_ids.add(item["id"])
            counts["seen"] += 1
            existing = db.get_listing(conn, item["id"])

            if existing is not None:
                price_changed = (
                    item["price_eur"] is not None
                    and item["price_eur"] != existing["price_eur"]
                )
                if not price_changed and not _needs_refresh(existing, cfg.scrape.refresh_after_hours):
                    db.touch_listing(conn, item["id"])
                    continue

            outcome = _fetch_and_store(conn, cfg, fetcher, item, search, referer=url)
            if outcome == "new":
                counts["new"] += 1
            elif outcome in ("price_change", "edited", "relisted"):
                counts["changed"] += 1

            if search.max_listings is not None and counts["seen"] >= search.max_listings:
                conn.commit()
                counts["pages"] = page + 1
                counts["capped"] = True
                return counts

        conn.commit()
        url = result["next_url"]
        page += 1
        counts["pages"] = page

    if mark_missing:
        counts["delisted"] = db.mark_delisted(conn, seen_ids, search.name)
        conn.commit()
    return counts


def _fetch_and_store(conn, cfg, fetcher, item, search, referer=None) -> str:
    try:
        html = fetcher.get(item["url"], referer=referer)
    except FileNotFoundError:
        log.info("listing %s is already gone (404)", item["id"])
        return "gone"

    data = parse_detail_page(html, item["url"])
    # The search page already gave us a usable version of several fields; use it
    # wherever the detail page did not yield one.
    data["id"] = data.get("id") or item["id"]
    data["title"] = data.get("title") or item["title"]
    data["make"] = data.get("make") or search.make
    data["model"] = data.get("model") or search.model
    data["location"] = data.get("location") or item.get("location")
    data["posted_at"] = data.get("posted_at") or item.get("posted_at")
    if data.get("price_eur") is None:
        data["price_eur"], data["price_kind"] = item["price_eur"], item["price_kind"]
    if data.get("seller_type") in (None, "unknown") and item.get("is_commercial"):
        data["seller_type"] = "commercial"
    data["search_name"] = search.name

    image_urls = data.pop("image_urls", []) or []
    outcome = db.upsert_listing(conn, data)
    for position, image_url in enumerate(image_urls[: cfg.images.max_per_listing]):
        db.add_image(conn, data["id"], position, image_url)

    if data.get("parse_warnings"):
        log.debug("listing %s parse warnings: %s", data["id"], data["parse_warnings"])
    return outcome


def run_scrape(conf, conn, fetcher: Fetcher | None = None) -> dict:
    fetcher = fetcher or Fetcher(conf.scrape)
    run_id = db.start_run(conn, "scrape")
    totals = {"seen": 0, "new": 0, "changed": 0, "delisted": 0, "skipped_wanted": 0}
    ok = True
    error = None

    for search in conf.searches:
        if not search.enabled:
            continue
        try:
            counts = scrape_search(conn, conf, fetcher, search)
        except Blocked as exc:
            log.error("aborting run: %s", exc)
            ok, error = False, str(exc)
            break
        except Exception as exc:
            log.exception("search %s failed", search.name)
            ok, error = False, str(exc)
            continue
        for key in totals:
            totals[key] += counts.get(key, 0)
        conn.execute(
            "UPDATE searches SET last_run_at = ?, last_status = ? WHERE name = ?",
            (db.utcnow(), f"{counts['new']} new / {counts['seen']} seen", search.name),
        )
        conn.commit()

    saved = images.download_pending(conn, fetcher, conf.images)
    log.info("downloaded %s images", saved)

    db.finish_run(
        conn, run_id, ok,
        listings_seen=totals["seen"], listings_new=totals["new"],
        listings_changed=totals["changed"], error=error,
    )
    return totals


def run_scoring_and_alerts(conf, conn) -> dict:
    """Score whatever needs scoring, then mail anything that clears the bar."""
    run_id = db.start_run(conn, "score")
    scored = scoring.score_pending(conn, conf.scoring)
    alerts = 0

    for score in scored:
        if not mailer.qualifies_for_instant(conf.email, score, score.get("price_eur")):
            continue
        if db.already_notified(conn, score["listing_id"], "instant"):
            continue
        try:
            mailer.send_instant_alert(conn, conf.email, conf.resend_api_key, score["listing_id"])
            # An instant alert has already shown you the listing; don't repeat it
            # in the next digest.
            db.record_notification(conn, score["listing_id"], "digest")
            conn.commit()
            alerts += 1
        except mailer.MailError as exc:
            log.error("instant alert failed for %s: %s", score["listing_id"], exc)

    db.finish_run(conn, run_id, True, scored=len(scored))
    return {"scored": len(scored), "alerts": alerts}


def run_once(conf, conn) -> dict:
    """One full cycle. This is what the schedule triggers."""
    result = run_scrape(conf, conn)
    if conf.scoring.enabled:
        result.update(run_scoring_and_alerts(conf, conn))
    return result


def run_digest(conf, conn) -> str | None:
    run_id = db.start_run(conn, "digest")
    try:
        provider_id = mailer.send_digest(conn, conf.email, conf.resend_api_key)
    except mailer.MailError as exc:
        log.error("digest failed: %s", exc)
        db.finish_run(conn, run_id, False, error=str(exc))
        return None
    db.finish_run(conn, run_id, True)
    return provider_id
