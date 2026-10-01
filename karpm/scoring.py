"""Pass 3: the verdict.

Each listing is judged against your written preferences, against what the two
reading passes made of its description and photos, and against what the database
has actually seen for the same model. It comes back as a structured score rather
than prose - so the result is sortable, storable, and comparable across months
of listings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from . import db, derived
from .ai import passes, provider
from .parse.fields import is_mapped

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are appraising used motorcycles advertised on Kleinanzeigen, a German \
classifieds site, for one specific buyer. You will be given one listing at a \
time, the buyer's preferences, and price statistics for comparable listings \
already in the buyer's database.

Judge each listing on two axes and then give one overall verdict:

fit (1-5)   - how well the bike matches the buyer's stated preferences.
              5 = exactly what they asked for; 1 = not the kind of bike they want.
value (1-5) - value for money, given mileage, age, condition, service history, \
              damage, and how the asking price compares to the listed comparables.
              5 = clearly underpriced for what it is; 3 = market rate; 1 = overpriced.
overall (1-5) - the buyer's practical verdict: is this worth contacting the seller \
              about today? Weigh fit slightly higher than value, and let a serious \
              red flag (accident damage, missing papers, inconsistent story, \
              obviously scam-like wording, expired inspection on an old bike) cap \
              the overall at 2 regardless of price.

Rules:
- Reserve 5 for listings you would tell the buyer to act on immediately. If every \
  listing scores 4-5 the score is useless; most ordinary listings are a 3.
- Base value on the comparables provided when they are given. If none are given, \
  say so in your reasoning rather than inventing a market price.
- German listings often omit things. Absent information is not the same as a \
  defect - note it as an open question in cons, do not assume the worst.
- Watch for the classic Kleinanzeigen scam patterns: price far below market with \
  a vague description, seller claiming to be abroad, shipping/escrow offered for \
  a vehicle, pressure to move off-platform. Flag these in red_flags.
- Photos, when provided, are evidence about condition: rust, crash damage, worn \
  tyres, mismatched parts, a clean garage vs. a field. Use them.
- Write headline, reasoning, pros, cons and red_flags in English, quoting German \
  terms from the listing where useful.
- fair_price_eur is your estimate of what this bike should cost in euros given \
  everything you know. Use null only if you truly cannot tell.
"""


class ListingScore(BaseModel):
    """The structured verdict the model must return for every listing."""

    overall: int = Field(ge=1, le=5, description="Practical verdict, 1-5")
    fit: int = Field(ge=1, le=5, description="Match against the buyer's preferences, 1-5")
    value: int = Field(ge=1, le=5, description="Value for money, 1-5")
    fair_price_eur: int | None = Field(description="Estimated fair price in euros, or null")
    headline: str = Field(description="One line, max 90 characters, what this bike is")
    reasoning: str = Field(description="2-4 sentences justifying the overall score")
    pros: list[str] = Field(description="Concrete strengths, each a short phrase")
    cons: list[str] = Field(description="Concrete weaknesses or open questions")
    red_flags: list[str] = Field(description="Scam or serious-defect signals; empty if none")


def load_preferences(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Preferences file {path} not found - this is the description of what you "
            "actually want, and scoring is meaningless without it."
        )
    return path.read_text(encoding="utf-8").strip()


def extractions_to_text(found: dict) -> list[str]:
    """What the first two passes found, as lines for the scoring prompt.

    Marked as read rather than as fact: these came from a model reading an ad,
    and the one weighing them up should know that.
    """
    lines = []
    text = (found.get("text") or {}).get("data") or {}
    if text:
        lines.append("\nRead from the description by an earlier pass "
                     "(a model's reading of the seller's words, not verified):")
        if text.get("summary"):
            lines.append(f"  {text['summary']}")
        for label, key in (("Work the seller says was done", "recent_work"),
                           ("Faults the seller admits", "known_faults"),
                           ("Wording worth noting", "red_flags"),
                           ("Modifications", "modifications"),
                           ("Included", "included_extras")):
            items = text.get(key) or []
            if not items:
                continue
            rendered = [
                f"{i['what']}" + (f" ({i['when']})" if i.get("when") else "")
                if isinstance(i, dict) else str(i) for i in items
            ]
            lines.append(f"  {label}: " + "; ".join(rendered))
        if text.get("selling_reason"):
            lines.append(f"  Reason for selling, as stated: {text['selling_reason']}")
        if text.get("questions_to_ask"):
            lines.append("  The ad does not say: " + "; ".join(text["questions_to_ask"]))

    photos = (found.get("photos") or {}).get("data") or {}
    if photos:
        lines.append("\nSeen in the photographs by an earlier pass:")
        if photos.get("condition_summary"):
            lines.append(f"  {photos['condition_summary']}")
        for label, key in (("Visible problems", "visible_issues"),
                           ("Visible signs of care", "positives"),
                           ("Not shown at all", "coverage_gaps")):
            items = photos.get(key) or []
            if items:
                lines.append(f"  {label}: " + "; ".join(str(i) for i in items))
        if photos.get("photo_quality"):
            lines.append(f"  On the photographs themselves: {photos['photo_quality']}")
        if photos.get("shortlist"):
            lines.append("  The photographs below are the ones it thought most "
                         "worth a second look.")
    return lines


