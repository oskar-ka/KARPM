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

from . import syndicated
from .fields import (
    MONTHS_DE,
    ad_id_from_url,
    apply_attributes,
    clean,
    html_to_text,
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


# Kleinanzeigen is migrating ad pages to an Astro app. On those the gallery is
# not in the markup at all - the page ships one <img> and hands the rest to a
# hydration payload for JavaScript to render. The payload is server-rendered
# JSON, so the photos are there to be read; they are just not where a scraper
# would look. Largest rendition first.
GALLERY_RENDITIONS = ("xxLargeUrl", "xLargeUrl", "large3Url", "large2Url", "largeUrl",
                      "teaserUrl", "thumbnail2Url", "thumbnailUrl")


def _unwrap_astro(value):
    """Astro encodes every value as [typeCode, value]. Strip the codes."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        value = value[1]
    if isinstance(value, list):
        return [_unwrap_astro(v) for v in value]
    if isinstance(value, dict):
        return {k: _unwrap_astro(v) for k, v in value.items()}
    return value


def astro_ad_data(soup) -> dict | None:
    """The ad's own data from the hydration payload, if this is the new page.

    Several islands carry the whole blob; the substring check avoids parsing
    megabytes of JSON for the ones that cannot help.
    """
    for island in soup.find_all("astro-island"):
        props = island.get("props") or ""
        if "imageDetails" not in props:
            continue
        try:
            decoded = json.loads(props)
        except (json.JSONDecodeError, TypeError):
            continue
        data = _unwrap_astro(decoded.get("data"))
        if isinstance(data, dict) and isinstance(data.get("imageDetails"), dict):
            return data
    return None


def _astro_images(data: dict) -> list[str]:
    photos = []
    for entry in (data.get("imageDetails") or {}).get("imageList") or []:
        if not isinstance(entry, dict):
            continue
        for key in GALLERY_RENDITIONS:
            if entry.get(key):
                photos.append(entry[key])
                break
    return photos


def _astro_attributes(data: dict) -> dict[str, str]:
    attrs = {}
    for entry in data.get("localizedAttributes") or []:
        if isinstance(entry, dict) and entry.get("localizedName"):
            attrs[entry["localizedName"]] = entry.get("localizedValue") or ""
    return attrs


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


# Labels where a value naming a month beats one naming only a year.
_DATED_LABELS = ("erstzulassung", "hu", "tuv", "tüv")

# What the page calls the category. True of every ad in a motorcycle search, so
# it says nothing; the block's "Enduro/Reiseenduro" says something.
_GENERIC_VALUES = {"motorrad", "motorräder", "motorraeder", "motorroller", "sonstige"}


def _more_precise(label: str, candidate: str, current: str) -> bool:
    """Should the block's value replace the one the page already gave?"""
    if current.strip().lower() in _GENERIC_VALUES \
            and candidate.strip().lower() not in _GENERIC_VALUES:
        return True
    if not any(word in label.lower() for word in _DATED_LABELS):
        return False
    return _has_month(candidate) and not _has_month(current)


def _has_month(text: str) -> bool:
    return ("/" in text or "." in text
            or any(month in text.lower() for month in MONTHS_DE))


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
            # Both payloads carry the description as HTML, not as text.
            out.setdefault("description", html_to_text(block.get("description")))
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

    # --- layer 1b: the hydration payload of the new Astro ad page ---
    astro = astro_ad_data(soup)
    if astro:
        seeded_images.extend(_astro_images(astro))
        raw_attrs.update(_astro_attributes(astro))
        out.setdefault("title", clean(astro.get("title")))
        out.setdefault("description", html_to_text(astro.get("description")))
        if astro.get("formattedCreationDate"):
            posted = parse_posted(astro["formattedCreationDate"])
            if posted:
                out["posted_at"] = posted.isoformat(timespec="seconds")
        # The payload states these outright, rather than leaving them to be
        # guessed from where a word appears in the page text.
        seller = astro.get("userDetails") or {}
        if isinstance(seller.get("commercial"), bool):
            out["seller_type"] = "commercial" if seller["commercial"] else "private"
        if seller.get("userId"):
            out["seller_id"] = str(seller["userId"])
        if seller.get("contactName"):
            out["seller_name"] = clean(seller["contactName"])

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

    if not out.get("posted_at"):
        posted = None
        for node in soup.select(", ".join(DATE_SELECTORS)):
            posted = parse_posted(clean(node.get_text()))
            if posted:
                break
        out["posted_at"] = posted.isoformat(timespec="seconds") if posted else None

    if not out.get("seller_name"):
        out["seller_name"] = _first_text(soup, SELLER_SELECTORS)
    if not out.get("seller_type"):
        out["seller_type"] = parse_seller_type(soup.get_text(" ", strip=True)[:6000])
    if not out.get("seller_id"):
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
        out["description"] = html_to_text(meta.get("content")) if meta else None
    if not out.get("image_urls"):
        out["image_urls"] = [
            m["content"] for m in soup.select("meta[property='og:image'][content]")
        ]

    # --- layer 4: identity and typed fields ---
    out["id"] = _ad_id(soup, url)
    # An ad cross-posted from mobile.de carries a spec sheet inside its
    # description. Read as prose it is noise in the scoring prompt; read as data
    # it fills columns that would otherwise be NULL. Done last, so every source
    # of a description has been tried and every page attribute is already in.
    spec, equipment, own_words = syndicated.split(out.get("description"))
    if spec:
        out["description"] = own_words
        out["equipment_json"] = equipment
        # The page's own attributes win, being structured at the source - except
        # where the block says the same thing more precisely. A page that gives
        # "Erstzulassung: 2004" against the block's "8/2004" is eight months
        # vaguer, and that feeds straight into the bike's age.
        for label, value in spec.items():
            if label not in raw_attrs or _more_precise(label, value, raw_attrs[label]):
                raw_attrs[label] = value

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


def image_sources(html: str, base_url: str = BASE) -> dict:
    """How many photos each source of an ad page offers, for diagnosis.

    When an ad yields one photo instead of twenty, this says which source the
    page actually populated - the gallery, its JSON-LD, or neither.
    """
    soup = _soup(html)
    counts: dict = {}
    gallery = [b for b, tag in _jsonld_with_tags(soup)
               if b.get("@type") == "ImageObject"
               and tag.find_parent("article", attrs={"data-adid": True}) is None]
    counts["json-ld ImageObject"] = len(gallery)
    counts["json-ld Product image"] = sum(
        len([b["image"]] if isinstance(b.get("image"), str) else b.get("image") or [])
        for b in _jsonld(soup)
        if b.get("@type") in ("Product", "Offer", "Vehicle", "Motorcycle", "Car"))
    for selector in IMAGE_SELECTORS:
        nodes = [n for n in soup.select(selector)
                 if n.find_parent("article", attrs={"data-adid": True}) is None]
        with_src = [n for n in nodes
                    if n.get("src") or n.get("data-imgsrc") or n.get("data-src")
                    or n.get("content")]
        counts[f"selector {selector}"] = len(with_src)
    astro = astro_ad_data(soup)
    counts["astro hydration payload"] = len(_astro_images(astro)) if astro else 0
    counts["meta og:image"] = len(soup.select("meta[property='og:image'][content]"))
    counts["TOTAL collected"] = len(_images(soup, base_url))
    return counts


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
