# KARPM

**K**leinanzeigen**r**elevanz**p**ruefungs**m**aschine — scrapes motorcycle
listings from Kleinanzeigen, stores them locally with full history, scores each
one with Claude against your written preferences, and emails you the good ones.

Built to run unattended on a Raspberry Pi.

```
searches (URLs you paste)
        │
        ▼
   scrape  ──►  SQLite  ──►  score with Claude  ──►  email
   twice/day    + images      structured 1-5         instant alerts
                + history     verdict per listing    + daily digest
```

## What it collects

Per listing: title, full description, price (and whether it is *VB*/negotiable),
make, model, type, first registration, mileage, power, displacement, previous
owners, HU/TÜV expiry, condition and accident status, service history, seller
type and location, posting date, view count, every photo, and every raw
attribute the ad shows — so a field Kleinanzeigen adds later is not lost.

Every observation is compared against the stored row, so you also get **price
drops, edits, time on market, and when a listing disappears** — which is the
data that tells you what actually sells and at what price.

## Setup

Needs Python 3.10 or newer (3.10 is what Raspberry Pi OS / Ubuntu 22.04 ship).

```bash
git clone <this repo> && cd KARPM
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp config.example.toml config.toml       # searches, schedule, thresholds
cp .env.example .env                     # ANTHROPIC_API_KEY, RESEND_API_KEY
cp preferences.example.md preferences.md # what you actually want — rewrite this
karpm init
```

**Searches** are Kleinanzeigen URLs you build in your browser. Filter by make,
model, radius and price there, paste the resulting URL into `config.toml`, and
the scraper paginates from it. No filter logic to keep in sync with the site.

**`preferences.md` is the most important file here.** It is handed to Claude with
every listing; vague preferences produce scores that feel arbitrary. Say what you
would tell a friend looking on your behalf, including what you *don't* care about.

### Verify the parsers against the live site first

Kleinanzeigen's markup is not a stable API. Before trusting a run:

```bash
karpm probe --url "https://www.kleinanzeigen.de/s-motorraeder-roller/..." --save tests/fixtures/live_search.html
karpm probe --url "https://www.kleinanzeigen.de/s-anzeige/..." --save tests/fixtures/live_detail.html
```

`probe` prints exactly what the parsers extracted and which selector matched,
and touches nothing else. Saved `live_*.html` files are picked up automatically
by the test suite (`pytest`) as regression fixtures, and are gitignored.

The parsers try JSON-LD, then several CSS selector shapes, then meta tags, then
regex, and write a `parse_warnings` list into every row — so a markup change
shows up as a warning in the data rather than silently becoming NULL. A failed
re-parse never overwrites a field that parsed correctly before.

Kleinanzeigen rebuilt the site in Astro with Tailwind, so the class names carry
no meaning and churn. The search parser therefore identifies fields by the shape
of their text — a price looks like `1.250 € VB`, a location like `80331 München`
— and reads each ad's embedded `ld+json` block for title, snippet and photo.
That survives a restyle in a way that class names do not.

Wanted ads (`Gesuch`) are skipped rather than stored: they are people looking to
buy, and they would skew the price comparables that the scoring prompt uses.

## Running

```bash
karpm run             # scrape, then score and send any instant alerts
karpm digest          # send the digest (--dry-run to see what would go out)
karpm daemon          # run continuously on the configured schedule
```

Other commands: `scrape`, `score`, `images`, `stats`, `top`, `score-one`, `probe`.

`karpm score-one <id> --show-prompt` prints the exact prompt for a listing
without calling the API — the fastest way to tune `preferences.md`.

On the Pi, install `deploy/karpm.service` (edit the paths), then:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now karpm
journalctl -u karpm -f
```

## Email

Two shapes, both via Resend:

- **Instant alert** — a listing scoring 5/5, or a 4/5 at least 20% under the
  model's estimated fair price. Good bikes sell the same day; this is the point
  of running twice a day rather than weekly.
- **Digest** — everything new at or above `digest_min_score`, once a morning.
  A listing already sent as an instant alert is not repeated in the digest, and
  an empty digest is skipped.

Thresholds live under `[email]` in `config.toml`.

## Scoring

Each listing gets a structured verdict from Claude: `overall`, `fit` (against
your preferences) and `value` (for money), all 1–5, plus an estimated fair
price, a headline, reasoning, pros, cons and red flags. Everything is stored, so
scores stay comparable and sortable across months.

The prompt includes price percentiles for the same model **from your own
database**, so "good value" is measured against what you are actually seeing
rather than the model's recollection of the market. Photos are sent too
(`scoring.max_images`, default 2) because rust, crash damage and worn tyres are
visible and not mentioned in the text.

Listings are re-scored when their price or text changes, or when you bump
`prompt_version` after editing your preferences.

## Rate limiting

Defaults are deliberately slow — 4–9 s between requests, retries with backoff,
`Retry-After` honoured, and the run aborts after three consecutive blocks rather
than hammering. Two runs a day is a few hundred requests, in the range of a
person browsing for an hour. Speeding this up is how you get blocked.

## Documentation

- [`docs/DATABASE.md`](docs/DATABASE.md) — what is stored, how a save works,
  and how to query it.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The committed fixtures are synthetic: they prove the extraction and pipeline
logic, not that the selectors still match the live site. Add real saved pages as
`tests/fixtures/live_*.html` for that.
