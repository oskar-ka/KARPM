"""Parsing a single Kleinanzeigen ad page.

Extraction runs in layers, best source first:
  1. JSON-LD (schema.org Product/Offer) - stable, machine-readable when present
  2. Known CSS selectors for the ad page
  3. Open Graph / meta tags
  4. Regex over the raw HTML
Whatever a layer fills, later layers leave alone. Fields nothing could fill are
recorded in parse_warnings so a markup change shows up in the data instead of
quietly becoming NULL.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .fields import (
    ad_id_from_url,
    apply_attributes,
    clean,
    parse_location,
    parse_posted,
    parse_price,
    parse_seller_type,
    slug,
)

BASE = "https://www.kleinanzeigen.de"

TITLE_SELECTORS = ("#viewad-title", "h1[itemprop=name]", "h1.boxedarticle--title", "h1")
PRICE_SELECTORS = ("#viewad-price", "h2[itemprop=price]", ".boxedarticle--price",
                   "[class*='boxedarticle--price']")
DESC_SELECTORS = ("#viewad-description-text", "[itemprop=description]",
                  ".viewad-description-text", "#viewad-description")
LOCALITY_SELECTORS = ("#viewad-locality", "#street-address", "[itemprop=address]",
                      ".viewad-locality")
DATE_SELECTORS = ("#viewad-extra-info span", ".viewad-extra-info span", "#viewad-details span")
ATTR_ROW_SELECTORS = ("li.addetailslist--detail", ".addetailslist--detail",
                      "#viewad-details li", ".attributelist--item")
TAG_SELECTORS = (".checktag", ".splitlinebox li", ".addetailslist--detail--tags span")
IMAGE_SELECTORS = ("#viewad-image", ".galleryimage-element img", "#viewad-product img",
                   ".ad-image img", "[data-imgsrc]")
SELLER_SELECTORS = (".userprofile-vip a", "#viewad-contact .iconlist-text a",
                    ".userprofile-vip-details-text", "#viewad-contact-box .text-body-regular")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _first_text(soup, selectors) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean(node.get_text(" ", strip=True))
            if text:
                return text
    return None


def _jsonld_with_tags(soup):
    """JSON-LD blocks paired with the script tag they came from, so callers can
    tell where in the page a block sits."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for block in (data if isinstance(data, list) else [data]):
            if isinstance(block, dict):
                yield block, tag


def _jsonld(soup) -> list[dict]:
    blocks = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        blocks.extend(data if isinstance(data, list) else [data])
    return [b for b in blocks if isinstance(b, dict)]


