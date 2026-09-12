"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import daemon, db, images, mailer, pipeline, scoring
from .config import load_config
from .http import Fetcher
from .parse.detail import parse_detail_page
from .parse.search import parse_search_page


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _open(args):
    conf = load_config(args.config)
    conn = db.connect(conf.db_path)
    db.init_db(conn)
    db.sync_searches(conn, conf.searches)
    return conf, conn


def cmd_init(args) -> int:
    conf, conn = _open(args)
    print(f"database ready at {conf.db_path}")
    print(f"{len(conf.searches)} search(es) configured:")
    for search in conf.searches:
        print(f"  - {search.name}: {search.url}")
    conn.close()
    return 0


def cmd_scrape(args) -> int:
    conf, conn = _open(args)
    result = pipeline.run_scrape(conf, conn)
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def cmd_score(args) -> int:
    conf, conn = _open(args)
    result = pipeline.run_scoring_and_alerts(conf, conn)
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def cmd_digest(args) -> int:
    conf, conn = _open(args)
    if args.dry_run:
        rows = mailer.digest_candidates(conn, conf.email)
        print(f"{len(rows)} listing(s) would be included:")
        for row in rows:
            print(f"  [{row['overall']}/5] {row['title']} - {row['price_eur']} EUR  {row['url']}")
        conn.close()
        return 0
    provider_id = pipeline.run_digest(conf, conn)
    print(f"digest sent: {provider_id}" if provider_id else "nothing to send")
    conn.close()
    return 0


def cmd_run(args) -> int:
    conf, conn = _open(args)
    result = pipeline.run_once(conf, conn)
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def cmd_daemon(args) -> int:
    conf, conn = _open(args)
    daemon.run_forever(conf, conn)
    conn.close()
    return 0


# Markers that appear on an ad page and nowhere else. Note that a results page
# is full of "/s-anzeige/" links, so that substring is only usable on the URL.
DETAIL_MARKERS = (
    'id="viewad-title"',
    'id="viewad-price"',
    'id="viewad-description',
    "addetailslist--detail",
)


def _page_kind(html: str, explicit: str, url: str | None) -> str:
    """Decide whether this page is an ad or a results list.

    The URL is the reliable signal, but `probe --file` has only the HTML.
    """
    if explicit != "auto":
        return explicit
    if url and "/s-anzeige/" in url:
        return "detail"
    return "detail" if any(marker in html for marker in DETAIL_MARKERS) else "search"


def cmd_probe(args) -> int:
    """Fetch one page and report what the parsers can extract from it.

    Run this first on the Pi: it is how you verify the selectors still match
    the live site without touching the database.
    """
    conf = load_config(args.config)
    fetcher = Fetcher(conf.scrape)
    html = Path(args.file).read_text(encoding="utf-8") if args.file else fetcher.get(args.url)

    if args.save:
        target = Path(args.save)
        target.write_text(html, encoding="utf-8")
        print(f"saved {len(html)} bytes to {target}")

    if _page_kind(html, args.kind, args.url) == "detail":
        data = parse_detail_page(html, args.url)
        if args.brief:
            data.pop("description", None)
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        print(f"\nparse warnings: {data.get('parse_warnings')}", file=sys.stderr)
    else:
        result = parse_search_page(html, base_url=args.url or "https://www.kleinanzeigen.de")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\n{len(result['items'])} item(s) via selector {result['selector']!r}",
              file=sys.stderr)
    return 0


def cmd_score_one(args) -> int:
    """Score a single listing already in the database - useful for tuning
    preferences.md without spending a full run."""
    conf, conn = _open(args)
    row = db.get_listing(conn, args.listing_id)
    if row is None:
        print(f"listing {args.listing_id} not found", file=sys.stderr)
        return 1
    scorer = scoring.Scorer(conf.scoring, scoring.load_preferences(conf.scoring.preferences_file))
    if args.show_prompt:
        print(scoring.listing_to_text(row, db.comparable_stats(conn, row)))
        return 0
    score = scorer.score_listing(conn, row)
    print(json.dumps(score, indent=2, ensure_ascii=False))
    if args.save:
        db.add_score(conn, row["id"], score)
        conn.commit()
        print("saved", file=sys.stderr)
    conn.close()
    return 0


def cmd_images(args) -> int:
    conf, conn = _open(args)
    saved = images.download_pending(conn, Fetcher(conf.scrape), conf.images, limit=args.limit)
    print(f"downloaded {saved} image(s)")
    conn.close()
    return 0


