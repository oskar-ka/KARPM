"""Normalising the German free-text values Kleinanzeigen shows into typed fields.

Everything here is pure and unit-tested - it is the part of the scraper that
does not depend on the site's HTML staying the same.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

MONTHS_DE = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    "jan": 1, "feb": 2, "mrz": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "okt": 10, "nov": 11, "dez": 12,
}


def clean(text: str | None) -> str | None:
    """Collapse whitespace, strip non-breaking spaces."""
    if text is None:
        return None
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


# Tags that end a line of their own. <br> is the one that matters - ad
# descriptions are written in a plain textarea and come back with one per line.
_BREAKS = ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote")


def html_to_text(value: str | None) -> str | None:
    """Plain text from a description that arrives as markup.

    The JSON-LD block and the Astro payload both carry the description as HTML -
    a `<br />` per line break and entities for punctuation - so keeping the
    string as it stands puts tags in the database, in the scoring prompt and in
    the email. The CSS-selector path never had this problem because it reads
    `get_text()`, which is why it went unnoticed on the older ad pages.
    """
    if value is None:
        return None
    if "<" not in value and "&" not in value:
        return clean(value)             # already plain; nothing to undo

    frag = BeautifulSoup(value, "html.parser")
    for br in frag.find_all("br"):
        br.replace_with("\n")
    for block in frag.find_all(_BREAKS):
        block.append("\n")
    # get_text also resolves the entities, so &#x2F; is a slash by here.
    text = frag.get_text()
    text = "\n".join(line.strip() for line in text.splitlines())
    return clean(text)


def slug(label: str) -> str:
    """Normalise an attribute label so 'Erstzulassung:' == 'erstzulassung'."""
    label = unicodedata.normalize("NFKD", (label or "").lower())
    label = label.replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "", label)


def parse_int_de(text: str | None) -> int | None:
    """'12.345 km' -> 12345. German thousands separators are dots."""
    if not text:
        return None
    match = re.search(r"\d[\d.\s]*", text.replace("\xa0", " "))
    if not match:
        return None
    digits = re.sub(r"[.\s]", "", match.group(0))
    try:
        return int(digits)
    except ValueError:
        return None


def parse_price(text: str | None) -> tuple[int | None, str]:
    """'4.500 € VB' -> (4500, 'vb'). Returns (amount, kind)."""
    if not text:
        return None, "unknown"
    lowered = text.lower()
    if "verschenk" in lowered:          # "Zu verschenken"
        return 0, "free"
    if "tausch" in lowered and not re.search(r"\d", lowered):
        return None, "unknown"
    amount = parse_int_de(text)
    if amount is None:
        return None, "vb" if "vb" in lowered else "unknown"
    # Kleinanzeigen marks negotiable prices "VB" (Verhandlungsbasis).
    kind = "vb" if re.search(r"\bvb\b|verhandlungsbasis", lowered) else "fixed"
    return amount, kind


def parse_month_year(text: str | None) -> date | None:
    """Erstzulassung / HU are given as '05/2019', '2019', or 'Mai 2019'."""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"(\d{1,2})\s*[/.\-]\s*(\d{4})", text)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1900 <= year <= 2100:
            return date(year, month, 1)
    m = re.search(r"([a-zA-ZäöüÄÖÜ]+)\s+(\d{4})", text)
    if m:
        month = MONTHS_DE.get(m.group(1).lower())
        if month:
            return date(int(m.group(2)), month, 1)
    m = re.search(r"\b(19|20)\d{2}\b", text)
    if m:
        return date(int(m.group(0)), 1, 1)
    return None


def parse_posted(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Heute, 14:23' / 'Gestern, 09:12' / '12.03.2026' -> datetime."""
    if not text:
        return None
    now = now or datetime.now()
    lowered = text.lower().strip()
    time_match = re.search(r"(\d{1,2}):(\d{2})", lowered)
    hour, minute = (int(time_match.group(1)), int(time_match.group(2))) if time_match else (0, 0)

    if "heute" in lowered:
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "gestern" in lowered:
        day = now - timedelta(days=1)
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", lowered)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), hour, minute)
    m = re.search(r"(\d{1,2})\.\s*([a-zäöü]+)\s*(\d{4})", lowered)
    if m and (month := MONTHS_DE.get(m.group(2))):
        return datetime(int(m.group(3)), month, int(m.group(1)), hour, minute)
    return None


def parse_location(text: str | None) -> tuple[str | None, str | None]:
    """'12345 Berlin - Mitte' -> ('12345', 'Berlin - Mitte')."""
    text = clean(text)
    if not text:
        return None, None
    m = re.match(r"^\s*(\d{4,5})\s+(.*)$", text)
    if m:
        return m.group(1), m.group(2).strip()
    return None, text


def parse_seller_type(text: str | None) -> str:
    lowered = (text or "").lower()
    if "privat" in lowered:
        return "private"
    if "gewerb" in lowered or "händler" in lowered or "haendler" in lowered:
        return "commercial"
    return "unknown"


def ad_id_from_url(url: str | None) -> str | None:
    """Ad URLs look like /s-anzeige/<slug>/2847612345-305-2074 - the id is first."""
    if not url:
        return None
    m = re.search(r"/s-anzeige/[^/]+/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{9,12})\b", url)
    return m.group(1) if m else None