def parse_detail_page(html: str, url: str | None = None) -> dict:
    """Parse an ad page into a dict matching the `listings` table columns."""
    soup = _soup(html)
    out: dict = {"url": url}
    warnings: list[str] = []
    raw_attrs: dict[str, str] = {}
    seeded_images: list[str] = []

    # --- layer 1: JSON-LD ---
    for block in _jsonld(soup):
        if block.get("@type") in ("Product", "Offer", "Vehicle", "Motorcycle", "Car"):
            out.setdefault("title", clean(block.get("name")))
            out.setdefault("description", clean(block.get("description")))
            offers = block.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict) and offers.get("price") is not None:
                try:
                    out["price_eur"] = int(float(offers["price"]))
                    out["price_kind"] = "fixed"
                except (TypeError, ValueError):
                    pass
            images = block.get("image")
            if images:
                # One more source, not the answer: a Product block often names
                # a single image, and taking it as the gallery used to skip the
                # gallery entirely and store one photo per ad.
                seeded_images.extend([images] if isinstance(images, str) else list(images))
            if block.get("mileageFromOdometer"):
                mileage = block["mileageFromOdometer"]
                raw_attrs["Kilometerstand"] = str(
                    mileage.get("value") if isinstance(mileage, dict) else mileage
                )
            if block.get("vehicleModelDate"):
                raw_attrs["Baujahr"] = str(block["vehicleModelDate"])
            if block.get("brand"):
                brand = block["brand"]
                raw_attrs["Marke"] = str(brand.get("name") if isinstance(brand, dict) else brand)

    # --- layer 2: CSS selectors ---
    if not out.get("title"):
        out["title"] = _first_text(soup, TITLE_SELECTORS)
    if not out.get("description"):
        node = next((soup.select_one(s) for s in DESC_SELECTORS if soup.select_one(s)), None)
        if node:
            out["description"] = clean(node.get_text("\n", strip=True))
    # The visible price is what the buyer sees, and it is the only place "VB"
    # (Verhandlungsbasis - negotiable) appears; JSON-LD gives a bare number.
    visible_price, visible_kind = parse_price(_first_text(soup, PRICE_SELECTORS))
    if visible_price is not None:
        out["price_eur"], out["price_kind"] = visible_price, visible_kind
    elif out.get("price_eur") is not None:
        if visible_kind == "vb":
            out["price_kind"] = "vb"
    else:
        out["price_eur"], out["price_kind"] = None, visible_kind

    raw_attrs.update(_attributes(soup))
    for tag in _tags(soup, raw_attrs):
        raw_attrs.setdefault(tag, "ja")

    postcode, location = parse_location(_first_text(soup, LOCALITY_SELECTORS))
    out["postcode"], out["location"] = postcode, location

    posted = None
    for node in soup.select(", ".join(DATE_SELECTORS)):
        posted = parse_posted(clean(node.get_text()))
        if posted:
            break
    out["posted_at"] = posted.isoformat(timespec="seconds") if posted else None

    seller_text = _first_text(soup, SELLER_SELECTORS)
    out["seller_name"] = seller_text
    out["seller_type"] = parse_seller_type(soup.get_text(" ", strip=True)[:6000])
    out["seller_id"] = _seller_id(soup)
    out["view_count"] = _view_count(soup)

    # Always read the gallery; whatever JSON-LD offered is merged into it.
    out["image_urls"] = _images(soup, url or BASE, seeded_images)

    # --- layer 3: meta tags ---
    if not out.get("title"):
        meta = soup.select_one("meta[property='og:title'], meta[name='title']")
        out["title"] = clean(meta.get("content")) if meta else None
    if not out.get("description"):
        meta = soup.select_one("meta[property='og:description'], meta[name='description']")
        out["description"] = clean(meta.get("content")) if meta else None
    if not out.get("image_urls"):
        out["image_urls"] = [
            m["content"] for m in soup.select("meta[property='og:image'][content]")
        ]

    # --- layer 4: identity and typed fields ---
    out["id"] = _ad_id(soup, url)
    fields, attr_warnings = apply_attributes(raw_attrs)
    out.update(fields)
    out["attributes_json"] = raw_attrs
    warnings.extend(attr_warnings)

    for required in ("title", "price_eur", "description"):
        if out.get(required) in (None, ""):
            warnings.append(f"missing:{required}")
    if not out.get("id"):
        warnings.append("missing:id")
    out["parse_warnings"] = warnings
    return out


def _attributes(soup) -> dict[str, str]:
    """The 'Details' list: label/value pairs such as Kilometerstand / 12.345 km."""
    attrs: dict[str, str] = {}
    for selector in ATTR_ROW_SELECTORS:
        for row in soup.select(selector):
            value_node = row.select_one(
                ".addetailslist--detail--value, .attributelist--value, span:last-child"
            )
            if value_node is None:
                continue
            value = clean(value_node.get_text(" ", strip=True))
            full = clean(row.get_text(" ", strip=True)) or ""
            label = clean(full[: len(full) - len(value)]) if value and full.endswith(value) else None
            if not label:
                label_node = row.select_one(
                    ".addetailslist--detail--label, .attributelist--key, span:first-child"
                )
                label = clean(label_node.get_text()) if label_node else None
            if label and value and slug(label) != slug(value):
                attrs.setdefault(label.rstrip(":"), value)
        if attrs:
            break
    return attrs


def _tags(soup, known: dict[str, str]) -> list[str]:
    """Boolean features rendered as chips (Scheckheftgepflegt, ABS, ...).

    Some of these selectors also match the label/value rows of the details
    list, which would re-enter every attribute as a junk boolean such as
    {"Art Motorräder": "ja"} and duplicate the whole block into the scoring
    prompt. Anything that reproduces a pair we already captured is dropped.
    """
    already = {slug(label) for label in known}
    already |= {slug(f"{label}{value}") for label, value in known.items()}

    tags = []
    for selector in TAG_SELECTORS:
        for node in soup.select(selector):
            text = clean(node.get_text(" ", strip=True))
            if not text or len(text) >= 60 or slug(text) in already:
                continue
            tags.append(text)
        if tags:
            break
    return tags


