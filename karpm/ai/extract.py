"""Running the two extraction passes over whatever needs them."""

from __future__ import annotations

import logging

from . import passes, provider
from .. import db

log = logging.getLogger(__name__)


def run_pass(conn, kind: str, cfg, client: provider.Provider | None = None,
             still_enabled=None) -> dict:
    """Run one extraction pass over the listings due for it.

    `still_enabled` is asked before each listing, so switching a pass off part
    way through a run stops it at the next listing rather than at the end of the
    queue - every listing is an API call.
    """
    if not cfg.enabled:
        log.info("the %s pass is disabled in the config; skipping it", kind)
        return {"kind": kind, "done": 0, "skipped": "disabled"}

    # The queue is read before a provider is built: with nothing due there is
    # no reason to want an API key, and a run that only scrapes must not fail
    # for the lack of one.
    due = db.listings_needing_extraction(conn, kind, cfg.prompt_version, cfg.max_per_run)
    if not due:
        return {"kind": kind, "done": 0}

    engine = client or provider.get(cfg.provider)
    if kind == "photos" and not engine.supports_images():
        log.error("provider %r cannot read images, so the photo pass cannot run",
                  cfg.provider)
        return {"kind": kind, "done": 0, "skipped": "provider has no vision"}

    log.info("%s pass: %s listing(s) to read with %s", kind, len(due), cfg.model)
    done = failed = 0
    tokens = 0

    for row in due:
        if still_enabled is not None and not still_enabled():
            log.warning("the %s pass was switched off mid-run - stopping after %s "
                        "of %s", kind, done, len(due))
            break

        request = (passes.text_request(row, cfg) if kind == "text"
                   else passes.photo_request(conn, row, cfg))
        if request is None:
            continue                    # nothing to look at after all

        try:
            reply = engine.complete(request)
        except provider.ProviderError as exc:
            log.error("%s pass failed for %s: %s", kind, row["id"], exc)
            failed += 1
            continue
        except Exception as exc:
            log.error("%s pass failed for %s: %s", kind, row["id"], exc, exc_info=True)
            failed += 1
            continue

        data = _tidy(kind, reply.data, conn, row, cfg)
        db.save_extraction(
            conn, row["id"], kind, data,
            provider=engine.name, model=reply.model or cfg.model,
            prompt_version=cfg.prompt_version,
            source_hash=db._source_hash(conn, row, kind),
            input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
        )
        done += 1
        tokens += reply.tokens
        log.info("  %s %s (%s tokens)", kind, row["id"], reply.tokens)

    return {"kind": kind, "done": done, "failed": failed, "tokens": tokens}


def _tidy(kind: str, data: dict, conn, row, cfg) -> dict:
    """Keep a model's answer inside what the rest of the code expects.

    A shortlist naming photos that do not exist, or more than were asked for,
    would otherwise be passed to the scoring pass as if it were real.
    """
    if kind != "photos":
        return data
    have = {image["position"] for image in db.listing_images(conn, row["id"])
            if image["local_path"]}
    shortlist = [p for p in data.get("shortlist", []) if p in have]
    if len(shortlist) > cfg.shortlist:
        shortlist = shortlist[: cfg.shortlist]
    if not shortlist and have:
        # It looked at them and picked none; fall back to the first few rather
        # than sending the scoring pass no photographs at all.
        shortlist = sorted(have)[: cfg.shortlist]
    data["shortlist"] = shortlist
    return data

