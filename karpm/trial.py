"""`karpm trial` - scrape a search and report how well it parsed.

This exercises the real scrape path (same fetcher, same parsers, same storage,
same image downloads) against a throwaway database, and then reports what was
actually extracted. It deliberately stops before scoring and email: no Claude
API calls, no mail, nothing sent anywhere.

The point is not "did it run" but "did it parse" - a scraper that returns 25
rows of NULLs exits successfully.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import images, pipeline
from .config import SearchConfig
from .http import Fetcher

# The fields worth reporting on, and whether a gap is a problem or just an ad
# that did not state it.
FIELDS = [
    ("title", True), ("price_eur", True), ("description", True),
    ("km", True), ("first_reg_year", True), ("hp", True), ("ccm", False),
    ("make", False), ("model", False), ("location", True), ("postcode", False),
    ("posted_at", True), ("seller_type", False), ("inspection_until", False),
    ("owners", False), ("condition", False),
]


@dataclass
class TrialReport:
    counts: dict
    rows: list
    coverage: dict
    image_stats: dict
    warnings: list

    @property
    def ok(self) -> bool:
        """Every required field present on every listing, and images landed."""
        if not self.rows:
            return False
        required_ok = all(
            self.coverage[name]["missing"] == 0 for name, required in FIELDS if required
        )
        return required_ok and self.image_stats["failed"] == 0


def run_trial(conf, conn, search: SearchConfig, fetcher: Fetcher | None = None,
              download_images: bool = True) -> TrialReport:
    fetcher = fetcher or Fetcher(conf.scrape)

    # A capped run has not looked at the whole search, so absence proves nothing.
    counts = pipeline.scrape_search(conn, conf, fetcher, search, mark_missing=False)

    saved = images.download_pending(
        conn, fetcher, conf.images, delay_range=conf.scrape.image_delay_range
    ) if download_images else 0

    rows = _attach_image_counts(conn, conn.execute(
        "SELECT * FROM listings WHERE search_name = ? ORDER BY first_seen_at", (search.name,)
    ).fetchall())

    return TrialReport(
        counts=counts,
        rows=rows,
        coverage=_coverage(rows),
        image_stats=_image_stats(conn, saved, download_images),
        warnings=_warnings(rows),
    )


def _attach_image_counts(conn, rows) -> list[dict]:
    """sqlite3.Row is read-only, so return plain dicts carrying the count."""
    out = []
    for row in rows:
        item = dict(row)
        item["images"] = conn.execute(
            "SELECT COUNT(*) n FROM images WHERE listing_id = ? AND local_path IS NOT NULL",
            (row["id"],),
        ).fetchone()["n"]
        out.append(item)
    return out


def _coverage(rows) -> dict:
    coverage = {}
    for name, required in FIELDS:
        missing = [r["id"] for r in rows if r[name] in (None, "")]
        coverage[name] = {
            "present": len(rows) - len(missing),
            "missing": len(missing),
            "missing_ids": missing,
            "required": required,
        }
    return coverage


def _image_stats(conn, saved: int, attempted: bool) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(local_path IS NOT NULL) downloaded, "
        "COALESCE(SUM(bytes), 0) bytes FROM images"
    ).fetchone()
    total = row["total"] or 0
    downloaded = row["downloaded"] or 0
    listings_with = conn.execute(
        "SELECT COUNT(DISTINCT listing_id) n FROM images WHERE local_path IS NOT NULL"
    ).fetchone()["n"]
    return {
        "urls": total,
        "downloaded": downloaded,
        "failed": (total - downloaded) if attempted else 0,
        "bytes": row["bytes"] or 0,
        "saved_this_run": saved,
        "listings_with_images": listings_with,
        "attempted": attempted,
    }


def _warnings(rows) -> list:
    out = []
    for row in rows:
        parsed = json.loads(row["parse_warnings"] or "[]")
        if parsed:
            out.append({"id": row["id"], "url": row["url"], "warnings": parsed})
    return out


# --- rendering ---------------------------------------------------------------

def _fmt(value, width: int) -> str:
    return ("-" if value in (None, "") else str(value)).rjust(width)


def render(report: TrialReport, limit_note: str = "") -> str:
    counts, lines = report.counts, []
    add = lines.append

    add("SEARCH")
    if counts.get("total_results") is not None:
        add(f"  ads in this search   {counts['total_results']}"
            + (f" across {counts['page_count']} page(s)" if counts.get("page_count") else ""))
    add(f"  pages fetched        {counts.get('pages', 0)}")
    add(f"  ads on those pages   {counts.get('listed', 0)}"
        f"   (matched by selector {counts.get('selector')!r})")
    if counts.get("skipped_wanted"):
        add(f"  wanted ads skipped   {counts['skipped_wanted']}   (Gesuch - buyers, not sellers)")
    add(f"  listings processed   {counts.get('seen', 0)}{limit_note}")
    add(f"  stored as new        {counts.get('new', 0)}")
    if counts.get("changed"):
        add(f"  updated              {counts['changed']}")
    if counts.get("unchanged"):
        add(f"  already current      {counts['unchanged']}   (no ad page fetched)")

    add("")
    add("LISTINGS")
    add(f"  {'id':<11}{'price':>7} {'km':>7} {'EZ':>5} {'PS':>4} {'HU':>8} {'img':>4}  title")
    for row in report.rows:
        add(f"  {row['id']:<11}{_fmt(row['price_eur'], 7)} {_fmt(row['km'], 7)} "
            f"{_fmt(row['first_reg_year'], 5)} {_fmt(row['hp'], 4)} "
            f"{_fmt((row['inspection_until'] or '')[:7], 8)} {_fmt(row['images'], 4)}  "
            f"{(row['title'] or '')[:46]}")

    add("")
    add(f"FIELD COVERAGE  ({len(report.rows)} listing(s))")
    for name, required in FIELDS:
        stat = report.coverage[name]
        total = len(report.rows)
        filled = stat["present"]
        bar = "█" * round(20 * filled / total) if total else ""
        flag = ""
        if stat["missing"]:
            shown = ", ".join(stat["missing_ids"][:3])
            more = f" +{stat['missing'] - 3}" if stat["missing"] > 3 else ""
            flag = f"  {'MISSING' if required else 'not stated'}: {shown}{more}"
        add(f"  {name:<18}{filled:>3}/{total:<3} {bar:<20}{flag}")

    stats = report.image_stats
    add("")
    add("IMAGES")
    if not stats["attempted"]:
        add("  skipped (--no-images)")
    else:
        add(f"  urls found           {stats['urls']}")
        add(f"  downloaded           {stats['downloaded']}"
            f"   ({stats['bytes'] / 1e6:.1f} MB, {stats['listings_with_images']} listing(s))")
        if stats["failed"]:
            add(f"  FAILED               {stats['failed']}")

    if report.warnings:
        add("")
        add("PARSE WARNINGS")
        for item in report.warnings[:10]:
            add(f"  {item['id']}  {', '.join(item['warnings'])}")
            add(f"    {item['url']}")
        if len(report.warnings) > 10:
            add(f"  ... and {len(report.warnings) - 10} more")

    add("")
    add("Scoring and email were not run - no Claude API calls, nothing sent.")
    add("VERDICT: looks good" if report.ok else
        "VERDICT: problems above - check MISSING fields and warnings")
    return "\n".join(lines)


