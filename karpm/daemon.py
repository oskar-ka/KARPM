"""Long-running loop for the Pi.

Wall-clock scheduling rather than sleep-intervals, so a restart or a clock
correction does not shift the run times. State is kept in the database, so the
daemon does not re-run a slot it already completed after a crash.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import date, datetime, timedelta

from . import db, pipeline

log = logging.getLogger(__name__)

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


def _next_fire(times: list[tuple[int, int]], after: datetime) -> datetime:
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


def run_forever(conf, conn, poll_seconds: int = 30) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    scrape_times = _parse_times(conf.schedule.scrape_at)
    digest_times = _parse_times(conf.schedule.digest_at)
    log.info("daemon started - scraping at %s, digest at %s",
             conf.schedule.scrape_at, conf.schedule.digest_at)

    while not _stop:
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

        next_scrape = _next_fire(scrape_times, datetime.now())
        next_digest = _next_fire(digest_times, datetime.now())
        log.debug("next scrape %s, next digest %s", next_scrape, next_digest)

        for _ in range(poll_seconds):
            if _stop:
                break
            time.sleep(1)

    log.info("daemon stopped")
