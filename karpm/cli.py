"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

from . import daemon, db, images, mailer, pipeline, scoring, trial
from .ai import extract, provider
from .ai import models as ai_models
from .config import SearchConfig, load_config
from .http import Blocked, Fetcher
from .parse.detail import parse_detail_page
from .parse.search import parse_search_page

log = logging.getLogger(__name__)


# Commands meant for trying things out run at the testing pace unless told
# otherwise. The unattended ones - the scheduled runs that go on for months -
# stay polite by default, because those are the ones that would get the Pi's IP
# blocked.
FAST_BY_DEFAULT = {"trial", "probe", "raw"}


def _resolve_pace(args) -> bool:
    if getattr(args, "fast", False):
        return True
    if getattr(args, "polite", False):
        return False
    return args.command in FAST_BY_DEFAULT


def _apply_pace(conf, args):
    """Swap in the [trial] delays when this invocation calls for them."""
    fast = _resolve_pace(args)
    conf.scrape = conf.scrape.at_pace(conf.trial if fast else None)
    if fast and args.command not in FAST_BY_DEFAULT:
        log.warning("running %s at the testing pace (%.1f-%.1fs between pages) - fine for a "
                    "one-off, but not what you want for an unattended schedule",
                    args.command, conf.scrape.min_delay_s, conf.scrape.max_delay_s)
    return conf


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _open(args):
    conf = _apply_pace(load_config(args.config), args)
    # Which file, resolved. Two copies of config.toml in different directories -
    # one the web UI writes, one this reads - look identical until you see the
    # paths side by side.
    log.info("config: %s (scoring %s)", Path(args.config).resolve(),
             "on" if conf.scoring.enabled else "off")
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


# --- showing a prompt ------------------------------------------------------
#
# --show-prompt has to be readable and it has to be complete, and of the two,
# complete wins: a prompt that leaves out the instructions is worse than no
# prompt, because it looks like the whole thing. So everything that goes to the
# model is printed, each part fenced by a line of stars, and everything outside
# those fences is ours and is labelled as such.

FENCE = "*" * 72


def _fenced(name: str, body: str, note: str = "") -> str:
    """One block that is really sent, between two unmistakable lines."""
    opening = f"*** {name} - SENT{(' - ' + note) if note else ''}"
    return (f"\n{FENCE}\n{opening}\n{FENCE}\n{body}"
            f"\n{FENCE}\n*** END OF {name}\n{FENCE}")


def _render_prompt(request, what: str = "") -> str:
    """Everything that would be sent for one request, and nothing else.

    Rendered from the request the pass itself builds rather than assembled here,
    or it would drift from what is really sent - which is exactly what
    --show-prompt exists to show.
    """
    sent = provider._thinking_for(request)          # what this model accepts
    settings = [f"model       {request.model}",
                f"max_tokens  {request.max_tokens}"]
    if "thinking" in sent:
        settings.append(f"thinking    adaptive, effort {sent['output_config']['effort']}")
    else:
        settings.append("thinking    not sent - this model takes neither "
                        "adaptive thinking nor an effort level")
    if request.cache_system:
        settings.append("caching     the system prompt is marked for caching; "
                        "the rest is not")

    photos = [block for block in request.blocks if block["type"] == "image"]
    texts = [block for block in request.blocks if block["type"] == "text"]
    preface = [
        "-" * 72,
        f"PREFACE - none of this is sent{(' - ' + what) if what else ''}",
        "-" * 72,
        *settings,
        f"user turn   {len(texts)} text block(s), {len(photos)} photo(s)",
        "",
        "Everything between a line of stars and its END line is sent verbatim.",
        "Everything else on this page is KARPM talking to you.",
        "-" * 72,
    ]

    out = ["\n".join(preface), _fenced("SYSTEM PROMPT", request.system)]

    body = []
    for block in request.blocks:
        if block["type"] == "text":
            body.append(block["text"])
        else:
            # The bytes go instead of this line. Saying which file, so a
            # shortlist can be checked against the photos on disk.
            body.append(f"[IMAGE: {block['path']} - the file's bytes are sent here]")
    out.append(_fenced("USER MESSAGE", "\n".join(body)))

    if request.schema is not None:
        out.append(_fenced(
            "RESPONSE SCHEMA",
            json.dumps(request.schema.model_json_schema(), indent=2),
            "the shape the answer must fit, not prose the model reads"))
    return "\n".join(out) + "\n"


