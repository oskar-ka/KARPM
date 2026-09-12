"""Downloading listing photos to disk.

Images are kept locally so a listing stays reviewable after it is taken down -
which is exactly when you most want to compare it against what is on the market
now.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def download_pending(conn, fetcher, cfg, limit: int = 500) -> int:
    """Fetch every image row that has no local file yet. Returns count saved."""
    if not cfg.enabled:
        return 0

    root = Path(cfg.dir)
    root.mkdir(parents=True, exist_ok=True)
    saved = 0

    rows = db.pending_images(conn, limit=limit)
    per_listing: dict[str, int] = {}
    for row in rows:
        listing_id = row["listing_id"]
        per_listing[listing_id] = per_listing.get(listing_id, 0) + 1
        if per_listing[listing_id] > cfg.max_per_listing:
            continue
        try:
            data = fetcher.get(row["url"], binary=True)
        except Exception as exc:                       # one bad image is not fatal
            log.warning("image download failed %s: %s", row["url"], exc)
            continue
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

    conn.commit()
    return saved


def media_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
