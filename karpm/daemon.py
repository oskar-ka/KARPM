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
from pathlib import Path

from . import db, pipeline
from .config import load_config

log = logging.getLogger(__name__)

# A floor of one second, only because zero would spin. Anything above that is
# your call: a one-second heartbeat is noisy, and that is a fine way to watch
# what the daemon is doing while you set it up.
MIN_HEARTBEAT_S = 1
MAX_HEARTBEAT_S = 3600

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


def run_command(conf, conn, row, config_path=None) -> tuple[bool, str]:
    """Carry out one thing the web UI asked for."""
    params = json.loads(row["params_json"] or "{}")
    command = row["command"]
    if command == "scrape":
        return True, json.dumps(pipeline.run_once(conf, conn, config_path))
    if command == "digest":
        provider_id = pipeline.run_digest(conf, conn)
        return True, f"sent: {provider_id}" if provider_id else "nothing new to send"
    if command == "reload":
        # The daemon re-reads the config every cycle and every heartbeat anyway.
        # What this adds is proof: it reports back what it now sees, so a
        # setting that appears not to have applied can be checked rather than
        # guessed at.
        if not config_path:
            return False, "this daemon was not started from a config file"
        fresh = load_config(config_path)
        return True, (f"re-read {Path(config_path).resolve()}: "
                      f"scraping at {fresh.schedule.scrape_at or 'never'}, "
                      f"digest at {fresh.schedule.digest_at or 'never'}, "
                      f"scoring {'on' if fresh.scoring.enabled else 'off'}, "
                      f"heartbeat every {heartbeat_seconds(fresh)}s, "
                      f"{len([s for s in fresh.searches if s.enabled])} search(es) enabled")
    if command == "rescore":
        # Checked before anything is deleted. Wiping the verdicts and then
        # finding scoring switched off would destroy what cannot be rebuilt.
        if not conf.scoring.enabled:
            return False, ("scoring is disabled in the config, so nothing was "
                           "scored and no existing scores were touched")
        if params.get("all"):
            # Forget the old verdicts so every listing is scored again.
            conn.execute("DELETE FROM scores")
            conn.commit()
        result = pipeline.run_scoring_and_alerts(conf, conn, config_path)
        return True, json.dumps(result)
    return False, f"unknown command {command!r}"


def _handle_pending_command(conf, conn, config_path=None) -> bool:
    """Run one queued command, if there is one. True if something ran."""
    row = db.claim_command(conn)
    if row is None:
        return False
    log.info("running queued command %s (#%s)", row["command"], row["id"])
    try:
        ok, result = run_command(conf, conn, row, config_path)
    except Exception as exc:
        log.exception("queued command %s failed", row["command"])
        db.finish_command(conn, row["id"], False, f"{type(exc).__name__}: {exc}")
        return True
    db.finish_command(conn, row["id"], ok, result)
    log.info("command %s finished: %s", row["command"], result[:200])
    return True


def _publish_schedule(conn, conf, scrape_times, digest_times, score_times) -> None:
    """Put what the heartbeat reports where it can read it.

    The heartbeat runs on its own thread with its own connection, so app_state
    is how it learns what the schedule now says.
    """
    for key, times in (("next_scrape", scrape_times), ("next_digest", digest_times),
                       ("next_score", score_times)):
        when = _next_fire(times, datetime.now())
        db.set_state(conn, key, when.isoformat() if when else "not scheduled")
    db.set_state(conn, "scoring", "on" if conf.scoring.enabled else "off")


def heartbeat_seconds(conf) -> int:
    """The configured interval, held to something a daemon can honour."""
    return max(MIN_HEARTBEAT_S, min(int(conf.schedule.heartbeat_s), MAX_HEARTBEAT_S))


def _beat(db_path, stop: threading.Event, every: int, config_path=None) -> None:
    """Record and announce that the daemon is alive, until asked to stop.

    It is a thread rather than a line in the main loop because a scrape or a
    scoring run holds that loop for an hour at a time, and a heartbeat that
    stopped whenever the daemon was busiest would say it had died exactly when
    it was working hardest. It reads what to report from app_state, which the
    main loop keeps up to date.

    The config is re-read on every beat, so changing heartbeat_s takes effect on
    the next one rather than at the next restart.
    """
    conn = db.connect(db_path)
    try:
        while True:
            try:
                db.set_state(conn, "heartbeat", db.utcnow())
                log.info("%s", status_line(conn))
            except Exception:               # a locked database is not fatal here
                log.debug("heartbeat failed", exc_info=True)

            if config_path:
                try:
                    every = heartbeat_seconds(load_config(config_path))
                except Exception:           # a half-saved file is not a new setting
                    log.debug("could not re-read %s this beat", config_path,
                              exc_info=True)
            if stop.wait(every):
                return
    finally:
        conn.close()


