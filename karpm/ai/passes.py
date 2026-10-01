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

import hashlib
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from . import provider
from .. import db

log = logging.getLogger(__name__)

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


# --- the prompts, and where they live -------------------------------------

def defaults() -> dict:
    """The prompts built into the code, by pass.

    A function rather than a constant because it is also what seeds the file on
    disk, and a caller that edits what it gets back must not be editing ours.
    """
    return {"text": TEXT_SYSTEM, "photos": PHOTO_SYSTEM}


def load_prompt(kind: str, cfg) -> str:
    """The instructions this pass runs with.

    The file wins when there is one; otherwise the built-in. A fresh install has
    no file, and a pass that refused to run without one would make editing the
    prompt a requirement rather than an option.
    """
    path = Path(getattr(cfg, "prompt_file", "") or "")
    # No file yet is the ordinary state of a fresh install, and not worth a
    # word. A file that is there and will not be read is a different matter:
    # running against instructions nobody chose would look exactly like running
    # against the ones they wrote.
    if str(path) and path.exists():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
            log.warning("%s is empty, so the %s pass is using the built-in "
                        "prompt", path, kind)
        except OSError as exc:
            log.warning("could not read the %s prompt from %s (%s); using the "
                        "built-in one", kind, path, exc)
    return defaults()[kind]


def settings(kind: str, cfg) -> dict:
    """The settings that shape a pass's answer, as the version hashes them.

    What is sent to the model and nothing else: the pass's switch, its queue
    size and where its prompt is stored change nothing about what comes back,
    so they are left out - flipping one must not send every listing through
    the pass again.
    """
    out = {"provider": cfg.provider, "model": cfg.model, "effort": cfg.effort,
           "temperature": cfg.temperature, "max_tokens": cfg.max_tokens}
    if kind == "photos":
        out["max_photos_in"] = cfg.max_photos_in
        out["shortlist"] = cfg.shortlist
    return out


def digest(prompt: str, params: dict) -> str:
    """Eight hex characters standing for one prompt and one set of parameters.

    The same input always gives the same eight characters, so an edit you undo
    costs nothing, and anything that would change the answer changes them.
    """
    canonical = json.dumps({"prompt": prompt, "params": params}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def version(kind: str, cfg, prompt: str | None = None) -> str:
    """The key that decides whether stored findings are still current.

    A hash of the prompt's text and of the settings that shape the answer. It
    replaces a version typed into the config by hand: editing the prompt or
    pointing the pass at another model is exactly as much of a change as
    bumping a number, and nobody remembers to bump a number.
    """
    return digest(prompt if prompt is not None else load_prompt(kind, cfg),
                  settings(kind, cfg))


# --- pass 1 and 2 requests -------------------------------------------------

def _request(kind: str, cfg, prompt: str | None, blocks: list[dict]) -> provider.Request:
    """One request for a pass, with the settings every request of it carries."""
    return provider.Request(
        system=prompt if prompt is not None else load_prompt(kind, cfg),
        blocks=blocks,
        schema=TextFindings if kind == "text" else PhotoFindings,
        model=cfg.model,
        effort=cfg.effort,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )


def _text_body(title: str, price: str, description: str) -> str:
    return "\n".join([f"Title: {title}", f"Price: {price}", "",
                      "Description, verbatim:", description])


def text_request(row, cfg, prompt: str | None = None) -> provider.Request:
    body = _text_body(
        row["title"],
        f"{row['price_eur']} EUR" if row["price_eur"] else "not stated",
        row["description"] or "(the ad has no description)",
    )
    return _request("text", cfg, prompt, [provider.text(body)])


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


def photo_request(conn, row, cfg, prompt: str | None = None) -> provider.Request | None:
    """None when there is nothing to look at."""
    shown = []
    for image in db.listing_images(conn, row["id"])[: cfg.max_photos_in]:
        path = Path(image["local_path"]) if image["local_path"] else None
        if path and path.exists():
            shown.append((image["position"], path))
    if not shown:
        return None

    blocks = [provider.text(_photo_intro(row["title"], str(len(shown)),
                                         ", ".join(str(p) for p, _ in shown), cfg))]
    for position, path in shown:
        blocks.append(provider.text(_photo_label(str(position))))
        blocks.append(provider.image(path))

    return _request("photos", cfg, prompt, blocks)


def _photo_intro(title: str, count: str, positions: str, cfg) -> str:
    return (f"{title}. {count} photograph(s) follow, at positions {positions}. "
            f"Shortlist at most {cfg.shortlist}.")


def _photo_label(position: str) -> str:
    return f"Photo at position {position}:"


# --- the shape of a request, with nothing of any ad in it ------------------

def template_request(kind: str, cfg, prompt: str | None = None) -> provider.Request:
    """A pass's request with placeholders where the ad's own fields go.

    Built by the same code as the real thing, so what it shows is what is
    sent: only the values are stand-ins. The photo pass is shown with one
    photograph; a real request repeats the label and the image once per photo.
    """
    if kind == "text":
        body = _text_body("{title}", "{price} EUR, or 'not stated'", "{description}")
        return _request("text", cfg, prompt, [provider.text(body)])
    blocks = [
        provider.text(_photo_intro("{title}", "{count}", "{positions}", cfg)),
        provider.text(_photo_label("{position}")),
        provider.image_stub("{photo, base64 - one label and one image per photo}"),
    ]
    return _request("photos", cfg, prompt, blocks)