def _images(soup, base_url: str, seeded: list[str] | None = None) -> list[str]:
    """Every photo of this ad, from both places the page lists them.

    Two rules matter here. Collect from *all* the selectors rather than stopping
    at the first that matches: where "#viewad-image" is only the main photo and
    the rest of the gallery sits under different markup, stopping early yields
    exactly one image per ad. And ignore anything inside an article[data-adid],
    because those are the "similar ads" cards showing other people's bikes.

    The page's own JSON-LD lists the gallery as ImageObject blocks. Those are
    server-rendered and come first, so they win when the same photo appears
    twice under different renditions.
    """
    urls: list[str] = []
    seen_photos: set[str] = set()

    def add(raw_url: str | None) -> None:
        if not raw_url or raw_url.startswith("data:"):
            return
        full = urljoin(base_url, raw_url)
        # The same photo is offered under several "rule" renditions; key on the
        # path so it is not collected once per rendition.
        photo = full.partition("?")[0]
        if photo in seen_photos:
            return
        seen_photos.add(photo)
        urls.append(full)

    for seed in seeded or ():
        add(seed)

    for block, tag in _jsonld_with_tags(soup):
        if block.get("@type") != "ImageObject":
            continue
        if tag.find_parent("article", attrs={"data-adid": True}) is not None:
            continue                        # a similar-ads card, not this ad
        add(block.get("contentUrl"))

    for selector in IMAGE_SELECTORS:
        for node in soup.select(selector):
            if node.find_parent("article", attrs={"data-adid": True}) is not None:
                continue
            add(node.get("src") or node.get("data-imgsrc") or node.get("data-src")
                or node.get("content"))

    # The old CDN encoded the size in the filename; harmless on current URLs.
    return [re.sub(r"_(\d+)\.(jpg|jpeg|png|webp)$", r"_57.\2", u) for u in urls]


def _ad_id(soup, url: str | None) -> str | None:
    listing_id = ad_id_from_url(url)
    if listing_id:
        return listing_id
    node = soup.select_one("#viewad-ad-id-box, [data-adid]")
    if node:
        if node.get("data-adid"):
            return str(node["data-adid"])
        m = re.search(r"\d{6,}", node.get_text())
        if m:
            return m.group(0)
    canonical = soup.select_one("link[rel=canonical][href]")
    return ad_id_from_url(canonical["href"]) if canonical else None


def _seller_id(soup) -> str | None:
    link = soup.select_one("a[href*='/s-bestandsliste.html?userId=']")
    if link:
        m = re.search(r"userId=(\d+)", link["href"])
        if m:
            return m.group(1)
    return None


def _view_count(soup) -> int | None:
    node = soup.select_one("#viewad-cntr-num, .viewad-cntr-num")
    if not node:
        return None
    m = re.search(r"\d[\d.]*", node.get_text())
    return int(m.group(0).replace(".", "")) if m else None


# Wording Kleinanzeigen uses when an ad no longer exists. These are only
# consulted once the page has failed to parse as an ad - sellers write things
# like "das Zubehör ist nicht mehr verfügbar" in perfectly live listings, and
# matching on the raw text alone would delete them.
GONE_MARKERS = (
    "anzeige ist nicht mehr verfügbar",
    "anzeige wurde gelöscht",
    "anzeige ist leider nicht mehr",
    "anzeige nicht gefunden",
    "anzeige existiert nicht",
    "diese anzeige wurde beendet",
    "ad is no longer available",
)

LIVE = "live"
GONE = "gone"
UNKNOWN = "unknown"


def classify_ad_page(html: str, final_url: str | None = None,
                     expected_id: str | None = None) -> str:
    """Decide whether an ad page shows a live listing, a removed one, or
    something we cannot read.

    Returns "live", "gone" or "unknown". "unknown" is the safe answer: callers
    must not delist on it. Order matters - a page that parses as a real advert
    is live no matter what phrases appear in the seller's own text.
    """
    data = parse_detail_page(html, final_url)
    parsed_as_ad = bool(data.get("title")) and (
        data.get("price_eur") is not None or data.get("description")
    )

    if parsed_as_ad:
        # A redirect to a different ad would be someone else's listing.
        if expected_id and data.get("id") and data["id"] != expected_id:
            return UNKNOWN
        return LIVE

    # Not an advert. Were we bounced somewhere else entirely?
    if final_url and "/s-anzeige/" not in final_url:
        return GONE

    lowered = html.lower()
    if any(marker in lowered for marker in GONE_MARKERS):
        return GONE

    return UNKNOWN