def _prompt_text(scorer, conn, row, what: str = "") -> str:
    return _render_prompt(scorer.build_request(conn, row), what)


def _extract_kinds(args) -> tuple[str, ...]:
    """Which passes a --text-only / --photos-only pair asks for."""
    if args.text_only:
        return ("text",)
    if args.photos_only:
        return ("photos",)
    return ("text", "photos")


def _say(message: str) -> None:
    """Progress, on stderr so stdout stays pipeable into jq.

    Flushed because the interesting part of this command is the wait: a model
    that thinks for a minute with nothing on screen looks like one that has hung.
    """
    print(message, file=sys.stderr, flush=True)


def _takes_effort(model_id: str) -> bool:
    known = ai_models.get(model_id)
    return known is None or bool(known.effort)


def _extract_config(conf) -> dict:
    return {"text": conf.extract_text, "photos": conf.extract_photos}


def cmd_extract(args) -> int:
    """Passes 1 and 2 on their own, without scraping or scoring."""
    conf, conn = _open(args)
    cfg_for = _extract_config(conf)
    result = {kind: extract.run_pass(conn, kind, cfg_for[kind])
              for kind in _extract_kinds(args)}
    print(json.dumps(result, indent=2))
    conn.close()
    return 1 if any(r.get("failed") for r in result.values()) else 0


def cmd_extract_one(args) -> int:
    """Read a single listing - the cheap way to try a prompt change.

    Unlike `extract`, it does not care whether the listing is due: asking for
    one by id is the explicit instruction, and re-reading something already read
    is most of the point when you are editing a prompt.
    """
    conf, conn = _open(args)
    row = db.get_listing(conn, args.listing_id)
    if row is None:
        print(f"listing {args.listing_id} not found", file=sys.stderr)
        conn.close()
        return 1

    _say(f"{row['id']}: {row['title']}")
    cfg_for = _extract_config(conf)
    failed = False
    for kind in _extract_kinds(args):
        cfg = cfg_for[kind]
        label = "pass 1 (description)" if kind == "text" else "pass 2 (photos)"
        request = extract.build_request(conn, kind, cfg, row)
        if request is None:
            _say(f"{label}: nothing to look at - none of this ad's photos have "
                 f"downloaded")
            continue

        if args.show_prompt:
            print(_render_prompt(
                request, f"{label} for {row['id']}, and no API call was made"))
            continue

        photos = sum(1 for block in request.blocks if block["type"] == "image")
        words = sum(len(block.get("text", "").split()) for block in request.blocks)
        _say(f"{label}: sending {words} word(s)"
             + (f" and {photos} photo(s)" if photos else "")
             + f" to {cfg.model} via {cfg.provider}"
             + (f" at effort {cfg.effort} - a thinking model can take a minute..."
                if _takes_effort(cfg.model) else "..."))

        engine = provider.get(cfg.provider)
        started = time.monotonic()
        try:
            read = extract.read_one(conn, kind, cfg, row, engine)
        except provider.ProviderError as exc:
            _say(f"{label}: FAILED after {time.monotonic() - started:.0f}s - {exc}")
            failed = True
            continue
        data, reply = read
        _say(f"{label}: answered in {time.monotonic() - started:.0f}s - "
             f"{reply.input_tokens} tokens in, {reply.output_tokens} out"
             + (f", {reply.cached_tokens} cached" if reply.cached_tokens else ""))
        print(json.dumps({kind: data}, indent=2, ensure_ascii=False))
        if args.save:
            extract.save(conn, kind, cfg, row, engine, data, reply)
            conn.commit()
            _say(f"{label}: saved")
        else:
            _say(f"{label}: not saved (pass --save to keep it)")

    conn.close()
    return 1 if failed else 0


def cmd_score(args) -> int:
    conf, conn = _open(args)
    result = pipeline.run_scoring_and_alerts(conf, conn)
    print(json.dumps(result, indent=2))
    conn.close()
    return 1 if result.get("alert_failures") else 0


