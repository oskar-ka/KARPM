"""How far away a listing is.

A bike 40 km away is a Saturday morning; the same bike 500 km away is a weekend
and a trailer. That difference belongs in the scoring prompt, and it needs
nothing but the postcode both ends already carry.

Coordinates come from a table shipped with the package rather than a geocoding
service: the Pi is often the only thing awake at 07:30, and a lookup that can
fail over the network is a lookup that will.
"""

from __future__ import annotations

import gzip
import logging
import math
import re
from functools import lru_cache
from importlib import resources

log = logging.getLogger(__name__)

PLZ_RE = re.compile(r"\b(\d{5})\b")

# Straight-line distance understates a drive. Germany's road network puts the
# real distance at roughly a quarter more, which is close enough to plan around
# and honest about being an estimate.
ROAD_FACTOR = 1.25


@lru_cache(maxsize=1)
def _centroids() -> dict[str, tuple[float, float]]:
    """postcode -> (lat, lon). Read once, on the first question asked."""
    table: dict[str, tuple[float, float]] = {}
    try:
        raw = resources.files("karpm.data").joinpath("plz_centroids.csv.gz").read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        log.warning("the postcode table is missing; distances will be unavailable")
        return table
    for line in gzip.decompress(raw).decode("utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        plz, lat, lon = line.split(",")
        table[plz] = (float(lat), float(lon))
    return table


def postcode(value: str | None) -> str | None:
    """The five-digit postcode in a string like "22765 Hamburg - Altona"."""
    if not value:
        return None
    found = PLZ_RE.search(value)
    return found.group(1) if found else None


def coordinates(plz: str | None) -> tuple[float, float] | None:
    return _centroids().get(plz) if plz else None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in kilometres."""
    radius = 6371.0
    lat1, lon1 = a
    lat2, lon2 = b
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    inner = (math.sin(dphi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(inner))


def distance_km(home: str | None, there: str | None) -> int | None:
    """Rough road distance between two German postcodes, or None.

    None means "cannot say" - an unknown postcode, or no home set - and must not
    be shown as 0 km, which reads as "just round the corner".
    """
    start, end = coordinates(postcode(home)), coordinates(postcode(there))
    if start is None or end is None:
        return None
    return round(haversine_km(start, end) * ROAD_FACTOR)
