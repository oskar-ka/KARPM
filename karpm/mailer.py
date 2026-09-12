"""Email output via Resend.

Two shapes: an instant alert for a single standout listing, and a periodic
digest. Both are plain HTML with inline styles, because email clients ignore
everything else.
"""

from __future__ import annotations

import html
import json
import logging

import requests

from . import db

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

SCORE_COLORS = {5: "#0b7a3b", 4: "#3f8f4a", 3: "#8a8a1f", 2: "#a35a17", 1: "#9b2226"}


class MailError(RuntimeError):
    pass


def send(cfg, api_key: str, subject: str, html_body: str, text_body: str) -> str | None:
    if not cfg.enabled:
        log.info("email disabled, would have sent: %s", subject)
        return None
    if not api_key:
        raise MailError("RESEND_API_KEY is not set")
    if not cfg.to_addresses:
        raise MailError("no recipients configured (email.to_addresses)")

    response = requests.post(
        RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": cfg.from_address,
            "to": cfg.to_addresses,
            "subject": f"{cfg.subject_prefix} {subject}".strip(),
            "html": html_body,
            "text": text_body,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise MailError(f"Resend returned {response.status_code}: {response.text[:500]}")
    return response.json().get("id")


# --- rendering --------------------------------------------------------------

def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _thousands(value: int) -> str:
    """German thousands separator: 4500 -> '4.500'."""
    return f"{value:,}".replace(",", ".")


def _price(value: int | None, kind: str | None = None) -> str:
    if value is None:
        return "price not stated"
    return f"{_thousands(value)} €" + (" VB" if kind == "vb" else "")


def _specs(row) -> str:
    bits = []
    if row["first_reg_year"]:
        bits.append(f"EZ {row['first_reg_year']}")
    if row["km"] is not None:
        bits.append(f"{_thousands(row['km'])} km")
    if row["hp"]:
        bits.append(f"{row['hp']} PS")
    if row["owners"]:
        bits.append(f"{row['owners']} Halter")
    if row["location"]:
        bits.append(str(row["location"]))
    return " · ".join(bits)


def _thumb_url(conn, listing_id: str) -> str | None:
    rows = db.listing_images(conn, listing_id)
    return rows[0]["url"] if rows else None


def _listing_card(conn, row) -> str:
    """One listing as an HTML card. `row` comes from the listing_current view."""
    score = row["overall"]
    color = SCORE_COLORS.get(score, "#555")
    thumb = _thumb_url(conn, row["id"])
    pros = json.loads(row["pros_json"] or "[]")
    cons = json.loads(row["cons_json"] or "[]")
    flags = json.loads(row["red_flags_json"] or "[]")

    fair = ""
    if row["fair_price_eur"] and row["price_eur"]:
        delta = row["fair_price_eur"] - row["price_eur"]
        amount = _thousands(abs(delta))
        if delta > 0:
            fair = (f'<span style="color:#0b7a3b;font-weight:600;">{amount} € under '
                    f'estimated fair price</span>')
        elif delta < 0:
            fair = f'<span style="color:#9b2226;">{amount} € over estimated fair price</span>'

    def bullets(items, symbol, colour):
        if not items:
            return ""
        rendered = "".join(
            f'<li style="margin:2px 0;">{_esc(i)}</li>' for i in items[:4]
        )
        return (f'<ul style="margin:6px 0 0 0;padding-left:18px;color:{colour};'
                f'font-size:13px;list-style-type:\'{symbol} \';">{rendered}</ul>')

    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #e2e2e2;border-radius:8px;margin-bottom:14px;background:#fff;">
  <tr>
    <td width="140" valign="top" style="padding:12px;">
      {'<img src="' + _esc(thumb) + '" width="128" style="border-radius:6px;display:block;" alt="">' if thumb else ''}
    </td>
    <td valign="top" style="padding:12px 12px 12px 0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">
      <div style="display:inline-block;background:{color};color:#fff;border-radius:4px;
                  padding:2px 8px;font-weight:700;font-size:13px;">{_esc(score)}/5</div>
      <span style="color:#666;font-size:12px;margin-left:8px;">
        fit {_esc(row['fit'])}/5 · value {_esc(row['value'])}/5</span>
      <div style="margin:6px 0 2px;font-size:16px;font-weight:600;">
        <a href="{_esc(row['url'])}" style="color:#14507d;text-decoration:none;">{_esc(row['title'])}</a>
      </div>
      <div style="font-size:15px;font-weight:600;margin:2px 0;">
        {_price(row['price_eur'], row['price_kind'])}
        <span style="font-weight:400;font-size:13px;margin-left:6px;">{fair}</span>
      </div>
      <div style="color:#555;font-size:13px;margin:2px 0 6px;">{_esc(_specs(row))}</div>
      <div style="font-size:13px;color:#333;">{_esc(row['reasoning'])}</div>
      {bullets(pros, '+', '#0b7a3b')}
      {bullets(cons, '−', '#8a5a00')}
      {bullets(flags, '!', '#9b2226')}
    </td>
  </tr>
</table>"""


def _wrap(title: str, intro: str, cards: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;padding:16px;background:#f5f5f4;">
<div style="max-width:680px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">
  <h1 style="font-size:19px;margin:0 0 4px;">{_esc(title)}</h1>
  <p style="color:#666;font-size:13px;margin:0 0 16px;">{_esc(intro)}</p>
  {cards}
  <p style="color:#999;font-size:11px;margin-top:20px;">
    KARPM · scores are generated by Claude from the listing text and photos and are
    a first filter, not an inspection.</p>
</div></body></html>"""


def _plain(rows) -> str:
    lines = []
    for row in rows:
        lines.append(
            f"[{row['overall']}/5] {row['title']} - {_price(row['price_eur'], row['price_kind'])}\n"
            f"    {_specs(row)}\n    {row['reasoning']}\n    {row['url']}\n"
        )
    return "\n".join(lines)


# --- public API -------------------------------------------------------------

def send_instant_alert(conn, cfg, api_key: str, listing_id: str) -> str | None:
    row = conn.execute("SELECT * FROM listing_current WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        return None
    subject = f"{row['overall']}/5 · {row['title']} · {_price(row['price_eur'], row['price_kind'])}"
    body = _wrap(
        "Strong match just listed",
        f"Found {row['posted_at'] or 'just now'} · scored {row['overall']}/5",
        _listing_card(conn, row),
    )
    provider_id = send(cfg, api_key, subject, body, _plain([row]))
    db.record_notification(conn, listing_id, "instant", provider_id)
    conn.commit()
    return provider_id


def digest_candidates(conn, cfg) -> list:
    """Scored, still-active listings above the cutoff that were never digested."""
    return conn.execute(
        """
        SELECT lc.* FROM listing_current lc
        LEFT JOIN notifications n ON n.listing_id = lc.id AND n.kind = 'digest'
        WHERE lc.is_active = 1 AND lc.overall IS NOT NULL
          AND lc.overall >= ? AND n.id IS NULL
        ORDER BY lc.overall DESC, lc.value DESC, lc.first_seen_at DESC
        LIMIT ?
        """,
        (cfg.digest_min_score, cfg.digest_max_listings),
    ).fetchall()


def send_digest(conn, cfg, api_key: str) -> str | None:
    rows = digest_candidates(conn, cfg)
    if not rows and cfg.skip_empty_digest:
        log.info("no new listings above score %s, skipping digest", cfg.digest_min_score)
        return None

    best = rows[0]["overall"] if rows else 0
    subject = (f"{len(rows)} new listing{'s' if len(rows) != 1 else ''}"
               f"{f', best {best}/5' if rows else ''}")
    cards = "".join(_listing_card(conn, row) for row in rows) or \
        '<p style="color:#666;">Nothing new above the score cutoff.</p>'
    body = _wrap("New listings", f"Scored at least {cfg.digest_min_score}/5", cards)

    provider_id = send(cfg, api_key, subject, body, _plain(rows))
    for row in rows:
        db.record_notification(conn, row["id"], "digest", provider_id)
    conn.commit()
    return provider_id


def qualifies_for_instant(cfg, score: dict, price_eur: int | None) -> bool:
    """A listing earns an interrupt if it scores at the top or is a clear bargain."""
    if score["overall"] >= cfg.instant_min_score:
        return True
    fair = score.get("fair_price_eur")
    if fair and price_eur and price_eur > 0 and score["overall"] >= 4:
        discount = (fair - price_eur) / fair * 100
        return discount >= cfg.instant_min_bargain_pct
    return False