# Attribute labels seen on motorcycle listings, mapped to our column names.
ATTRIBUTE_MAP = {
    "kilometerstand": "km",
    "laufleistung": "km",
    "erstzulassung": "first_reg",
    "baujahr": "model_year",
    "leistung": "hp",
    "ps": "hp",
    "hubraum": "ccm",
    "art": "bike_type",
    "fahrzeugtyp": "bike_type",
    "marke": "make",
    "modell": "model",
    "fahrzeugzustand": "condition",
    "zustand": "condition",
    "hutuv": "inspection_until",
    "hu": "inspection_until",
    "hubis": "inspection_until",
    "tuv": "inspection_until",
    "tuvbis": "inspection_until",
    "hauptuntersuchung": "inspection_until",
    "anzahlfahrzeughalter": "owners",
    "anzahlderfahrzeughalter": "owners",
    "fahrzeughalter": "owners",
    "halter": "owners",
    "scheckheftgepflegt": "full_service_hist",
    "farbe": "color",
    "kraftstoffart": "fuel_type",
    "kraftstoff": "fuel_type",
    "antriebsart": "drive_type",
    "antrieb": "drive_type",
    "getriebe": "transmission",
    "getriebeart": "transmission",
}


def mapped_column(label: str) -> str | None:
    """Which typed field a raw label feeds, if any."""
    normalised = slug(label)
    key = ATTRIBUTE_MAP.get(normalised)
    if key is None and normalised.endswith("bis"):
        key = ATTRIBUTE_MAP.get(normalised[: -len("bis")])
    # One key, two columns; the date is the one worth checking for.
    return "first_reg_date" if key == "first_reg" else key


def is_mapped(label: str) -> bool:
    """True if this raw attribute already has a typed column of its own.

    Both the scoring prompt and the listing page use this to avoid showing the
    same fact twice. They each had their own idea of it once, which is how the
    page came to list every attribute again under the typed ones.
    """
    normalised = slug(label)
    return (normalised in ATTRIBUTE_MAP
            or (normalised.endswith("bis") and normalised[:-3] in ATTRIBUTE_MAP))


def apply_attributes(attrs: dict[str, str]) -> tuple[dict, list[str]]:
    """Map raw {label: value} pairs onto typed listing fields.

    Returns (fields, warnings). Unmapped labels are kept in the raw attributes
    blob so nothing is lost if Kleinanzeigen adds a field later.
    """
    fields: dict = {}
    warnings: list[str] = []

    for label, value in attrs.items():
        normalised = slug(label)
        key = ATTRIBUTE_MAP.get(normalised)
        if key is None and normalised.endswith("bis"):
            key = ATTRIBUTE_MAP.get(normalised[: -len("bis")])
        if key is None:
            continue
        if key == "km":
            fields["km"] = parse_int_de(value)
        elif key == "hp":
            fields["hp"] = parse_int_de(value)
        elif key == "ccm":
            fields["ccm"] = parse_int_de(value)
        elif key == "owners":
            fields["owners"] = parse_int_de(value)
        elif key == "model_year":
            fields["model_year"] = parse_int_de(value)
        elif key == "first_reg":
            d = parse_month_year(value)
            if d:
                fields["first_reg_date"] = d.isoformat()
                fields["first_reg_year"] = d.year
            elif clean(value):
                warnings.append(f"unreadable:{label}={clean(value)}")
        elif key == "inspection_until":
            d = parse_month_year(value)
            if d:
                fields["inspection_until"] = d.isoformat()
            elif clean(value):
                # "HU: Neu" is a real answer that is not a date. Dropping it
                # silently would lose the one thing the ad said about the TÜV.
                warnings.append(f"unreadable:{label}={clean(value)}")
        elif key == "condition":
            fields["condition"] = clean(value)
            lowered = (value or "").lower()
            if "unbesch" in lowered:
                fields["damaged"] = 0
            elif "besch" in lowered:
                fields["damaged"] = 1
        elif key == "full_service_hist":
            fields["full_service_hist"] = 0 if "nein" in (value or "").lower() else 1
        elif key in ("make", "model", "bike_type", "color", "fuel_type",
                     "drive_type", "transmission"):
            fields[key] = clean(value)

    for required in ("km", "first_reg_year"):
        if fields.get(required) is None:
            warnings.append(f"missing:{required}")
    return fields, warnings


# A German plate: district letters, one or two identifying letters, up to four
# digits - optionally followed by the two numbers of a Saisonkennzeichen.
PLATE_RE = re.compile(
    r"\b([A-ZÄÖÜ]{1,3})[-\s]?([A-Z]{1,2})[-\s]?(\d{1,4})\b(?:\s*(\d{1,2})\s*/\s*(\d{1,2}))?"
)
# Only read a plate where the text says that is what it is. The pattern alone
# matches things like "TUV AU 2024" and half the model names on the site, and a
# wrong plate is worse than none: it would be read as this bike's history.
PLATE_CONTEXT = re.compile(r"(kennzeichen|nummernschild|amtliches?\s+kennzeichen)\s*:?\s*",
                           re.IGNORECASE)


def parse_plate(text: str | None) -> tuple[str | None, str | None]:
    """A licence plate and its season, from text that says it is one.

    Returns (plate, season) - season being "04/10" for a Saisonkennzeichen,
    which says the bike is only registered April to October. That is worth
    knowing on its own: it means winter storage, lower yearly mileage and a
    cheaper insurance band.
    """
    if not text:
        return None, None
    context = PLATE_CONTEXT.search(text)
    if not context:
        return None, None
    found = PLATE_RE.match(text[context.end():].strip())
    if not found:
        return None, None
    district, letters, digits, from_month, to_month = found.groups()
    plate = f"{district}-{letters} {digits}"
    season = f"{int(from_month):02d}/{int(to_month):02d}" if from_month and to_month else None
    return plate, season
