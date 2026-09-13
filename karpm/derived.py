"""Numbers worked out from what was stored, rather than read off the page.

Nothing here needs another request. They are the figures a person computes in
their head while reading an ad - how hard has it been ridden, how long until
the next TÜV, how long has this been sitting unsold - and each one is more
useful to the scoring prompt than the raw fields it comes from.

Every value may be None. None means "cannot say", and must be shown as that
rather than as a zero, which reads as a fact.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from . import geo

# A bike ridden this little per year has probably sat still, which is its own
# kind of wear: seals, fuel, brake fluid, flat-spotted tyres.
LOW_KM_PER_YEAR = 1500


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        return None


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def age_years(row, today: date | None = None) -> float | None:
    """Age from first registration, in years."""
    registered = _as_date(row["first_reg_date"])
    if registered is None and row["first_reg_year"]:
        registered = date(int(row["first_reg_year"]), 1, 1)
    if registered is None:
        return None
    days = ((today or date.today()) - registered).days
    return round(days / 365.25, 1) if days > 0 else None


def km_per_year(row, today: date | None = None) -> int | None:
    """The single most telling number about a used bike.

    8,000 a year is a bike that gets ridden; 800 is one that has been standing,
    and standing costs more to put right than mileage does.
    """
    age = age_years(row, today)
    if not age or not row["km"]:
        return None
    return round(row["km"] / age)


def hu_months_left(row, today: date | None = None) -> int | None:
    """Months of TÜV remaining. Negative means it has run out."""
    until = _as_date(row["inspection_until"])
    if until is None:
        return None
    return _months_between(today or date.today(), until)


def days_on_market(row, now: datetime | None = None) -> int | None:
    """How long the ad has been up - which is how much room there is to haggle."""
    posted = _as_date(row["posted_at"]) or _as_date(row["first_seen_at"])
    if posted is None:
        return None
    return max(((now.date() if now else date.today()) - posted).days, 0)


def price_history(conn, listing_id: str) -> list[tuple[str, int, int]]:
    """Every price change, oldest first: (when, from, to)."""
    rows = conn.execute(
        "SELECT observed_at, prev_price_eur, price_eur FROM listing_history "
        "WHERE listing_id = ? AND event = 'price_change' AND prev_price_eur IS NOT NULL "
        "ORDER BY id", (listing_id,)).fetchall()
    return [(r["observed_at"][:10], r["prev_price_eur"], r["price_eur"]) for r in rows]


def total_price_drop(conn, listing_id: str, row=None) -> tuple[int, int] | None:
    """(euros, percent) off the first asking price, or None if it never moved."""
    changes = price_history(conn, listing_id)
    if not changes:
        return None
    first, last = changes[0][1], changes[-1][2]
    if not first or last is None or last >= first:
        return None
    return first - last, round((first - last) / first * 100)


def distance_km(row, home_plz: str | None) -> int | None:
    return geo.distance_km(home_plz, row["postcode"] or row["location"])


def equipment(row) -> list[str]:
    try:
        return json.loads(row["equipment_json"] or "[]")
    except (TypeError, ValueError):
        return []


def summarise(conn, row, home_plz: str | None = None, today: date | None = None) -> dict:
    """Every derived value for one listing, for the page and for the prompt."""
    today = today or datetime.now(timezone.utc).date()
    return {
        "age_years": age_years(row, today),
        "km_per_year": km_per_year(row, today),
        "hu_months_left": hu_months_left(row, today),
        "days_on_market": days_on_market(row),
        "price_drop": total_price_drop(conn, row["id"], row),
        "distance_km": distance_km(row, home_plz),
        "equipment": equipment(row),
        "photo_count": conn.execute(
            "SELECT COUNT(*) n FROM images WHERE listing_id = ? AND local_path IS NOT NULL",
            (row["id"],)).fetchone()["n"],
    }