def cmd_digest(args) -> int:
    conf, conn = _open(args)
    if args.dry_run:
        rows = mailer.digest_candidates(conn, conf.email)
        print(f"{len(rows)} listing(s) would be included:")
        for row in rows:
            print(f"  [{row['overall']}/5] {row['title']} - {row['price_eur']} EUR  {row['url']}")
        conn.close()
        return 0
    try:
        provider_id = pipeline.run_digest(conf, conn)
    except mailer.MailError as exc:
        print(f"digest FAILED to send: {exc}", file=sys.stderr)
        conn.close()
        return 1
    print(f"digest sent: {provider_id}" if provider_id else "nothing new to send")
    conn.close()
    return 0


def cmd_run(args) -> int:
    conf, conn = _open(args)
    result = pipeline.run_once(conf, conn, args.config)
    print(json.dumps(result, indent=2))
    conn.close()
    return 0


def cmd_daemon(args) -> int:
    conf, conn = _open(args)
    daemon.run_forever(conf, conn, config_path=args.config)
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


class SearchNotFound(LookupError):
    """Raised when --search names something that is not in the config."""


def _search_by_name(conf, name: str, config_path: str):
    configured = {s.name: s for s in conf.searches}
    if name not in configured:
        raise SearchNotFound(
            f"no search named {name!r} in {config_path}. "
            f"Available: {', '.join(configured) or 'none'}"
        )
    return configured[name]


