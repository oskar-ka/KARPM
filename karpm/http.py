"""Polite HTTP client.

One Raspberry Pi fetching a few hundred pages twice a day should look like a
person browsing. The defaults here are slow on purpose; every request is
spaced by a randomised delay, retries back off, and repeated blocks abort the
run rather than hammering on.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import NamedTuple

import requests

log = logging.getLogger(__name__)

CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)


def decode(resp: requests.Response) -> str:
    """Decode a response body using the charset the document declares.

    requests falls back to ISO-8859-1 for any text/* response that arrives
    without a charset in the Content-Type header (RFC 2616). Kleinanzeigen
    serves UTF-8 and declares it in a meta tag, so that fallback turns every
    umlaut into mojibake - "Bremsklötze" becomes "BremsklÃ¶tze" - which then
    poisons the description text in the database and in the scoring prompt.
    """
    declared = (resp.encoding or "").lower()
    if declared and declared not in ("iso-8859-1", "latin-1", "latin1", "ascii"):
        return resp.text

    match = CHARSET_RE.search(resp.content[:4096])
    encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        return resp.content.decode(encoding, errors="replace")
    except LookupError:
        return resp.content.decode("utf-8", errors="replace")


class Blocked(RuntimeError):
    """Raised when the site is clearly refusing us (403 / captcha wall)."""


class Page(NamedTuple):
    """A fetched page, plus where we actually landed after redirects."""

    content: str | bytes
    url: str
    status: int


class Fetcher:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.consecutive_blocks = 0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": cfg.accept_language,
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self._last_request = 0.0

    def _wait(self, delay_range: tuple[float, float] | None = None) -> None:
        low, high = delay_range or (self.cfg.min_delay_s, self.cfg.max_delay_s)
        delay = random.uniform(low, high)
        elapsed = time.monotonic() - self._last_request
        if elapsed < delay:
            log.debug("waiting %.1fs before next request", delay - elapsed)
            time.sleep(delay - elapsed)

    def _dump(self, text: str, url: str) -> str:
        """Save a page we refused to trust, so it can actually be looked at."""
        try:
            target = Path(self.cfg.dump_dir)
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"blocked_{time.strftime('%Y%m%d_%H%M%S')}.html"
            path.write_text(f"<!-- {url} -->\n{text}", encoding="utf-8")
            return str(path)
        except OSError as exc:
            return f"(could not save: {exc})"

    def get(self, url: str, *, referer: str | None = None, binary: bool = False,
            delay_range: tuple[float, float] | None = None):
        """Fetch a URL, returning text (or bytes). Raises on permanent failure."""
        page = self.fetch(url, referer=referer, binary=binary, delay_range=delay_range)
        return page.content

    def fetch(self, url: str, *, referer: str | None = None, binary: bool = False,
              delay_range: tuple[float, float] | None = None) -> Page:
        """Like get(), but also reports the URL we ended up at.

        Kleinanzeigen answers a removed ad by redirecting to the category page
        rather than serving a 404, so the final URL is part of the evidence
        about whether a listing still exists.
        """
        headers = {"Referer": referer} if referer else {}
        if binary:
            # The image CDN's ".AUTO" renditions pick a format from Accept, so
            # asking for them with an HTML Accept header is asking the wrong
            # question.
            headers["Accept"] = "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8"
        last_error: Exception | None = None
        saw_block = False

        for attempt in range(self.cfg.max_retries + 1):
            self._wait(delay_range)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(
                    url, headers=headers, timeout=self.cfg.timeout_s, allow_redirects=True
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("request failed (%s/%s) %s: %s",
                            attempt + 1, self.cfg.max_retries + 1, url, exc)
                time.sleep(2 ** attempt * 5)
                continue

            if resp.status_code == 200:
                if binary:
                    self.consecutive_blocks = 0
                    return Page(resp.content, resp.url, 200)
                text = decode(resp)
                matched = _block_markers(text)
                if matched:
                    saw_block = True
                    dump = self._dump(text, url)
                    self._register_block(url)
                    wait = self.cfg.block_backoff_s * (self.consecutive_blocks ** 2)
                    log.warning(
                        "%s returned 200 but looks like a block page (matched %s); "
                        "sleeping %ss before retry %s/%s. Body saved to %s",
                        url, ", ".join(matched), wait, attempt + 1,
                        self.cfg.max_retries + 1, dump,
                    )
                    time.sleep(wait)
                    continue
                self.consecutive_blocks = 0
                return Page(text, resp.url, 200)

            if resp.status_code in (403, 429, 503):
                saw_block = True
                self._register_block(url)
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if (retry_after or "").isdigit() else 60 * (attempt + 1) ** 2
                log.warning("blocked (%s) on %s, sleeping %ss", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                raise FileNotFoundError(f"404 for {url}")

            last_error = RuntimeError(f"HTTP {resp.status_code} for {url}")
            wait = 2 ** attempt * 5
            log.warning("HTTP %s for %s; retrying in %ss (%s/%s)",
                        resp.status_code, url, wait, attempt + 1, self.cfg.max_retries + 1)
            time.sleep(wait)

        if saw_block:
            # Exhausting the retries on block pages is a block, whatever the
            # consecutive counter says - surface it as one so callers can stop
            # cleanly instead of dying on a generic error.
            raise Blocked(
                f"{url} kept returning a block page. Kleinanzeigen is refusing this "
                f"client. Saved copies are in {self.cfg.dump_dir}/ - open one to see "
                f"what it is. Wait a few hours, and consider raising "
                f"scrape.min_delay_s / max_delay_s."
            )
        raise last_error or RuntimeError(f"gave up on {url}")

    def _register_block(self, url: str) -> None:
        self.consecutive_blocks += 1
        if self.consecutive_blocks >= self.cfg.block_threshold:
            raise Blocked(
                f"blocked {self.consecutive_blocks} times in a row (last: {url}). "
                "Stopping this run - increase scrape delays or wait a few hours."
            )


BLOCK_MARKERS = ("captcha", "are you a robot", "sicherheitsabfrage", "zugriff verweigert",
                 "unusual traffic", "ungewöhnliche aktivität")


def _block_markers(html: str) -> list[str]:
    """Markers suggesting a 200 response is really a captcha or block page.

    Returns which ones matched, so a refusal to trust a page is explainable
    rather than a mystery.
    """
    lowered = html[:4000].lower()
    return [m for m in BLOCK_MARKERS if m in lowered]


def _looks_blocked(html: str) -> bool:
    return bool(_block_markers(html))