def _when(value: str | None) -> str:
    """A stored next-fire time as something worth reading in a log line."""
    if not value or value == "not scheduled":
        return "never"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    today = date.today()
    stamp = moment.strftime("%H:%M")
    if moment.date() == today:
        return stamp
    if (moment.date() - today).days == 1:
        return f"{stamp} tomorrow"
    return moment.strftime("%d %b %H:%M")


def status_line(conn) -> str:
    """One line saying the daemon is alive and what it is waiting for."""
    parts = [f"next scrape {_when(db.get_state(conn, 'next_scrape'))}",
             f"digest {_when(db.get_state(conn, 'next_digest'))}"]
    if db.get_state(conn, "scoring") == "off":
        parts.append("scoring off")
    elif db.get_state(conn, "next_score") in (None, "not scheduled"):
        parts.append("score with each scrape")
    else:
        parts.append(f"score {_when(db.get_state(conn, 'next_score'))}")

    counts = db.pending_counts(conn)
    waiting = [f"{counts[key]} to {label}"
               for key, label in (("refetch", "re-fetch"), ("rescore", "re-score"))
               if counts[key]]
    queued = len([c for c in db.recent_commands(conn, 20) if c["status"] == "pending"])
    if queued:
        waiting.append(f"{queued} queued command(s)")
    if db.is_paused(conn):
        waiting.append("SCHEDULE PAUSED")
    if waiting:
        parts.append("; ".join(waiting))
    return "heartbeat - " + ", ".join(parts)


def _fire_due(conn, kind: str, times: list[tuple[int, int]], action) -> bool:
    """Run `action` if a slot for today has passed and has not been run yet.

    An empty `times` is not an error and not a special case: there is simply no
    slot to be due, so the daemon carries on doing everything else.
    """
    today = date.today()
    now = datetime.now()
    for hour, minute in times:
        slot = datetime.combine(today, datetime.min.time()).replace(hour=hour, minute=minute)
        if now < slot or _ran_today(conn, kind, slot):
            continue
        log.info("%s slot %02d:%02d", kind, hour, minute)
        try:
            result = action()
            log.info("%s finished: %s", kind, result)
        except Exception:
            log.exception("%s run failed", kind)
        return True
    return False


def run_forever(conf, conn, poll_seconds: int = 30, config_path: str | None = None) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # A command still marked running means we died holding it.
    stale = db.reset_stale_commands(conn)
    if stale:
        log.warning("marked %s interrupted command(s) as failed", stale)

    log.info("daemon started - scraping at %s, digest at %s, scoring %s, "
             "heartbeat every %ss",
             conf.schedule.scrape_at or "never",
             conf.schedule.digest_at or "never",
             "off" if not conf.scoring.enabled
             else conf.schedule.score_at or "with each scrape",
             heartbeat_seconds(conf))

    # Published before the first beat, or the first line of a fresh start would
    # say "next scrape never" and send someone looking for a fault.
    _publish_schedule(conn, conf, _parse_times(conf.schedule.scrape_at),
                      _parse_times(conf.schedule.digest_at),
                      _parse_times(conf.schedule.score_at))

    every = heartbeat_seconds(conf)
    stop_beating = threading.Event()
    beat = threading.Thread(target=_beat,
                            args=(conf.db_path, stop_beating, every, config_path),
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
        # Empty: scoring rides along with each scrape instead of having slots.
        score_times = _parse_times(conf.schedule.score_at)

        if _handle_pending_command(conf, conn, config_path):
            continue                      # look for the next one straight away

        if db.is_paused(conn):
            _sleep(poll_seconds, conn)
            continue

        _fire_due(conn, "scrape", scrape_times,
                  lambda: pipeline.run_once(conf, conn, config_path))
        _fire_due(conn, "digest", digest_times, lambda: pipeline.run_digest(conf, conn))
        if conf.scoring.enabled:
            _fire_due(conn, "score", score_times,
                      lambda: pipeline.run_scoring_and_alerts(conf, conn, config_path))

        _publish_schedule(conn, conf, scrape_times, digest_times, score_times)
        # Never doze longer than a heartbeat: a short one is someone watching,
        # and a queued command should not outlast the interval they chose.
        _sleep(min(poll_seconds, heartbeat_seconds(conf)), conn)

    stop_beating.set()
    beat.join(timeout=5)
    # One last beat, so the UI shows when it stopped rather than a stale time.
    db.set_state(conn, "heartbeat", db.utcnow())
    log.info("daemon stopped")


def _sleep(seconds: int, conn=None) -> None:
    """Sleep in one-second steps, waking early for a signal or a button.

    Without the `conn` check a queued command waits out the whole interval, so
    "scrape now" meant "scrape within the next half hour" - which is not what
    any of those buttons say.
    """
    for _ in range(seconds):
        if _stop:
            return
        time.sleep(1)
        if conn is not None and db.has_pending_command(conn):
            return
