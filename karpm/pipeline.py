"""The end-to-end run: scrape -> store -> download images -> score -> alert."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timedelta, timezone

from . import db, images, mailer, scoring
from .ai import extract
from .config import load_config
from .http import Blocked, Fetcher
from .parse import detail
from .parse.detail import parse_detail_page
from .parse.search import parse_search_page

log = logging.getLogger(__name__)


def _needs_refresh(row, refresh_after_hours: int) -> bool:
    """Re-fetch a known ad's detail page only occasionally - the search page
    already tells us it still exists, and the price shown there catches most
    changes."""
    if row["needs_refetch"]:
        return True                     # the parser changed, or you asked
    try:
        last = datetime.fromisoformat(row["last_seen_at"])
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > timedelta(hours=refresh_after_hours)


@dataclass
class SearchPlan:
    """What one search contains, worked out before anything is fetched."""

    search: object
    items: list = field(default_factory=list)
    total_results: int | None = None
    page_count: int | None = None
    per_page: int | None = None
    pages_walked: int = 0
    skipped_wanted: int = 0
    duplicates: int = 0              # the same ad offered on more than one page
    truncated: bool = False          # we stopped early, so we did not see it all
    stopped_because: str | None = None
    selector: str | None = None

    # filled in by classify_plan()
    new: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    refresh: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)

    @property
    def to_fetch(self) -> list:
        return self.new + self.changed + self.refresh

    def images_expected(self, max_per_listing: int | None) -> int:
        """Exact number of photos the ads we will fetch are going to yield.

        The search page shows each ad's photo count on its thumbnail, so this
        is a count rather than an estimate. Ads with one photo carry no badge,
        which is why an unknown count is taken as 1.
        """
        total = 0
        for item in self.to_fetch:
            count = item.get("image_count") or 1
            total += min(count, max_per_listing) if max_per_listing else count
        return total


def enumerate_search(cfg, fetcher: Fetcher, search, save_pages=None) -> SearchPlan:
    """Phase one: walk the search pages and collect what is there.

    Nothing is fetched beyond the result pages themselves. Doing this first
    costs a handful of requests and buys an exact plan: how many ads exist, how
    many are new, and precisely how many photos are about to be downloaded.
    """
    plan = SearchPlan(search=search)
    collected: set[str] = set()
    url: str | None = search.url
    page = 0

    while url:
        log.info("[%s] search page %s%s", search.name, page + 1,
                 f"/{plan.page_count}" if plan.page_count else "")
        try:
            html = fetcher.get(url)
        except FileNotFoundError:
            if page == 0:
                raise
            # Results shrink while we are walking them, so a later page can
            # vanish between being linked and being asked for. That is the end
            # of the pagination, not a failed run.
            log.info("[%s] page %s is gone (404) - treating it as the end",
                     search.name, page + 1)
            break
        if save_pages is not None:
            target = Path(save_pages) / f"page_{page + 1:02d}.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"<!-- {url} -->\n{html}", encoding="utf-8")
            log.info("[%s] saved %s", search.name, target)
        result = parse_search_page(html, base_url=url)

        if not result["items"]:
            log.warning(
                "[%s] no listings parsed from %s - the page markup may have changed. "
                "Run `karpm probe --url <url> --save` and check the saved HTML.",
                search.name, url,
            )
            plan.stopped_because = "a page returned no parseable ads"
            break

        plan.selector = result["selector"]
        if result.get("total_results") is not None:
            plan.total_results = result["total_results"]
            # Page one is the authority on page size; later pages only revise
            # the total, and the last one cannot report a size at all.
            if plan.per_page is None:
                plan.per_page = result.get("per_page")
                plan.page_count = result.get("page_count")

        for item in result["items"]:
            # "Gesuch" ads are people wanting to buy, not sell. Storing them
            # would skew the price comparables and waste scoring calls.
            if item.get("is_wanted"):
                plan.skipped_wanted += 1
                continue
            # A paid TOP placement on page one reappears in its organic position
            # further in, so ads have to be deduplicated across pages and not
            # just within one - otherwise it is fetched and counted twice.
            if item["id"] in collected:
                plan.duplicates += 1
                continue
            collected.add(item["id"])
            plan.items.append(item)

        page += 1
        plan.pages_walked = page
        url = result["next_url"]

        if search.max_ads is not None and len(plan.items) >= search.max_ads:
            del plan.items[search.max_ads:]
            plan.truncated = True
            plan.stopped_because = f"reached the {search.max_ads}-ad limit"
            break
        if url is None:
            plan.stopped_because = "the last page offered no next link"
            break

    log.info("[%s] stopped after %s page(s): %s", search.name, plan.pages_walked,
             plan.stopped_because or "no more pages")
    # The search says how many pages it has; stopping short of that without
    # being told to is the symptom of a pagination control we cannot follow.
    if (plan.page_count and plan.pages_walked < plan.page_count
            and not plan.truncated):
        log.warning("[%s] the search claims %s page(s) but the walk ended after %s. "
                    "Re-run with --save-pages DIR and look at the last file saved.",
                    search.name, plan.page_count, plan.pages_walked)
    return plan


def classify_plan(conn, cfg, plan: SearchPlan) -> SearchPlan:
    """Sort the plan's ads into new, changed, stale and unchanged.

    Only the first three need their page fetched; the rest are simply still
    there and get their last_seen timestamp bumped.
    """
    for item in plan.items:
        existing = db.get_listing(conn, item["id"])
        if existing is None:
            plan.new.append(item)
        elif item["price_eur"] is not None and item["price_eur"] != existing["price_eur"]:
            plan.changed.append(item)
        elif _needs_refresh(existing, cfg.scrape.refresh_after_hours):
            plan.refresh.append(item)
        else:
            plan.unchanged.append(item)
    return plan


def describe_plan(plan: SearchPlan, cfg) -> str:
    images = plan.images_expected(cfg.images.max_per_listing if cfg.images.enabled else 0)
    lines = [
        f"  {plan.total_results if plan.total_results is not None else len(plan.items)} ad(s) "
        f"reported by the search"
        + (f" across {plan.page_count} page(s)" if plan.page_count else ""),
        f"  {plan.pages_walked} page(s) walked, {len(plan.items)} ad(s) collected"
        + (f", {plan.skipped_wanted} wanted ad(s) skipped" if plan.skipped_wanted else "")
        + (f", {plan.duplicates} repeat(s) of a promoted ad" if plan.duplicates else ""),
        f"  {len(plan.new)} new, {len(plan.changed)} with a new price, "
        f"{len(plan.refresh)} due a refresh, {len(plan.unchanged)} unchanged",
        f"  {len(plan.to_fetch)} ad page(s) and {images} image(s) to fetch",
    ]
    if plan.stopped_because:
        lines.append(f"  stopped because {plan.stopped_because}")
    if plan.truncated:
        lines.append("  (this is not the whole search)")
    return "\n".join(lines)


def scrape_search(conn, cfg, fetcher: Fetcher, search, mark_missing: bool = True,
                  save_pages=None) -> dict:
    """Walk one saved search: enumerate it, then fetch only what needs fetching.

    `mark_missing` reconciles listings that were not in the results. A run that
    stopped early never saw the whole search, so it must not draw conclusions
    from an ad's absence.
    """
    global _short_dumps
    _short_dumps = 0
    plan = classify_plan(conn, cfg, enumerate_search(cfg, fetcher, search, save_pages))
    log.info("[%s] plan:\n%s", search.name, describe_plan(plan, cfg))

    counts = {"seen": len(plan.items), "new": 0, "changed": 0, "pages": plan.pages_walked,
              "delisted": 0, "skipped_wanted": plan.skipped_wanted, "listed": len(plan.items),
              "capped": plan.truncated, "selector": plan.selector,
              "total_results": plan.total_results, "page_count": plan.page_count,
              "unchanged": len(plan.unchanged),
              "images_expected": plan.images_expected(cfg.images.max_per_listing)}

    seen_ids = {item["id"] for item in plan.items}

    short: list[tuple[str, int, int]] = []

    for item in plan.unchanged:
        db.touch_listing(conn, item["id"])

    for index, item in enumerate(plan.to_fetch, start=1):
        log.info("[%s] ad %s/%s: %s", search.name, index, len(plan.to_fetch), item["id"])
        outcome, stored_id, photos = _fetch_and_store(conn, cfg, fetcher, item, search,
                                                      referer=search.url)
        # The search page already said how many photos this ad has. Finding far
        # fewer on the ad page means the gallery is not where we looked.
        promised = item.get("image_count")
        if promised and photos < min(promised, cfg.images.max_per_listing or promised):
            short.append((item["id"], promised, photos))
        # The ad page is the authority on its own id; record that too, so a
        # listing is never reported missing just because the two disagree.
        if stored_id:
            seen_ids.add(stored_id)
        if outcome == "new":
            counts["new"] += 1
        elif outcome in ("price_change", "edited", "relisted"):
            counts["changed"] += 1
        if index % 10 == 0:
            conn.commit()

    conn.commit()

    if short:
        expected = sum(p for _, p, _ in short)
        found = sum(f for _, _, f in short)
        log.warning(
            "%s ad(s) yielded fewer photos than their search listing advertised "
            "(%s expected, %s found). Up to %s of those pages are saved in %s/ - "
            "send one of those files.",
            len(short), expected, found, SHORT_DUMP_BUDGET, cfg.scrape.dump_dir,
        )
    counts["photos_short"] = len(short)

    if mark_missing and plan.truncated:
        log.info("[%s] skipping the delisting check - the run stopped early, so an ad's "
                 "absence from what we saw proves nothing", search.name)
    elif mark_missing:
        counts.update(reconcile_missing(conn, cfg, fetcher, search, seen_ids))
        conn.commit()
    return counts


def _fetch_and_store(conn, cfg, fetcher, item, search, referer=None) -> tuple[str, str | None]:
    try:
        html = fetcher.get(item["url"], referer=referer)
    except FileNotFoundError:
        log.info("listing %s is already gone (404)", item["id"])
        return "gone", None, 0
    return _store_detail(conn, cfg, html, item, search)


# How many short-gallery pages one run will save before it stops bothering.
SHORT_DUMP_BUDGET = 3
_short_dumps = 0


def _dump_short_gallery(cfg, listing_id, url, html, found, promised) -> str | None:
    """Save an ad page that yielded fewer photos than its listing advertised.

    Reasoning about why a page parsed badly, without the bytes that page
    actually contained, has now been wrong twice. This keeps the evidence.
    """
    global _short_dumps
    if _short_dumps >= SHORT_DUMP_BUDGET:
        return None
    try:
        target = Path(cfg.scrape.dump_dir) / f"short_gallery_{listing_id}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"<!-- {url}\n     advertised {promised} photo(s), parsed {found} -->\n{html}",
            encoding="utf-8")
    except OSError as exc:
        log.debug("could not save %s: %s", listing_id, exc)
        return None
    _short_dumps += 1
    return str(target)


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

    promised = item.get("image_count")
    cap = cfg.images.max_per_listing
    if promised and len(image_urls) < min(promised, cap or promised):
        saved = _dump_short_gallery(cfg, data["id"], item["url"], html,
                                    len(image_urls), promised)
        if saved:
            log.warning("ad %s advertised %s photo(s) but only %s parsed - page saved to %s",
                        data["id"], promised, len(image_urls), saved)
    return outcome, data["id"], len(image_urls)


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
    # The daemon rereads its config every cycle, so a search added in the web UI
    # arrives here without a row in `searches`. Without this the scrape still
    # runs, but the UPDATE below matches nothing and the search never shows a
    # last run - working, and looking like it never happened.
    db.sync_searches(conn, conf.searches)
    run_id = db.start_run(conn, "scrape")
    totals = {"seen": 0, "new": 0, "changed": 0, "delisted": 0, "skipped_wanted": 0,
              "still_live": 0, "unverified": 0, "unchanged": 0}
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
        # not a count: true if any search stopped short of the whole result set
        totals["capped"] = totals.get("capped", False) or bool(counts.get("capped"))
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


def enabled_switch(config_path, section: str):
    """A check that re-reads the config, for asking mid-run whether to carry on.

    Each of the three passes has its own `enabled`, and each is asked about its
    own - switching the photo pass off should not stop the scoring that is
    running beside it.

    None when there is no file to re-read, which means "carry on" - a caller
    without a config path has nothing newer to learn.
    """
    if not config_path:
        return None

    def still_enabled() -> bool:
        try:
            return getattr(load_config(config_path), section).enabled
        except Exception:       # a half-saved config is not a reason to stop
            return True

    return still_enabled


def scoring_switch(config_path):
    return enabled_switch(config_path, "scoring")


def run_extraction(conf, conn, config_path=None, client=None) -> dict:
    """Passes 1 and 2: read the description, then look at the photos.

    Both write findings beside the listing rather than into it. They are worth
    running even with scoring off - the findings show on the listing page - so
    neither is gated on `scoring.enabled`.
    """
    return {
        "extract_text": extract.run_pass(
            conn, "text", conf.extract_text, client,
            enabled_switch(config_path, "extract_text")),
        "extract_photos": extract.run_pass(
            conn, "photos", conf.extract_photos, client,
            enabled_switch(config_path, "extract_photos")),
    }


def run_scoring_and_alerts(conf, conn, config_path=None) -> dict:
    """Score whatever needs scoring, then mail anything that clears the bar."""
    if not conf.scoring.enabled:
        log.info("scoring is disabled in the config; skipping it")
        return {"scored": 0, "alerts": 0, "alert_failures": 0, "skipped": "disabled"}

    run_id = db.start_run(conn, "score")
    scored = scoring.score_pending(conn, conf.scoring, conf.home_plz,
                                   still_enabled=scoring_switch(config_path))
    alerts = 0
    alert_failures = 0

    for score in scored:
        if score.get("ignored"):
            continue                    # you have dismissed it; no interruption
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


def any_ai_enabled(conf) -> bool:
    """Whether any of the three passes would do something."""
    return (conf.extract_text.enabled or conf.extract_photos.enabled
            or conf.scoring.enabled)


def run_ai_passes(conf, conn, config_path=None, client=None) -> dict:
    """The three passes in order, and the alerts the third one earns.

    This is everything that costs money, which is why the schedule gates it as
    one thing: pass 3 is the only one you would set a slot for, and running it
    without the two that feed it would score listings on less than is known
    about them.
    """
    result = run_extraction(conf, conn, config_path, client)
    result.update(run_scoring_and_alerts(conf, conn, config_path))
    return result


def run_once(conf, conn, config_path=None, client=None) -> dict:
    """One full cycle. This is what a scrape slot, or the button, triggers.

    The AI passes ride along unless they have slots of their own - if they do,
    the point of setting them was to decide when the spending happens. Each
    pass still checks its own `enabled`, so one of them being off is not a
    reason to skip the others.
    """
    result = run_scrape(conf, conn)
    if conf.schedule.score_at:
        log.info("the AI passes have their own slots (%s), so this run only scrapes",
                 ", ".join(conf.schedule.score_at))
    else:
        result.update(run_ai_passes(conf, conn, config_path, client))
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