def _thousands(value) -> str:
    """12345 -> "12.345", as Germans write it. Anything else is left as it is,
    which is what lets a placeholder stand where a number goes."""
    return f"{value:,}".replace(",", ".") if isinstance(value, int) else str(value)


def _flag(value) -> str | None:
    """A yes-or-absent fact. A string is passed through, so a placeholder can
    stand in for the "yes"."""
    if isinstance(value, str):
        return value
    return "yes" if value else None


def listing_to_text(row, comparables: dict | None, worked_out: dict | None = None,
                    found: dict | None = None) -> str:
    """Render a listing row as compact facts for the prompt."""
    def fmt(label: str, value, suffix: str = "") -> str | None:
        return f"{label}: {value}{suffix}" if value not in (None, "") else None

    price = row["price_eur"]
    price_str = "not stated"
    if price is not None:
        price_str = f"{_thousands(price)} EUR"
        if row["price_kind"] == "vb":
            price_str += " (VB - negotiable)"
        elif row["price_kind"] == "free":
            price_str = "free"

    lines = [
        f"Title: {row['title']}",
        f"Price: {price_str}",
        *filter(None, [
            fmt("Make", row["make"]),
            fmt("Model", row["model"]),
            fmt("Type", row["bike_type"]),
            fmt("First registration", row["first_reg_date"]),
            fmt("Model year", row["model_year"]),
            fmt("Mileage", _thousands(row["km"]) if row["km"] else None, " km"),
            fmt("Power", row["hp"], " PS"),
            fmt("Displacement", row["ccm"], " ccm"),
            fmt("Previous owners", row["owners"]),
            fmt("Inspection valid until (HU/TUEV)", row["inspection_until"]),
            fmt("Condition", row["condition"]),
            fmt("Damaged", _flag(row["damaged"])),
            fmt("Full service history", _flag(row["full_service_hist"])),
            fmt("Colour", row["color"]),
            fmt("Final drive", row["drive_type"]),
            fmt("Transmission", row["transmission"]),
            fmt("Fuel", row["fuel_type"]),
            fmt("Seller", row["seller_type"]),
            fmt("Location", row["location"]),
            fmt("Posted", row["posted_at"]),
            fmt("Views", row["view_count"]),
        ]),
    ]

    equipment = json.loads(row["equipment_json"] or "[]") if row["equipment_json"] else []
    if equipment:
        lines.append("Listed equipment: " + ", ".join(equipment))

    # Worked out rather than read off the page. The model could derive these
    # itself, but it would be doing arithmetic instead of judging a motorcycle.
    if worked_out:
        facts = []
        if worked_out.get("km_per_year"):
            facts.append(f"{_thousands(worked_out['km_per_year'])} km/year")
        if worked_out.get("age_years"):
            facts.append(f"{worked_out['age_years']} years old")
        months = worked_out.get("hu_months_left")
        if isinstance(months, str):
            facts.append(months)        # a placeholder standing for the line
        elif months is not None:
            facts.append(f"HU expired {-months} month(s) ago" if months < 0
                         else f"{months} month(s) of HU left")
        if worked_out.get("distance_km") is not None:
            facts.append(f"about {worked_out['distance_km']} km away")
        if worked_out.get("days_on_market") is not None:
            facts.append(f"{worked_out['days_on_market']} day(s) on the market")
        if worked_out.get("price_drop"):
            euros, percent = worked_out["price_drop"]
            facts.append(f"asking price cut by {euros} EUR ({percent}%) since it went up")
        if worked_out.get("photo_count"):
            facts.append(f"{worked_out['photo_count']} photo(s)")
        if facts:
            lines.append("Worked out: " + "; ".join(facts))

    # Only pass through attributes that are not already shown as typed fields
    # above, so the model does not read the same fact twice.
    extra = json.loads(row["attributes_json"] or "{}")
    unmapped = {k: v for k, v in extra.items() if not is_mapped(k)}
    if unmapped:
        lines.append("Other listed attributes: " + ", ".join(f"{k}: {v}" for k, v in unmapped.items()))

    lines.extend(extractions_to_text(found or {}))
    lines.append("\nFull description (verbatim, German):\n" + (row["description"] or "(empty)"))

    if comparables:
        lines.append(
            "\nComparable listings for this model already collected "
            f"(n={comparables['sample_size']}): "
            f"25th percentile {comparables['price_p25']} EUR, "
            f"median {comparables['price_median']} EUR, "
            f"75th percentile {comparables['price_p75']} EUR"
            + (f", median mileage {comparables['km_median']} km" if comparables["km_median"] else "")
        )
    else:
        lines.append("\nNo comparable listings for this model in the database yet.")

    return "\n".join(lines)


