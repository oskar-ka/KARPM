"""preferences.md, as five questions rather than one blank page.

The file itself is unchanged - it is still one markdown document, and it still
goes to pass 3 whole. What changed is how it is edited: "tell the model what you
want" is a hard thing to answer into an empty textarea, and five narrower
questions get better answers out of a person than one broad one.

The five boxes are the file: what the page shows is all of it, and a save
rewrites it whole. Anything under a heading we do not know - a file written by
hand, or written before this page existed - is not shown and does not survive
the next save. The backup written beside the file is what that leans on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

@dataclass(frozen=True)
class Part:
    key: str
    heading: str
    help: str
    placeholder: str = ""


PARTS = (
    Part("about", "About the bike",
         "What you are looking for at all. The search URLs decide what gets "
         "fetched; this decides what counts as a good one when it arrives.",
         "A do-everything travel enduro I can ride to work and take to the Alps."),
    Part("needs", "What it needs",
         "Hard requirements. A listing that fails one of these should score "
         "badly however good it looks otherwise.",
         "- Under 60,000 km\n- Full service history\n- No accident damage"),
    Part("likes", "What I would like",
         "Wants, not requirements. These lift a score; missing them should not "
         "sink one.",
         "- Panniers or a topcase included\n- Heated grips\n- One previous owner"),
    Part("unimportant", "What is not important",
         "Just as useful as the wants: it stops a listing being marked down for "
         "something you would never notice.",
         "- Colour\n- Scratched panels, as long as nothing is bent\n- Tyre brand"),
    Part("logistics",  "Logistics",
         "Distance, budget, timing, and what you can do yourself. A bargain "
         "six hours away is not the same bargain.",
         "- Up to 300 km, further only for something special\n"
         "- 7,000 EUR ceiling, 6,000 comfortable\n"
         "- I can do my own servicing, not engine work"),
)

BY_KEY = {part.key: part for part in PARTS}
HEADINGS = {part.heading.lower(): part.key for part in PARTS}

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def split(text: str | None) -> dict:
    """One markdown file into a value per box.

    Only the five headings are read. Anything else the file held is not shown,
    and a save will not write it back.
    """
    values = {part.key: "" for part in PARTS}
    if not text or not text.strip():
        return values

    marks = list(_HEADING.finditer(text))
    for index, mark in enumerate(marks):
        key = HEADINGS.get(mark.group(1).strip().lower())
        if key is None:
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()
        # The same heading twice: keep both rather than pick one.
        values[key] = f"{values[key]}\n\n{body}".strip() if values[key] else body
    return values


def compile(values: dict) -> str:
    """The boxes back into the file pass 3 is given.

    Empty boxes keep their heading. The file is also a document somebody reads,
    and a missing section reads as an oversight where an empty one reads as "I
    do not care about this", which is itself worth telling the model.
    """
    out = []
    for part in PARTS:
        body = (values.get(part.key) or "").strip()
        out.append(f"## {part.heading}\n\n{body}".rstrip())
    return "\n\n".join(out) + "\n"
