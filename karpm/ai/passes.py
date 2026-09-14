"""The three passes, and what each one is asked for.

Pass 1 reads the seller's prose. Pass 2 looks at the photos. Pass 3 weighs
everything up and gives the verdict. They are separate because they are
different jobs - a model that is good at pulling "Reifen neu, Kette bei 40tkm"
out of a paragraph need not be the one you want judging whether a bike looks
cared for - and because they go stale for different reasons.

Nothing here knows which provider will answer. Each pass builds a system prompt
and a neutral block list; `provider.py` turns that into an API call.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from . import provider
from .. import db

# --- pass 1: what the description says ------------------------------------

TEXT_SYSTEM = """You read German motorcycle classified ads and pull out what the \
seller has actually said. You are not judging the bike; you are reading.

Rules that matter more than completeness:
- Only record what the text states or clearly implies. If the ad does not say, \
leave the field empty. A guess is worse than a gap, because everything you \
return is treated downstream as something the seller said.
- Quote or closely paraphrase for anything you flag. The buyer needs to be able \
to check you against the ad.
- German ads are terse and full of abbreviations: "Scheckheft" means a stamped \
service book, "HU neu" means a fresh inspection, "Bastler" means a project bike \
that does not run properly, "gelaufen" refers to mileage. "VB" is an invitation \
to haggle, not a fact about the bike.
- Sellers volunteer good news and bury bad news. What is conspicuously absent \
from an otherwise detailed ad is worth noting."""


class ServiceItem(BaseModel):
    what: str = Field(description="the part or service, in English")
    when: str | None = Field(default=None, description="date or mileage, as stated")
    quote: str = Field(description="the German text this came from")


class TextFindings(BaseModel):
    """What pass 1 returns. Every field is optional because most ads are thin."""

    summary: str = Field(description="two or three sentences, in English, on what "
                                     "the seller is claiming about this bike")
    recent_work: list[ServiceItem] = Field(
        default_factory=list,
        description="parts replaced or servicing done, as stated in the ad")
    known_faults: list[str] = Field(
        default_factory=list,
        description="anything the seller admits is wrong, worn or not working")
    red_flags: list[str] = Field(
        default_factory=list,
        description="phrasing that should worry a buyer: a project bike sold as "
                    "running, an unexplained hurry, a story that does not add up, "
                    "or an ad that says nothing about condition at all")
    selling_reason: str | None = Field(
        default=None, description="why they say they are selling, if they say")
    negotiable: bool | None = Field(
        default=None, description="true if the ad invites an offer (VB, or similar)")
    modifications: list[str] = Field(
        default_factory=list, description="non-standard parts, as stated")
    included_extras: list[str] = Field(
        default_factory=list, description="luggage, spare parts, gear thrown in")
    questions_to_ask: list[str] = Field(
        default_factory=list,
        description="what the ad leaves out that a buyer would want answered")


def text_request(row, cfg) -> provider.Request:
    body = [
        f"Title: {row['title']}",
        f"Price: {row['price_eur']} EUR" if row["price_eur"] else "Price: not stated",
        "",
        "Description, verbatim:",
        row["description"] or "(the ad has no description)",
    ]
    return provider.Request(
        system=TEXT_SYSTEM,
        blocks=[provider.text("\n".join(body))],
        schema=TextFindings,
        model=cfg.model,
        effort=cfg.effort,
    )


# --- pass 2: what the photos show -----------------------------------------

PHOTO_SYSTEM = """You look at photographs of a used motorcycle and report what \
is visible. You are not deciding whether to buy it; you are describing evidence.

Two jobs:

1. Say what the photos show about condition. Corrosion, crash damage, mismatched \
or missing panels, chain and sprocket wear, tyre condition, fluid leaks, a worn \
seat, aftermarket parts, the state of the surroundings it is photographed in. \
Note where you are guessing, and say so.

2. Choose the photographs worth a second look. You will be given up to a dozen; \
pick the few that carry the most information. A gallery is usually five real \
angles and a lot of near-duplicates - take one of each angle, plus anything that \
shows a defect or a detail the others do not. Rank them, best first.

What matters:
- Say what you can see, not what is usually true of this model.
- A photograph that hides something is evidence too: every shot from the good \
side, no engine, nothing of the chain.
- Photographed in a tidy garage on a clean floor is worth noting. So is a bike \
covered in road grime in a dark car park.
- Do not speculate about mileage or history from a photograph."""


class PhotoNote(BaseModel):
    position: int = Field(description="the photo's position, as given to you")
    shows: str = Field(description="what this photo shows, one sentence")
    concern: str | None = Field(
        default=None, description="anything visible here a buyer should look at")


class PhotoFindings(BaseModel):
    condition_summary: str = Field(
        description="three or four sentences on what the photos say about the "
                    "state of this bike")
    visible_issues: list[str] = Field(
        default_factory=list, description="defects actually visible in the photos")
    positives: list[str] = Field(
        default_factory=list, description="signs of care that are visible")
    photo_notes: list[PhotoNote] = Field(
        default_factory=list, description="one note per photo you were shown")
    shortlist: list[int] = Field(
        default_factory=list,
        description="positions of the most informative photos, best first")
    coverage_gaps: list[str] = Field(
        default_factory=list,
        description="what the gallery does not show that a buyer would want to see")
    photo_quality: str | None = Field(
        default=None, description="whether the photographs are good enough to judge "
                                  "condition from at all")


def photo_request(conn, row, cfg) -> provider.Request | None:
    """None when there is nothing to look at."""
    shown = []
    for image in db.listing_images(conn, row["id"])[: cfg.max_photos_in]:
        path = Path(image["local_path"]) if image["local_path"] else None
        if path and path.exists():
            shown.append((image["position"], path))
    if not shown:
        return None

    blocks = [provider.text(
        f"{row['title']}. {len(shown)} photograph(s) follow, "
        f"at positions {', '.join(str(p) for p, _ in shown)}. "
        f"Shortlist at most {cfg.shortlist}.")]
    for position, path in shown:
        blocks.append(provider.text(f"Photo at position {position}:"))
        blocks.append(provider.image(path))

    return provider.Request(
        system=PHOTO_SYSTEM,
        blocks=blocks,
        schema=PhotoFindings,
        model=cfg.model,
        effort=cfg.effort,
    )