def _image_blocks(conn, listing_id: str, max_images: int,
                  shortlist: list[int] | None = None) -> list[dict]:
    """The photographs to show the scoring model.

    The shortlist comes from the photo pass, which has looked at the whole
    gallery. Without one this falls back to the first few in the seller's order,
    which is arbitrary - it is whatever they happened to upload first.
    """
    if max_images <= 0:
        return []
    rows = db.listing_images(conn, listing_id)
    if shortlist:
        by_position = {row["position"]: row for row in rows}
        rows = [by_position[p] for p in shortlist if p in by_position]
    blocks = []
    for row in rows[:max_images]:
        path = Path(row["local_path"]) if row["local_path"] else None
        if not path or not path.exists():
            continue
        blocks.append(provider.image(path))
    return blocks


def version(cfg, preferences: str) -> str:
    """The key that decides whether a stored verdict is still current.

    A hash of the rubric, the preferences and the settings that shape the
    answer, in place of a version typed into the config by hand. Rewriting what
    you want, or pointing the pass at another model, is exactly as much of a
    change as bumping a number - and nobody remembers to bump a number.
    """
    return passes.digest(SYSTEM_PROMPT + "\n\nBuyer's preferences:\n\n" + preferences,
                         passes.settings("score", cfg))


class Scorer:
    """Pass 3, through the same seam as the two reading passes.

    It asks a provider rather than the Anthropic SDK directly, so `provider` in
    [scoring] is a real setting: the pass that decides can be put behind a
    different model from the two that feed it.
    """

    def __init__(self, cfg, preferences: str, client=None,
                 home_plz: str | None = None) -> None:
        self.cfg = cfg
        self.preferences = preferences
        self.home_plz = home_plz
        self.engine = client or provider.get(cfg.provider)
        self.version = version(cfg, preferences)

    def build_request(self, conn, row) -> provider.Request:
        comparables = db.comparable_stats(conn, row)
        worked_out = derived.summarise(conn, row, self.home_plz)
        found = db.extractions_for(conn, row["id"])
        shortlist = (found.get("photos", {}).get("data", {}) or {}).get("shortlist")
        return self._request([
            provider.text(listing_to_text(row, comparables, worked_out, found)),
            *_image_blocks(conn, row["id"], self.cfg.max_images, shortlist),
        ])

    def template_request(self) -> provider.Request:
        """The request with placeholders where the listing's own facts go.

        Rendered by the same code as a real one, from a listing whose every
        field is a stand-in, so the shape cannot drift from what is sent. A
        real request leaves out the lines for facts the ad does not state, and
        carries up to `max_images` photographs rather than one.
        """
        return self._request([
            provider.text(listing_to_text(PLACEHOLDER_ROW, PLACEHOLDER_COMPARABLES,
                                          PLACEHOLDER_WORKED_OUT, PLACEHOLDER_FOUND)),
            provider.image_stub("{photo, base64 - one image per shortlisted photo, "
                                "up to max_images}"),
        ])

    def _request(self, blocks: list[dict]) -> provider.Request:
        return provider.Request(
            # The rubric and preferences are identical for every listing in a
            # run, so they sit in a cached system prefix and the listing goes in
            # the user turn.
            system=SYSTEM_PROMPT + "\n\nBuyer's preferences:\n\n" + self.preferences,
            blocks=blocks,
            schema=ListingScore,
            model=self.cfg.model,
            effort=self.cfg.effort,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
        )

    def score_listing(self, conn, row) -> dict:
        reply = self.engine.complete(self.build_request(conn, row))
        return {
            **reply.data,
            "model": reply.model or self.cfg.model,
            "prompt_version": self.version,
            "content_hash": row["content_hash"],
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
        }


