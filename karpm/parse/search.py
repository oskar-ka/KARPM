"""Parsing a Kleinanzeigen search-results page.

Kleinanzeigen rebuilt the search page as an Astro app styled with Tailwind, so
the markup carries no semantic class names any more - the price lives in a
`<p class="my-xsmall text-title3 font-strong text-secondary">`, which will churn
with the next redesign. Two things on the page *are* stable and are what this
parser leans on:

  * `<article data-adid=... data-href=...>` - the ad id and link
  * a per-ad `<script type="application/ld+json">` ImageObject with the title,
    the description snippet and the photo URL

Everything else is found by the shape of its text - a price looks like
"1.250 € VB", a location like "80331 München", a date like "02.04.2026" or
"Gestern, 21:26" - which survives a restyle in a way that class names do not.
The legacy selectors are kept as a fallback in case an older page is served.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fields import ad_id_from_url, clean, parse_posted, parse_price

BASE = "https://www.kleinanzeigen.de"

ITEM_SELECTORS = (
    "article[data-adid]",
    "article.aditem",
    "li.ad-listitem article",
    "[data-adid]",
)

# A standalone price label, not a price mentioned inside a description.
PRICE_RE = re.compile(
    r"^(?:\d[\d.\s]*(?:,\d{2})?\s*€(?:\s*VB)?|VB|Zu verschenken|Preis auf Anfrage)$",
    re.IGNORECASE,
)
POSTCODE_RE = re.compile(r"^\d{4,5}\s+\S")
DATE_RE = re.compile(r"^(?:\d{1,2}\.\d{1,2}\.\d{4}|Heute|Gestern)\b")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_search_page(html: str, base_url: str = BASE) -> dict:
    """Return {'items': [...], 'next_url': str|None, 'selector': str|None}."""
    soup = _soup(html)
    items, used = [], None

    for selector in ITEM_SELECTORS:
        nodes = soup.select(selector)
        if not nodes:
            continue
        used = selector
        for node in nodes:
            item = _parse_item(node, base_url)
            if item:
                items.append(item)
        if items:
            break

    # The same ad can appear twice (paid top placement plus the organic hit).
    seen, unique = set(), []
    for item in items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)

    return {"items": unique, "next_url": _next_url(soup, base_url), "selector": used}


def _parse_item(node, base_url: str) -> dict | None:
    href = node.get("data-href")
    if not href:
        link = node.select_one("a[href*='/s-anzeige/']") or node.find("a", href=True)
        href = link.get("href") if link else None
    if not href:
        return None

    url = urljoin(base_url, href)
    listing_id = node.get("data-adid") or ad_id_from_url(url)
    if not listing_id:
        return None

    embedded = _embedded_json(node)
    price_text = _price_text(node)
    price, price_kind = parse_price(price_text)
    posted = parse_posted(_first_matching(node, DATE_RE))

    return {
        "id": str(listing_id),
        "url": url,
        "title": embedded.get("title") or _title(node),
        "snippet": embedded.get("description"),
        "thumbnail": embedded.get("contentUrl"),
        "price_eur": price,
        "price_kind": price_kind,
        "location": _first_matching(node, POSTCODE_RE),
        "posted_at": posted.isoformat(timespec="seconds") if posted else None,
        # "Gesuch" marks a wanted ad - someone looking to buy, not to sell.
        "is_wanted": _has_tag(node, "Gesuch"),
        # "PRO" marks a commercial seller.
        "is_commercial": _has_tag(node, "PRO"),
    }


def _embedded_json(node) -> dict:
    """Each ad carries its own ImageObject with title, snippet and photo URL."""
    tag = node.find("script", attrs={"type": "application/ld+json"})
    if not tag:
        return {}
    try:
        data = json.loads(tag.string or tag.get_text() or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "title": clean(data.get("title")),
        "description": clean(data.get("description")),
        "contentUrl": data.get("contentUrl"),
    }


def _title(node) -> str | None:
    for selector in ("h3 a", "h2 a", "a.ellipsis", "h2", "h3"):
        found = node.select_one(selector)
        if found:
            text = clean(found.get_text(" ", strip=True))
            if text:
                return text
    return None


def _price_text(node) -> str | None:
    """The asking price, ignoring a struck-through 'was' price beside it."""
    for element in node.find_all(["p", "span", "div", "strong"]):
        classes = " ".join(element.get("class") or [])
        if "line-through" in classes:
            continue
        text = clean(element.get_text(" ", strip=True))
        if text and PRICE_RE.match(text):
            return text
    return None


def _first_matching(node, pattern: re.Pattern) -> str | None:
    for raw in node.stripped_strings:
        text = clean(raw)
        if text and pattern.match(text):
            return text
    return None


def _has_tag(node, label: str) -> bool:
    return any(clean(raw) == label for raw in node.stripped_strings)


# The "next page" arrow. Matching aria-label*="eite" instead would also match
# the numbered "Seite 2" / "Seite 3" links, and on page 2 the first of those is
# "Seite 1" - which walks the pagination backwards forever.
NEXT_SELECTORS = (
    "link[rel=next]",
    "a[rel=next]",
    'a[aria-label="Nächste"]',
    "a[aria-label^='Nächste']",
    "a.pagination-next",
    ".pagination-next",
)


def _next_url(soup, base_url: str) -> str | None:
    for selector in NEXT_SELECTORS:
        node = soup.select_one(selector)
        if node and node.get("href"):
            return urljoin(base_url, node["href"])
    return None
