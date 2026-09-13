"""The spec sheet some ads carry inside their description text.

Ads cross-posted from mobile.de arrive with a block like this appended to
whatever the seller wrote:

    Motorrad, Enduro/Reiseenduro
    Gebrauchtfahrzeug

    Erstzulassung: 8/2004
    Anzahl der Fahrzeughalter: 2
    HU: Neu
    Farbe: Gelb
    Antriebsart: Kardan

    Ausstattung
    ABS, Griffheizung, Navigationsvorbereitung, Scheckheftgepflegt

    Inserat bereitgestellt von

It is a full spec sheet - owner count, final drive, colour, equipment - sitting
in a text field, while the page's own attribute list carries six values. Read as
prose it is noise in the scoring prompt; read as data it fills columns that were
otherwise NULL.

A run of label lines is not enough evidence on its own: a seller who lists
"Reifen: neu / Kette: neu / Bremsen: neu" has written three of them, and cutting
that out of the description would throw away exactly the sort of thing worth
scoring. So the block is only taken when something only mobile.de emits is in
it - the attribution footer, the Ausstattung heading, or one of its own labels.
"""

from __future__ import annotations

import re

# "Erstzulassung: 8/2004", "Farbe (Hersteller): --"
LABEL_RE = re.compile(r"^([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9 .()/-]{1,40}):\s*(.*)$")

# mobile.de writes this where it has nothing.
BLANK_VALUES = {"--", "-", "", "k.a.", "keine angabe"}

MIN_RUN = 3

# Labels mobile.de writes and people do not. One of these, or the Ausstattung
# heading, or the attribution footer, is what tells a syndicated block apart
# from a seller listing what they have replaced.
SYNDICATED_LABELS = {
    "erstzulassung", "anzahl der fahrzeughalter", "kraftstoffart", "antriebsart",
    "farbe (hersteller)", "getriebeart", "schadstoffklasse", "fahrzeugnummer",
    "hubraum", "leistung", "art",
}

# The line above the run says what kind of vehicle it is, in a fixed vocabulary.
CONDITION_WORDS = {
    "gebrauchtfahrzeug", "neufahrzeug", "unfallfahrzeug", "vorführfahrzeug",
    "vorfuehrfahrzeug", "jahreswagen", "oldtimer",
}
EQUIPMENT_HEADING = "ausstattung"
ATTRIBUTION = "inserat bereitgestellt von"


def _is_label(line: str) -> bool:
    return bool(LABEL_RE.match(line.strip()))


def split(description: str | None) -> tuple[dict[str, str], list[str], str | None]:
    """Pull the block out of a description.

    Returns (attributes, equipment, description without the block). When there
    is no block the description comes back unchanged, which is the common case -
    most ads are written by hand.
    """
    if not description:
        return {}, [], description

    lines = description.splitlines()
    start, end = _find_run(lines)
    if start is None:
        return {}, [], description
    if not _is_syndicated(lines, start, end):
        # Someone's own list of what they have replaced. Leave it in the text,
        # where it is worth more than it would be as three loose attributes.
        return {}, [], description

    attributes: dict[str, str] = {}
    for line in lines[start:end]:
        match = LABEL_RE.match(line.strip())
        if not match:
            continue
        label, value = match.group(1).strip(), match.group(2).strip()
        if value.lower() in BLANK_VALUES:
            continue                    # stated as unknown; not a value
        attributes.setdefault(label, value)

    equipment, end = _equipment(lines, end)
    start = _widen_upwards(lines, start, attributes)
    end = _skip_attribution(lines, end)

    kept = lines[:start] + lines[end:]
    remaining = "\n".join(kept).strip() or None
    return attributes, equipment, remaining


def _is_syndicated(lines: list[str], start: int, end: int) -> bool:
    """Is this mobile.de's block, or a person writing a list?"""
    for line in lines[start:end]:
        match = LABEL_RE.match(line.strip())
        if match and match.group(1).strip().lower() in SYNDICATED_LABELS:
            return True
    for line in lines[end:]:
        lowered = line.strip().lower()
        if lowered == EQUIPMENT_HEADING or lowered.startswith(ATTRIBUTION):
            return True
    return False


def _find_run(lines: list[str]) -> tuple[int | None, int | None]:
    """The longest run of consecutive label lines, if it is long enough."""
    best = best_len = None
    index = 0
    while index < len(lines):
        if not _is_label(lines[index]):
            index += 1
            continue
        run_start = index
        while index < len(lines) and _is_label(lines[index]):
            index += 1
        length = index - run_start
        if best_len is None or length > best_len:
            best, best_len = run_start, length
    if best is None or best_len < MIN_RUN:
        return None, None
    return best, best + best_len


def _equipment(lines: list[str], end: int) -> tuple[list[str], int]:
    """An "Ausstattung" heading and the comma-separated list under it."""
    cursor = end
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines) or lines[cursor].strip().lower() != EQUIPMENT_HEADING:
        return [], end

    cursor += 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines):
        return [], end
    items = [part.strip() for part in lines[cursor].split(",") if part.strip()]
    return items, cursor + 1


def _widen_upwards(lines: list[str], start: int, attributes: dict) -> int:
    """Take in the vehicle-type lines that sit just above the run."""
    cursor = start
    for _ in range(3):
        previous = cursor - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous < 0:
            break
        text = lines[previous].strip()
        lowered = text.lower()
        if lowered in CONDITION_WORDS:
            attributes.setdefault("Fahrzeugzustand", text)
        elif lowered.startswith("motorrad,") or lowered.startswith("motorräder,"):
            attributes.setdefault("Art", text.split(",", 1)[1].strip())
        else:
            break
        cursor = previous
    return cursor


def _skip_attribution(lines: list[str], end: int) -> int:
    """"Inserat bereitgestellt von" and whatever follows it is mobile.de's footer."""
    cursor = end
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor < len(lines) and lines[cursor].strip().lower().startswith(ATTRIBUTION):
        return len(lines)
    return end
