"""Long-running loop for the Pi.

Wall-clock scheduling rather than sleep-intervals, so a restart or a clock
correction does not shift the run times. State is kept in the database, so the
daemon does not re-run a slot it already completed after a crash.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from datetime import date, datetime, timedelta

from . import db, pipeline
from .config import load_config

log = logging.getLogger(__name__)

# How often the heartbeat is written. The web UI calls the daemon dead after
# web.HEARTBEAT_STALE_AFTER, so this has to be comfortably shorter than that.
HEARTBEAT_EVERY_S = 30

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("received signal %s, finishing current step then exiting", signum)
    _stop = True


def _parse_times(values: list[str]) -> list[tuple[int, int]]:
    times = []
    for value in values:
        hour, _, minute = value.partition(":")
        times.append((int(hour), int(minute or 0)))
    return sorted(times)


def _next_fire(times: list[tuple[int, int]], after: datetime) -> datetime | None:
    """When this fires next, or None if it is not scheduled at all.

    An empty list is a real setting - it means the daemon only acts on what the
    web UI queues - so it must not be an IndexError in the middle of the loop.
    """
    if not times:
        return None
    for hour, minute in times:
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > after:
            return candidate
    hour, minute = times[0]
    tomorrow = after.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time()).replace(hour=hour, minute=minute)


def _ran_today(conn, kind: str, slot: datetime) -> bool:
    """Did we already run this kind at or after today's slot time?"""
    row = conn.execute(
        "SELECT started_at FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
    ).fetchone()
    if not row or not row["started_at"]:
        return False
    try:
        last = datetime.fromisoformat(row["started_at"])
    except ValueError:
        return False
    return last.replace(tzinfo=None) >= slot


def run_command(conf, conn, row) -> tuple[bool, str]:
    """Carry out one thing the web UI asked for."""
    params = json.loads(row["params_json"] or "{}")
    command = row["command"]
    if command == "scrape":
        return True, json.dumps(pipeline.run_once(conf, conn))
    if command == "digest":
        provider_id = pipeline.run_digest(conf, conn)
        return True, f"sent: {provider_id}" if provider_id else "nothing new to send"
    if command == "rescore":
        if params.get("all"):
            # Forget the old verdicts so every listing is scored again.
            conn.execute("DELETE FROM scores")
            conn.commit()
        result = pipeline.run_scoring_and_alerts(conf, conn)
        return True, json.dumps(result)
    return False, f"unknown command {command!r}"


def _handle_pending_command(conf, conn) -> bool:
    """Run one queued command, if there is one. True if something ran."""
    row = db.claim_command(conn)
    if row is None:
        return False
    log.info("running queued command %s (#%s)", row["command"], row["id"])
    try:
        ok, result = run_command(conf, conn, row)
    except Exception as exc:
        log.exception("queued command %s failed", row["command"])
        db.finish_command(conn, row["id"], False, f"{type(exc).__name__}: {exc}")
        return True
    db.finish_command(conn, row["id"], ok, result)
    log.info("command %s finished: %s", row["command"], result[:200])
    return True


def _beat(db_path, stop: threading.Event, every: int = HEARTBEAT_EVERY_S) -> None:
    """Write the heartbeat on its own connection until asked to stop.

    It is a thread rather than a line in the main loop because a scrape or a
    scoring run holds that loop for an hour at a time, and a heartbeat that
    stops whenever the daemon is busiest would tell the web UI it had died
    exactly when it was working hardest.
    """
    conn = db.connect(db_path)
    try:
        while True:
            try:
                db.set_state(conn, "heartbeat", db.utcnow())
            except Exception:               # a locked database is not fatal here
                log.debug("heartbeat write failed", exc_info=True)
            if stop.wait(every):
                return
    finally:
        conn.close()


def run_forever(conf, conn, poll_seconds: int = 30, config_path: str | None = None) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # A command still marked running means we died holding it.
    stale = db.reset_stale_commands(conn)
    if stale:
        log.warning("marked %s interrupted command(s) as failed", stale)

    log.info("daemon started - scraping at %s, digest at %s",
             conf.schedule.scrape_at, conf.schedule.digest_at)

    stop_beating = threading.Event()
    beat = threading.Thread(target=_beat, args=(conf.db_path, stop_beating),
                            name="karpm-heartbeat", daemon=True)
    beat.start()

    while not _stop:
        # Reread the config each cycle so edits made in the web UI take effect
        # without a restart.
        if config_path:
            try:
                conf = load_config(config_path)
            except Exception as exc:
                log.error("could not reload %s, keeping the previous config: %s",
                          config_path, exc)

        scrape_times = _parse_times(conf.schedule.scrape_at)
        digest_times = _parse_times(conf.schedule.digest_at)

        if _handle_pending_command(conf, conn):
            continue                      # look for the next one straight away

        if db.is_paused(conn):
            _sleep(poll_seconds)
            continue

        now = datetime.now()
        today = date.today()

        for hour, minute in scrape_times:
            slot = datetime.combine(today, datetime.min.time()).replace(hour=hour, minute=minute)
            if now >= slot and not _ran_today(conn, "scrape", slot):
                log.info("scrape slot %02d:%02d", hour, minute)
                try:
                    result = pipeline.run_once(conf, conn)
                    log.info("scrape finished: %s", result)
                except Exception:
                    log.exception("scrape run failed")
                break

        for hour, minute in digest_times:
            slot = datetime.combine(today, datetime.min.time()).replace(hour=hour, minute=minute)
            if now >= slot and not _ran_today(conn, "digest", slot):
                log.info("digest slot %02d:%02d", hour, minute)
                try:
                    pipeline.run_digest(conf, conn)
                except Exception:
                    log.exception("digest failed")
                break

        for key, times in (("next_scrape", scrape_times), ("next_digest", digest_times)):
            when = _next_fire(times, datetime.now())
            db.set_state(conn, key, when.isoformat() if when else "not scheduled")
        _sleep(poll_seconds)

    stop_beating.set()
    beat.join(timeout=5)
    # One last beat, so the UI shows when it stopped rather than a stale time.
    db.set_state(conn, "heartbeat", db.utcnow())
    log.info("daemon stopped")


def _sleep(seconds: int) -> None:
    """Sleep in one-second steps so a signal is noticed promptly."""
    for _ in range(seconds):
        if _stop:
            return
        time.sleep(1)