def cmd_raw(args) -> int:
    """One request, no retries, no backoff - just what the server said.

    `probe` and `trial` go through the polite fetcher, which retries and sleeps
    on anything suspicious. When that is the thing misbehaving, this bypasses it
    entirely so the actual response is visible.
    """
    import time

    import requests

    from .http import BLOCK_MARKERS, decode

    conf = load_config(args.config)

    if args.search:
        try:
            url = _search_by_name(conf, args.search, args.config).url
        except SearchNotFound as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"search {args.search!r} ->")
    else:
        url = args.url

    session = requests.Session()
    session.headers.update({
        "User-Agent": conf.scrape.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": conf.scrape.accept_language,
    })

    print(f"GET {url}")
    started = time.monotonic()
    try:
        resp = session.get(url, timeout=conf.scrape.timeout_s,
                           allow_redirects=not args.no_redirects)
    except requests.RequestException as exc:
        print(f"\nrequest failed after {time.monotonic() - started:.1f}s: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    text = decode(resp)
    print(f"\n  status        {resp.status_code}")
    print(f"  elapsed       {elapsed:.1f}s")
    print(f"  final url     {resp.url}")
    if resp.history:
        print(f"  redirects     {' -> '.join(str(r.status_code) for r in resp.history)}")
    print(f"  content-type  {resp.headers.get('Content-Type')}")
    print(f"  bytes         {len(resp.content)}")
    print(f"  encoding      header={resp.encoding!r} decoded as UTF-8-safe")

    matched = [m for m in BLOCK_MARKERS if m in text[:4000].lower()]
    print(f"  block markers {matched or 'none'}"
          + ("   <-- this is why the fetcher backs off" if matched else ""))

    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    print(f"  title         {title.group(1).strip()[:90] if title else '(none)'}")

    ads = len(re.findall(r'data-adid="', text))
    print(f"  data-adid     {ads} occurrence(s)")

    if args.save:
        Path(args.save).write_text(text, encoding="utf-8")
        print(f"\n  saved to {args.save}")
    else:
        print("\n--- first 600 characters of the body ---")
        print(text[:600])
    return 0


def cmd_probe(args) -> int:
    """Fetch one page and report what the parsers can extract from it.

    Run this first on the Pi: it is how you verify the selectors still match
    the live site without touching the database.
    """
    conf = _apply_pace(load_config(args.config), args)
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
        print("\nphotos offered by each source of this page:", file=sys.stderr)
        from .parse.detail import image_sources
        for source, count in image_sources(html, args.url or "").items():
            print(f"   {count:>4}  {source}", file=sys.stderr)
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
    scorer = scoring.Scorer(conf.scoring, scoring.load_preferences(conf.scoring.preferences_file),
                            home_plz=conf.home_plz)
    if args.show_prompt:
        print(_prompt_text(scorer, conn, row,
                           f"pass 3 for {row['id']}, and no API call was made"))
        return 0
    score = scorer.score_listing(conn, row)
    print(json.dumps(score, indent=2, ensure_ascii=False))
    if args.save:
        db.add_score(conn, row["id"], score)
        conn.commit()
        print("saved", file=sys.stderr)
    conn.close()
    return 0


def cmd_trial(args) -> int:
    """Scrape a search into a throwaway database and report what parsed.

    Runs the real fetcher, parsers, storage and image downloads, then stops:
    no scoring, no Claude API calls, no email.
    """
    conf = _apply_pace(load_config(args.config), args)

    # How many ads to take is the only scope knob: pages are walked until that
    # many are collected, or until the search runs out of them.
    limit = None if args.all_ads else (5 if args.max_ads is None else args.max_ads)
    if args.all_images:
        conf.images.max_per_listing = None

    if args.url:
        search = SearchConfig(name="trial", url=args.url, max_ads=limit)
    else:
        try:
            configured = _search_by_name(conf, args.search, args.config)
        except SearchNotFound as exc:
            print(exc, file=sys.stderr)
            return 2
        search = SearchConfig(**{**vars(configured), "max_ads": limit})

    # --make/--model override whatever the search carries; passing them with
    # --search used to be accepted and then quietly ignored.
    if args.make:
        search.make = args.make
    if args.model:
        search.model = args.model

    conf.db_path = args.db
    conf.images.dir = args.image_dir
    conf.images.enabled = not args.no_images

    if not args.keep and Path(args.db).exists():
        Path(args.db).unlink()
        for suffix in ("-wal", "-shm"):
            Path(args.db + suffix).unlink(missing_ok=True)
        print(f"starting from a clean trial database ({args.db})\n")

    conn = db.connect(conf.db_path)
    db.init_db(conn)

    scope = f"the first {limit} ad(s)" if limit is not None else "every ad in the search"
    images = ("none" if not conf.images.enabled else
              "every photo" if conf.images.max_per_listing is None else
              f"up to {conf.images.max_per_listing} photo(s) per ad")

    print(f"Trial run: {search.url}")
    print(f"  scope: {scope}; images: {images}")
    print(f"  pacing: {'testing' if _resolve_pace(args) else 'production'} - "
          f"{conf.scrape.min_delay_s:.1f}-{conf.scrape.max_delay_s:.1f}s between pages, "
          f"{conf.scrape.image_delay_range[0]:.1f}-"
          f"{conf.scrape.image_delay_range[1]:.1f}s between images")
    print("  the search pages are read first, so the plan below is a count, not a guess.\n")

    try:
        report = trial.run_trial(conf, conn, search, download_images=not args.no_images,
                                 save_pages=args.save_pages)
    except Blocked as exc:
        print(f"\nBLOCKED: {exc}", file=sys.stderr)
        print(f"Try `karpm raw \"{search.url}\"` to see the response directly.",
              file=sys.stderr)
        conn.close()
        return 1
    except Exception as exc:
        print(f"\nthe trial could not complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Try `karpm raw \"{search.url}\"` to see what the server returns.",
              file=sys.stderr)
        conn.close()
        return 1

    note = "  (capped by --limit)" if report.counts.get("capped") else ""
    print(trial.render(report, limit_note=note))

    if args.show_prompt and report.rows:
        listing_id = report.rows[0]["id"]
        row = db.get_listing(conn, listing_id)
        scorer = scoring.Scorer(conf.scoring,
                                scoring.load_preferences(conf.scoring.preferences_file),
                                home_plz=conf.home_plz)
        print("\n" + _prompt_text(
            scorer, conn, row,
            f"pass 3 for {listing_id}, and no API call was made"))

    conn.close()
    return 0 if report.ok else 1


def cmd_web(args) -> int:
    """Serve the web UI. Separate process from the daemon; they meet in the db."""
    from .web import address, create_app

    conf = load_config(args.config)
    host = "0.0.0.0" if args.lan else (args.host or conf.web.host)
    port = args.port or conf.web.port
    app = create_app(args.config)

    for line in address.describe(host, port):
        print(line)
    app.run(host=host, port=port, debug=args.debug, use_reloader=args.debug)
    return 0


def cmd_images(args) -> int:
    conf, conn = _open(args)
    saved = images.download_pending(conn, Fetcher(conf.scrape), conf.images, limit=args.limit,
                                    delay_range=conf.scrape.image_delay_range)
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


def _add_pass_flags(parser) -> None:
    """--text-only / --photos-only, shared by `extract` and `extract-one`."""
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--text-only", action="store_true",
                       help="pass 1 only: the description")
    which.add_argument("--photos-only", action="store_true",
                       help="pass 2 only: the photos")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karpm", description=__doc__)
    parser.add_argument("-c", "--config", default="config.toml",
                        help="path to the config file (default: config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging, including every URL fetched")
    pace = parser.add_mutually_exclusive_group()
    pace.add_argument("--fast", action="store_true",
                      help="testing pace: short delays between requests. Default for "
                           "trial, probe and raw.")
    pace.add_argument("--polite", action="store_true",
                      help="production pace: the delays in [scrape]. Default for scrape, "
                           "run, score, digest and daemon.")

    # The same flags after the subcommand, because that is where people type
    # them. SUPPRESS keeps an unused flag here from overwriting the global one.
    pace_parent = argparse.ArgumentParser(add_help=False)
    parent_group = pace_parent.add_mutually_exclusive_group()
    parent_group.add_argument("--fast", action="store_true", default=argparse.SUPPRESS,
                              help="testing pace: short delays between requests")
    parent_group.add_argument("--polite", action="store_true", default=argparse.SUPPRESS,
                              help="production pace: the delays configured in [scrape]")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(parents=[pace_parent], name="init", help="create the database and register searches").set_defaults(
        func=cmd_init)
    sub.add_parser(parents=[pace_parent], name="scrape", help="fetch listings once").set_defaults(func=cmd_scrape)
    p_extract = sub.add_parser(
        parents=[pace_parent], name="extract",
        help="read descriptions and photos with a model, without scoring")
    _add_pass_flags(p_extract)
    p_extract.set_defaults(func=cmd_extract)

    p_extract_one = sub.add_parser(
        parents=[pace_parent], name="extract-one",
        help="read a single listing by id")
    p_extract_one.add_argument("listing_id",
                               help="Kleinanzeigen ad id, as stored in listings.id")
    _add_pass_flags(p_extract_one)
    p_extract_one.add_argument("--show-prompt", action="store_true",
                               help="print the prompt and exit without calling the API")
    p_extract_one.add_argument("--save", action="store_true",
                               help="store what it finds; without this it is printed only")
    p_extract_one.set_defaults(func=cmd_extract_one)

    sub.add_parser(parents=[pace_parent], name="score", help="score unscored listings and send instant alerts").set_defaults(
        func=cmd_score)
    sub.add_parser(parents=[pace_parent], name="run", help="scrape, then run the AI passes and alert").set_defaults(func=cmd_run)
    sub.add_parser(parents=[pace_parent], name="daemon", help="run continuously on the configured schedule").set_defaults(
        func=cmd_daemon)
    sub.add_parser(parents=[pace_parent], name="stats", help="summarise what has been collected").set_defaults(func=cmd_stats)

    p_digest = sub.add_parser(parents=[pace_parent], name="digest", help="send the digest email")
    p_digest.add_argument("--dry-run", action="store_true", help="list what would be sent")
    p_digest.set_defaults(func=cmd_digest)

    p_raw = sub.add_parser(
        "raw", parents=[pace_parent],
        help="one request, no retries or backoff - show exactly what the server returned")
    p_raw.add_argument("url", nargs="?",
                       help="URL to fetch (omit when using --search)")
    p_raw.add_argument("--search", help="fetch the URL of this search from config.toml")
    p_raw.add_argument("--save", help="write the body here instead of printing a preview")
    p_raw.add_argument("--no-redirects", action="store_true", help="do not follow redirects")
    p_raw.set_defaults(func=cmd_raw)

    p_probe = sub.add_parser(parents=[pace_parent], name="probe", help="parse one live or saved page and dump the result")
    p_probe.add_argument("--url", help="page to fetch")
    p_probe.add_argument("--file", help="saved HTML file to parse instead")
    p_probe.add_argument("--save", help="write the fetched HTML here")
    p_probe.add_argument("--brief", action="store_true", help="omit the description")
    p_probe.add_argument("--kind", choices=("auto", "search", "detail"), default="auto",
                         help="force how the page is parsed (default: detect)")
    p_probe.set_defaults(func=cmd_probe)

    p_one = sub.add_parser(parents=[pace_parent], name="score-one", help="score a single listing by id")
    p_one.add_argument("listing_id", help="Kleinanzeigen ad id, as stored in listings.id")
    p_one.add_argument("--save", action="store_true", help="store the score")
    p_one.add_argument("--show-prompt", action="store_true", help="print the prompt, don't call the API")
    p_one.set_defaults(func=cmd_score_one)

    p_trial = sub.add_parser(
        "trial", parents=[pace_parent],
        help="dry run: scrape and parse a search into a throwaway db, no scoring or email")
    p_trial.add_argument("--url", help="search URL to try (otherwise use --search)")
    p_trial.add_argument("--search", default="trial",
                         help="name of a search from config.toml to try instead of --url")
    ads = p_trial.add_mutually_exclusive_group()
    ads.add_argument("--max-ads", type=int, default=None, dest="max_ads",
                     help="stop after this many ads, walking as many pages as that "
                          "needs (default 5)")
    ads.add_argument("--all-ads", action="store_true", dest="all_ads",
                     help="every ad in the search, to the last page")
    p_trial.add_argument("--all-images", action="store_true", dest="all_images",
                         help="every photo per ad; without it, images.max_per_listing applies")
    p_trial.add_argument("--db", default="data/trial.db", help="throwaway database path")
    p_trial.add_argument("--image-dir", default="data/trial_images",
                         help="where trial images are written (default: data/trial_images)")
    p_trial.add_argument("--make", help="make to record, as in config.toml")
    p_trial.add_argument("--model", help="model to record, as in config.toml")
    p_trial.add_argument("--no-images", action="store_true", help="skip image downloads")
    p_trial.add_argument("--keep", action="store_true",
                         help="append to the trial database instead of starting clean")
    p_trial.add_argument("--save-pages", metavar="DIR",
                         help="write every search page walked into DIR, including the one "
                              "that ends the walk")
    p_trial.add_argument("--show-prompt", action="store_true",
                         help="also print the scoring prompt for the first listing")
    p_trial.set_defaults(func=cmd_trial)

    # No pace_parent: this command makes no requests, so --fast and --polite
    # would be flags that do nothing.
    p_web = sub.add_parser("web",
                           help="serve the web UI (status, listings, config)")
    web_where = p_web.add_mutually_exclusive_group()
    web_where.add_argument("--host", help="override web.host from the config")
    web_where.add_argument("--lan", action="store_true",
                           help="bind every interface so other devices on your "
                                "network can open it; it has no login")
    p_web.add_argument("--port", type=int, help="override web.port from the config")
    p_web.add_argument("--debug", action="store_true", help="Flask debug mode and reloader")
    p_web.set_defaults(func=cmd_web)

    p_images = sub.add_parser(parents=[pace_parent], name="images", help="download images that have no local file yet")
    p_images.add_argument("--limit", type=int, default=500,
                          help="maximum images to download in one go (default: 500)")
    p_images.set_defaults(func=cmd_images)

    p_top = sub.add_parser(parents=[pace_parent], name="top", help="best-scoring active listings")
    p_top.add_argument("--min-score", type=int, default=4,
                       help="lowest overall score to show (default: 4)")
    p_top.add_argument("--limit", type=int, default=20,
                       help="maximum listings to show (default: 20)")
    p_top.set_defaults(func=cmd_top)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.command == "probe" and not (args.url or args.file):
        parser.error("probe needs --url or --file")
    if args.command == "raw":
        if not (args.url or args.search):
            parser.error("raw needs a URL or --search NAME")
        if args.url and args.search:
            parser.error("raw takes a URL or --search, not both")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