def cmd_stats(args) -> int:
    conf, conn = _open(args)
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(is_active) AS active,
               SUM(CASE WHEN price_eur IS NULL THEN 1 ELSE 0 END) AS no_price,
               MIN(first_seen_at) AS since
        FROM listings
        """
    ).fetchone()
    scored = conn.execute("SELECT COUNT(DISTINCT listing_id) AS n FROM scores").fetchone()["n"]
    print(f"listings:      {row['total']} ({row['active'] or 0} active) since {row['since']}")
    print(f"scored:        {scored}")
    print(f"without price: {row['no_price'] or 0}")

    print("\nby score:")
    for line in conn.execute(
        "SELECT overall, COUNT(*) n FROM listing_current WHERE overall IS NOT NULL "
        "GROUP BY overall ORDER BY overall DESC"
    ):
        print(f"  {line['overall']}/5  {'#' * min(40, line['n'])} {line['n']}")

    print("\nrecent price drops:")
    for line in conn.execute(
        """
        SELECT l.title, h.prev_price_eur, h.price_eur, h.observed_at
        FROM listing_history h JOIN listings l ON l.id = h.listing_id
        WHERE h.event = 'price_change' AND h.prev_price_eur IS NOT NULL
        ORDER BY h.observed_at DESC LIMIT 10
        """
    ):
        print(f"  {line['observed_at'][:10]}  {line['prev_price_eur']} -> {line['price_eur']} "
              f"  {line['title'][:60]}")
    conn.close()
    return 0


def cmd_top(args) -> int:
    conf, conn = _open(args)
    rows = conn.execute(
        """
        SELECT * FROM listing_current
        WHERE is_active = 1 AND overall IS NOT NULL AND overall >= ?
        ORDER BY overall DESC, value DESC, price_eur ASC LIMIT ?
        """,
        (args.min_score, args.limit),
    ).fetchall()
    for row in rows:
        print(f"[{row['overall']}/5 fit {row['fit']} value {row['value']}] "
              f"{row['price_eur']} EUR  {row['title']}")
        print(f"    {row['headline']}")
        print(f"    {row['url']}\n")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="karpm", description=__doc__)
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and register searches").set_defaults(
        func=cmd_init)
    sub.add_parser("scrape", help="fetch listings once").set_defaults(func=cmd_scrape)
    sub.add_parser("score", help="score unscored listings and send instant alerts").set_defaults(
        func=cmd_score)
    sub.add_parser("run", help="scrape, then score and alert").set_defaults(func=cmd_run)
    sub.add_parser("daemon", help="run continuously on the configured schedule").set_defaults(
        func=cmd_daemon)
    sub.add_parser("stats", help="summarise what has been collected").set_defaults(func=cmd_stats)

    p_digest = sub.add_parser("digest", help="send the digest email")
    p_digest.add_argument("--dry-run", action="store_true", help="list what would be sent")
    p_digest.set_defaults(func=cmd_digest)

    p_probe = sub.add_parser("probe", help="parse one live or saved page and dump the result")
    p_probe.add_argument("--url", help="page to fetch")
    p_probe.add_argument("--file", help="saved HTML file to parse instead")
    p_probe.add_argument("--save", help="write the fetched HTML here")
    p_probe.add_argument("--brief", action="store_true", help="omit the description")
    p_probe.add_argument("--kind", choices=("auto", "search", "detail"), default="auto",
                         help="force how the page is parsed (default: detect)")
    p_probe.set_defaults(func=cmd_probe)

    p_one = sub.add_parser("score-one", help="score a single listing by id")
    p_one.add_argument("listing_id")
    p_one.add_argument("--save", action="store_true", help="store the score")
    p_one.add_argument("--show-prompt", action="store_true", help="print the prompt, don't call the API")
    p_one.set_defaults(func=cmd_score_one)

    p_images = sub.add_parser("images", help="download images that have no local file yet")
    p_images.add_argument("--limit", type=int, default=500)
    p_images.set_defaults(func=cmd_images)

    p_top = sub.add_parser("top", help="best-scoring active listings")
    p_top.add_argument("--min-score", type=int, default=4)
    p_top.add_argument("--limit", type=int, default=20)
    p_top.set_defaults(func=cmd_top)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.command == "probe" and not (args.url or args.file):
        parser.error("probe needs --url or --file")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
