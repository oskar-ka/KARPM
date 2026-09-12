"""Parsing a Kleinanzeigen search-results page.

Selectors are layered: we try several known shapes and fall back to generic
attribute probing, so a cosmetic markup change does not silently return zero
results. `karpm probe` reports which layer fired.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fields import ad_id_from_url, clean, parse_price

BASE = "https://www.kleinanzeigen.de"

ITEM_SELECTORS = (
    "article.aditem",
    "li.ad-listitem article",
    "[data-adid]",
    "article[data-href]",
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_search_page(html: str, base_url: str = BASE) -> dict:
    """Return {'items': [...], 'next_url': str|None, 'selector': str|None}."""
    soup = _soup(html)
    items, used = [], None

    for selector in ITEM_SELECTORS:
        nodes = soup.select(selector)
        if nodes:
            used = selector
            for node in nodes:
                item = _parse_item(node, base_url)
                if item:
                    items.append(item)
            if items:
                break

    # Deduplicate: the same ad can appear twice (top-placement + organic).
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

    title_node = node.select_one("a.ellipsis, h2 a, .text-module-begin a, h2")
    price_node = node.select_one(
        ".aditem-main--middle--price-shipping--price, .aditem-main--middle--price, "
        "p.aditem-main--middle--price-shipping--price, [class*='--price']"
    )
    price, price_kind = parse_price(clean(price_node.get_text()) if price_node else None)

    return {
        "id": str(listing_id),
        "url": url,
        "title": clean(title_node.get_text()) if title_node else None,
        "price_eur": price,
        "price_kind": price_kind,
    }


def _next_url(soup, base_url: str) -> str | None:
    link = soup.select_one("link[rel=next]")
    if link and link.get("href"):
        return urljoin(base_url, link["href"])
    for selector in ("a.pagination-next", ".pagination-next", "a[aria-label*='eite']"):
        node = soup.select_one(selector)
        if node and node.get("href"):
            return urljoin(base_url, node["href"])
    return None
