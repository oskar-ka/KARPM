"""The end-to-end run: scrape -> store -> download images -> score -> alert."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db, images, mailer, scoring
from .http import Blocked, Fetcher
from .parse import detail
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

    while url and (search.max_pages is None or page < search.max_pages):
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

            log.info("[%s] listing %s/%s: %s %s", search.name, counts["seen"],
                     search.max_listings or "?", item["id"], (item.get("title") or "")[:50])
            outcome, stored_id = _fetch_and_store(conn, cfg, fetcher, item, search, referer=url)
            # The ad page is the authority on its own id; record that too, so a
            # listing is never reported missing just because the two disagree.
            if stored_id:
                seen_ids.add(stored_id)
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
        counts.update(reconcile_missing(conn, cfg, fetcher, search, seen_ids))
        conn.commit()
    return counts


def _fetch_and_store(conn, cfg, fetcher, item, search, referer=None) -> tuple[str, str | None]:
    try:
        html = fetcher.get(item["url"], referer=referer)
    except FileNotFoundError:
        log.info("listing %s is already gone (404)", item["id"])
        return "gone", None
    return _store_detail(conn, cfg, html, item, search)


def _store_detail(conn, cfg, html, item, search) -> tuple[str, str]:
    """Parse an already-fetched ad page and store it.

    Returns the outcome and the id it was stored under, which is taken from the
    ad page and can differ from the id the search results advertised.
    """
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
    return outcome, data["id"]


def reconcile_missing(conn, cfg, fetcher, search, seen_ids: set[str]) -> dict:
    """Work out what happened to listings that were not in the search results.

    Absence from the results is only a hint. Kleinanzeigen re-ranks, pagination
    is capped, and a price change can push an ad outside the search's own price
    filter - all of which make a perfectly live listing disappear from the
    results. So each missing listing has its own page fetched, and it is only
    delisted when that page says so. Anything inconclusive is left active and
    tried again next run.
    """
    counts = {"delisted": 0, "still_live": 0, "unverified": 0, "checks": 0}
    missing = db.missing_listings(conn, seen_ids, search.name)

    for row in missing:
        db.record_missing(conn, row["id"])

        if not cfg.scrape.verify_delisting:
            db.mark_delisted(conn, row["id"], reason="assumed")
            counts["delisted"] += 1
            continue

        if _verified_recently(row, cfg.scrape.recheck_missing_after_hours):
            counts["still_live"] += 1
            continue

        if counts["checks"] >= cfg.scrape.max_delist_checks:
            counts["unverified"] += 1
            continue

        counts["checks"] += 1
        status, html = _check_listing(fetcher, row)

        if status == detail.GONE:
            db.mark_delisted(conn, row["id"], reason="verified_gone")
            counts["delisted"] += 1
            log.info("listing %s confirmed gone", row["id"])
        elif status == detail.LIVE:
            # It is still there, just not in the results. Since we have the page
            # in hand, take the update too - the price may be why it dropped out.
            item = {"id": row["id"], "url": row["url"], "title": row["title"],
                    "price_eur": None, "price_kind": None}
            _store_detail(conn, cfg, html, item, search)
            db.clear_missing(conn, row["id"], verified=True)
            counts["still_live"] += 1
            log.info("listing %s missing from results but still live", row["id"])
        else:
            db.record_verification(conn, row["id"])
            counts["unverified"] += 1
            log.warning("listing %s absent from results and its page was unreadable - "
                        "leaving it active", row["id"])

    conn.commit()
    return counts


def _verified_recently(row, hours: int) -> bool:
    if not row["last_verified_at"]:
        return False
    try:
        last = datetime.fromisoformat(row["last_verified_at"])
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(hours=hours)


def _check_listing(fetcher, row) -> tuple[str, str | None]:
    """Fetch a listing's own page and decide whether it still exists."""
    try:
        page = fetcher.fetch(row["url"])
    except FileNotFoundError:
        return detail.GONE, None
    except Blocked:
        raise
    except Exception as exc:
        log.warning("could not check %s: %s", row["url"], exc)
        return detail.UNKNOWN, None

    return detail.classify_ad_page(page.content, page.url, expected_id=row["id"]), page.content


def run_scrape(conf, conn, fetcher: Fetcher | None = None) -> dict:
    fetcher = fetcher or Fetcher(conf.scrape)
    run_id = db.start_run(conn, "scrape")
    totals = {"seen": 0, "new": 0, "changed": 0, "delisted": 0, "skipped_wanted": 0,
              "still_live": 0, "unverified": 0}
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

    saved = images.download_pending(conn, fetcher, conf.images,
                                    delay_range=conf.scrape.image_delay_range)
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
    alert_failures = 0

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
            alert_failures += 1

    db.finish_run(conn, run_id, True, scored=len(scored))
    return {"scored": len(scored), "alerts": alerts, "alert_failures": alert_failures}


def run_once(conf, conn) -> dict:
    """One full cycle. This is what the schedule triggers."""
    result = run_scrape(conf, conn)
    if conf.scoring.enabled:
        result.update(run_scoring_and_alerts(conf, conn))
    return result


def run_digest(conf, conn) -> str | None:
    """Send the digest. Returns the provider id, or None if there was nothing
    to send. Raises MailError if sending failed - "nothing to send" and "could
    not send" must not look alike, or a broken API key reads as a quiet success
    for as long as nobody checks their inbox.
    """
    run_id = db.start_run(conn, "digest")
    try:
        provider_id = mailer.send_digest(conn, conf.email, conf.resend_api_key)
    except mailer.MailError as exc:
        log.error("digest failed: %s", exc)
        db.finish_run(conn, run_id, False, error=str(exc))
        raise
    db.finish_run(conn, run_id, True)
    return provider_id
