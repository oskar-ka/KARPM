"""Scoring listings with Claude.

Each listing is judged against your written preferences and against what the
database has actually seen for the same model, and comes back as a structured
score rather than prose - so the result is sortable, storable, and comparable
across months of listings.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from . import db, derived, images
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


def listing_to_text(row, comparables: dict | None, worked_out: dict | None = None) -> str:
    """Render a listing row as compact facts for the prompt."""
    def fmt(label: str, value, suffix: str = "") -> str | None:
        return f"{label}: {value}{suffix}" if value not in (None, "") else None

    price = row["price_eur"]
    price_str = "not stated"
    if price is not None:
        price_str = f"{price:,} EUR".replace(",", ".")
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
            fmt("Mileage", f"{row['km']:,}".replace(",", ".") if row["km"] else None, " km"),
            fmt("Power", row["hp"], " PS"),
            fmt("Displacement", row["ccm"], " ccm"),
            fmt("Previous owners", row["owners"]),
            fmt("Inspection valid until (HU/TUEV)", row["inspection_until"]),
            fmt("Condition", row["condition"]),
            fmt("Damaged", "yes" if row["damaged"] else None),
            fmt("Full service history", "yes" if row["full_service_hist"] else None),
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
            facts.append(f"{worked_out['km_per_year']:,} km/year".replace(",", "."))
        if worked_out.get("age_years"):
            facts.append(f"{worked_out['age_years']} years old")
        months = worked_out.get("hu_months_left")
        if months is not None:
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


def _image_blocks(conn, listing_id: str, max_images: int) -> list[dict]:
    if max_images <= 0:
        return []
    blocks = []
    for row in db.listing_images(conn, listing_id)[:max_images]:
        path = Path(row["local_path"]) if row["local_path"] else None
        if not path or not path.exists():
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": images.media_type(path),
                "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
            },
        })
    return blocks


class Scorer:
    def __init__(self, cfg, preferences: str, client: anthropic.Anthropic | None = None,
                 home_plz: str | None = None) -> None:
        self.cfg = cfg
        self.preferences = preferences
        self.home_plz = home_plz
        self.client = client or anthropic.Anthropic()

    def score_listing(self, conn, row) -> dict:
        comparables = db.comparable_stats(conn, row)
        worked_out = derived.summarise(conn, row, self.home_plz)
        content: list[dict] = [
            {"type": "text", "text": listing_to_text(row, comparables, worked_out)},
            *_image_blocks(conn, row["id"], self.cfg.max_images),
        ]

        response = self.client.messages.parse(
            model=self.cfg.model,
            max_tokens=4000,
            # The rubric and preferences are identical for every listing in a run,
            # so they sit in a cached system prefix and the listing goes in the
            # user turn.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT + "\n\nBuyer's preferences:\n\n" + self.preferences,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.effort},
            output_format=ListingScore,
        )

        if response.stop_reason == "refusal":
            raise RuntimeError(f"model declined to score listing {row['id']}")

        parsed: ListingScore = response.parsed_output
        return {
            **parsed.model_dump(),
            "model": self.cfg.model,
            "prompt_version": self.cfg.prompt_version,
            "content_hash": row["content_hash"],
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }


def score_pending(conn, cfg, home_plz: str | None = None) -> list[dict]:
    """Score every listing that needs it. Returns the scores written."""
    if not cfg.enabled:
        return []

    preferences = load_preferences(cfg.preferences_file)
    # Nothing about a listing changes when you rewrite what you are looking for,
    # so a changed preferences file has to mark the verdicts itself.
    marked = db.note_preferences(conn, preferences)
    if marked:
        log.info("preferences.md changed - %s listing(s) marked for re-scoring", marked)

    scorer = Scorer(cfg, preferences, home_plz=home_plz)
    rows = db.unscored_listings(conn, cfg.rescore_on_change, cfg.prompt_version, cfg.max_per_run)
    written = []

    for row in rows:
        try:
            score = scorer.score_listing(conn, row)
        except anthropic.APIError as exc:
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