# A listing made entirely of stand-ins, for showing the shape of a request.
# Every key `listing_to_text` reads is here, so a field added there without a
# placeholder shows up as a KeyError in the test rather than as a line quietly
# missing from the preview.
PLACEHOLDER_ROW = {
    "id": "{id}", "title": "{title}", "price_eur": "{price}", "price_kind": "{vb|fixed|free}",
    "make": "{make}", "model": "{model}", "bike_type": "{type}",
    "first_reg_date": "{first registration}", "model_year": "{model year}",
    "km": "{km}", "hp": "{hp}", "ccm": "{ccm}", "owners": "{owners}",
    "inspection_until": "{HU until}", "condition": "{condition}",
    "damaged": "{damaged: yes, or line absent}",
    "full_service_hist": "{service history: yes, or line absent}",
    "color": "{colour}", "drive_type": "{final drive}", "transmission": "{transmission}",
    "fuel_type": "{fuel}", "seller_type": "{seller}", "location": "{location}",
    "posted_at": "{posted}", "view_count": "{views}",
    "equipment_json": json.dumps(["{equipment, comma separated}"]),
    "attributes_json": json.dumps({"{other attribute}": "{value}"}),
    "description": "{description, verbatim}",
    "content_hash": "{content hash}",
}
PLACEHOLDER_COMPARABLES = {
    "sample_size": "{n}", "price_p25": "{p25}", "price_median": "{median}",
    "price_p75": "{p75}", "km_median": "{median km}",
}
PLACEHOLDER_WORKED_OUT = {
    "km_per_year": "{km/year}", "age_years": "{age}",
    "hu_months_left": "{months of HU left, or how long ago it expired}",
    "distance_km": "{distance}", "days_on_market": "{days on market}",
    "price_drop": ("{drop EUR}", "{drop %}"), "photo_count": "{photo count}",
}
PLACEHOLDER_FOUND = {
    "text": {"data": {
        "summary": "{pass 1 summary}",
        "recent_work": [{"what": "{work done}", "when": "{when}"}],
        "known_faults": ["{faults admitted}"], "red_flags": ["{wording noted}"],
        "modifications": ["{modifications}"], "included_extras": ["{included}"],
        "selling_reason": "{reason for selling}",
        "questions_to_ask": ["{what the ad does not say}"],
    }},
    "photos": {"data": {
        "condition_summary": "{pass 2 condition summary}",
        "visible_issues": ["{visible problems}"], "positives": ["{visible care}"],
        "coverage_gaps": ["{not shown}"], "photo_quality": "{photo quality}",
        "shortlist": ["{shortlisted positions}"],
    }},
}


def score_pending(conn, cfg, home_plz: str | None = None,
                  still_enabled=None) -> list[dict]:
    """Score every listing that needs it. Returns the scores written.

    `still_enabled` is asked before each listing, so turning scoring off while a
    run is in progress stops it at the next listing instead of at the end of the
    queue. Every listing is an API call, so that is the difference between one
    more and two hundred more.
    """
    if not cfg.enabled:
        log.info("scoring is disabled in the config; nothing was scored")
        return []

    preferences = load_preferences(cfg.preferences_file)
    # Nothing about a listing changes when you rewrite what you are looking for,
    # so a changed preferences file has to mark the verdicts itself.
    marked = db.note_preferences(conn, preferences)
    if marked:
        log.info("preferences.md changed - %s listing(s) marked for re-scoring", marked)

    rows = db.unscored_listings(conn, cfg.rescore_on_change, version(cfg, preferences),
                                cfg.max_per_run)
    if not rows:
        return []
    # Built after the queue is read: with nothing to score there is no reason to
    # want an API key, and a run that only scrapes must not fail for want of one.
    scorer = Scorer(cfg, preferences, home_plz=home_plz)
    written = []

    for row in rows:
        if still_enabled is not None and not still_enabled():
            log.warning("scoring was switched off mid-run - stopping after %s of %s "
                        "listing(s); the rest are still queued", len(written), len(rows))
            break
        try:
            score = scorer.score_listing(conn, row)
        except provider.ProviderError as exc:
            log.error("scoring failed for %s: %s", row["id"], exc)
            continue
        except Exception as exc:
            log.error("scoring failed for %s: %s", row["id"], exc, exc_info=True)
            continue
        db.add_score(conn, row["id"], score)
        conn.commit()
        written.append({**score, "listing_id": row["id"], "title": row["title"],
                        "url": row["url"], "price_eur": row["price_eur"],
                        "ignored": bool(row["ignored"])})
        log.info("scored %s: overall=%s fit=%s value=%s", row["id"], score["overall"],
                 score["fit"], score["value"])

    return written
