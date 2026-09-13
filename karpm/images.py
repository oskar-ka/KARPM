"""Downloading listing photos to disk.

Images are kept locally so a listing stays reviewable after it is taken down -
which is exactly when you most want to compare it against what is on the market
now.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

# The CDN serves each photo under a "rule" that names a rendition. The gallery
# links $_59.AUTO, the page's own JSON-LD links $_59.JPG for the same image, and
# not every rendition exists for every photo - so a 404 on one says nothing about
# the others. Tried in order; the bare URL, with no rule at all, is the last
# resort.
RULE_FALLBACKS = ("$_59.JPG", "$_59.AUTO", "$_57.AUTO", "$_35.AUTO")


def candidate_urls(url: str, preferred: str | None = None) -> list[str]:
    """The same photo addressed by each rendition worth trying.

    `preferred` is a rendition already known to work in this run, tried first so
    a CDN that has dropped one rendition does not cost a wasted request on every
    single photo.
    """
    base, _, query = url.partition("?")
    candidates = []
    if preferred and "rule=" in query:
        candidates.append(f"{base}?rule={preferred}")
    if url not in candidates:
        candidates.append(url)
    if "rule=" in query:
        for rule in RULE_FALLBACKS:
            alternative = f"{base}?rule={rule}"
            if alternative not in candidates:
                candidates.append(alternative)
    if base not in candidates:
        candidates.append(base)
    return candidates


def download_pending(conn, fetcher, cfg, limit: int = 500,
                     delay_range: tuple[float, float] | None = None) -> int:
    """Fetch every image row that has no local file yet. Returns count saved."""
    if not cfg.enabled:
        return 0

    root = Path(cfg.dir)
    root.mkdir(parents=True, exist_ok=True)
    saved = 0

    fallbacks: Counter[str] = Counter()
    preferred: str | None = None
    rows = db.pending_images(conn, limit=limit)
    if rows:
        pace = delay_range or (1.0, 1.0)
        log.info("downloading %s image(s), roughly %.0fs",
                 len(rows), len(rows) * sum(pace) / 2)
    per_listing: dict[str, int] = {}
    for row in rows:
        listing_id = row["listing_id"]
        per_listing[listing_id] = per_listing.get(listing_id, 0) + 1
        if cfg.max_per_listing is not None and per_listing[listing_id] > cfg.max_per_listing:
            continue
        data, used = _download(fetcher, row, delay_range, preferred)
        if data is None:
            continue
        if used != row["url"]:
            rule = used.partition("rule=")[2]
            fallbacks[rule or "no rule"] += 1
            preferred = rule or None
        if not data or len(data) > cfg.max_bytes:
            log.warning("skipping image %s (%s bytes)", row["url"], len(data or b""))
            continue

        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(row["url"].split("?")[0]).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            suffix = ".jpg"
        target = root / listing_id / f"{row['position']:02d}_{digest[:12]}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        db.record_image_download(conn, row["id"], str(target), digest, len(data))
        saved += 1
        if saved % 10 == 0:
            log.info("  ... %s/%s images", saved, len(rows))
            conn.commit()

    conn.commit()
    if fallbacks:
        # Not every photo exists under the rendition the gallery links to, so
        # say how often the fallbacks were needed rather than per photo.
        summary = ", ".join(f"{count}x {rule}" for rule, count in fallbacks.most_common())
        log.info("%s photo(s) needed a different rendition than the one linked (%s)",
                 sum(fallbacks.values()), summary)
    return saved


def _download(fetcher, row, delay_range, preferred=None) -> tuple[bytes | None, str | None]:
    """Fetch one photo, trying the other renditions if the linked one is gone."""
    listing = row["listing_url"] if "listing_url" in row.keys() else row["listing_id"]
    tried = candidate_urls(row["url"], preferred)

    for url in tried:
        try:
            return fetcher.get(url, binary=True, delay_range=delay_range), url
        except FileNotFoundError:
            continue                                   # this rendition is missing
        except Exception as exc:                       # one bad image is not fatal
            log.warning("image download failed for %s\n    photo: %s\n    error: %s",
                        listing, url, exc)
            return None, None

    log.warning("no rendition of this photo exists (%s tried)\n    ad:    %s\n    photo: %s",
                len(tried), listing, row["url"])
    return None, None


def media_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
