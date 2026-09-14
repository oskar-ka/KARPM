# KARPM

**K**leinanzeigen**r**elevanz**p**ruefungs**m**aschine — scrapes motorcycle
listings from Kleinanzeigen, stores them locally with full history, scores each
one with Claude against your written preferences, and emails you the good ones.

Built to run unattended on a Raspberry Pi.

```
searches (URLs you paste)
        │
        ▼
   scrape  ──►  SQLite  ──►  read it  ──►  score  ──►  email
   twice/day    + images     pass 1: text  pass 3:     instant alerts
                + history    pass 2: photos  1-5       + daily digest
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

A listing vanishing from the search results is not treated as proof it sold.
Results get re-ranked, `max_pages` caps how deep a run goes, and a price change
can push an ad outside the search's own price filter. So each missing listing
has its own page fetched, and it is delisted only if that page confirms the ad
is gone; anything inconclusive stays active and is re-checked next run.

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

Each search should name the model it targets (`make` / `model` in
`config.toml`). Ads list a *Marke* but no *Modell*, so without it listings
cannot be grouped into the price comparables the scoring prompt depends on.

### Try it before trusting it

`karpm trial` runs the real thing — same fetcher, parsers, storage and image
downloads — against a throwaway database, then stops. **No scoring, no Claude
API calls, no email.**

```bash
karpm trial --url "https://www.kleinanzeigen.de/s-motorraeder-roller/..." \
            --limit 5 --make BMW --model "R 1200 GS"
```

It reports what actually parsed, which is the thing worth checking — a scraper
that returns 25 rows of NULLs still exits successfully:

```
SEARCH
  ads on those pages   25   (matched by selector 'article[data-adid]')
  wanted ads skipped   1    (Gesuch - buyers, not sellers)
  listings processed   5  (capped by --limit)

LISTINGS
  id           price      km    EZ   PS       HU  img  title
  3422210980    4000   66976  2004   98  2028-09   12  BMW R 1200 GS

FIELD COVERAGE  (5 listing(s))
  km                  4/5   ████████████████      MISSING: 3510866610
  owners              0/5                         not stated: ...

IMAGES
  downloaded           43   (18.2 MB, 5 listing(s))

Scoring and email were not run - no Claude API calls, nothing sent.
VERDICT: problems above - check MISSING fields and warnings
```

Fields the site simply does not provide are marked *not stated*; fields that
should have parsed and did not are marked **MISSING**, with the ad URLs so you
can open one and look. It exits non-zero when a required field is missing, so
it works in a cron healthcheck too. Add `--show-prompt` to see exactly what
Claude would be sent for the first listing.

Start small (`--limit 5`) — every listing is a real request to a real site.

### Inspecting a single page

Kleinanzeigen's markup is not a stable API. To look at one page in detail:

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
karpm trial --url ... # dry run: scrape and parse only, no scoring or email
karpm run             # scrape, read, score, and send any instant alerts
karpm digest          # send the digest (--dry-run to see what would go out)
karpm daemon          # run continuously on the configured schedule
karpm web             # the web UI on http://127.0.0.1:8080
```

Other commands: `trial`, `scrape`, `extract`, `score`, `images`, `stats`, `top`, `score-one`,
`probe`, `raw`.

If a run stalls or comes back empty, `karpm raw "<url>"` makes one request with
no retries or backoff and prints exactly what the server returned — status,
redirects, size, and whether the body looks like a block page.

`karpm score-one <id> --show-prompt` prints the exact prompt for a listing
without calling the API — the fastest way to tune `preferences.md`.

On the Pi, install `deploy/karpm.service` and `deploy/karpm-web.service` (edit
the paths in both), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now karpm karpm-web
journalctl -u karpm -f
```

## The web UI

`karpm web` serves a page with the daemon's status, a sortable and filterable
table of every listing, a page per listing with its photos, price history and
scores, and forms for the searches, `preferences.md` and every setting in
`config.toml` — a field each, with a note on what it does. Buttons queue a
scrape, a digest, a reading or a re-score, and pause the schedule.

A listing's page shows what passes 1 and 2 found in panels of their own, naming
the model that said it, and outlines the photos pass 2 shortlisted. The findings
are kept apart from the parsed fields on purpose: mixed in with the mileage read
off the ad, a model's reading would be indistinguishable from something checked.

Settings are edited on the line they already occupy in `config.toml`, so the
comments you have written there survive a save, and a value that would stop
KARPM from starting is refused before anything is written.

It is a separate process that shares only the database with the daemon, so the
UI holds no privilege the daemon has, and restarting either one leaves the other
alone.

It binds to localhost and **has no login**. To use it from another device,
either tunnel in — nothing is exposed, and it works from outside the house too:

```bash
ssh -N -L 8080:localhost:8080 pi@raspberrypi.local   # then http://localhost:8080
```

or open it to your own network, which is convenient and hands the controls to
everyone on that Wi-Fi:

```bash
karpm web --lan      # prints the address to open, e.g. http://192.168.1.42:8080
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

## Three passes

A listing is looked at by three model calls, each configurable separately — its
own model, its own provider, its own switch.

**Pass 1** (`[extract_text]`) reads the German description and writes down what
the seller claims: work done, faults admitted, what is included, what an ad this
detailed is conspicuously not saying. **Pass 2** (`[extract_photos]`) looks at
the photos, reports what is visible, and shortlists the few worth a second look
— a gallery of twenty is rarely twenty pieces of evidence, and the expensive
model should not spend its attention on the near-duplicates. Both are reading
jobs a cheap model does well, and neither ever writes into a listing's own
fields: what they find is stored beside it and reaches pass 3 marked as claims.

**Pass 3** (`[scoring]`) is the one that decides, and gets the better model. It
gets a structured verdict out of Claude: `overall`, `fit` (against your
preferences) and `value` (for money), all 1–5, plus an estimated fair price, a
headline, reasoning, pros, cons and red flags. Everything is stored, so scores
stay comparable and sortable across months.

Its prompt includes price percentiles for the same model **from your own
database**, so "good value" is measured against what you are actually seeing
rather than the model's recollection of the market. The shortlisted photos go
with it (`scoring.max_images`, default 2) because rust, crash damage and worn
tyres are visible and not mentioned in the text.

The three go stale for different reasons, so they re-run independently: an
edited description sends a listing back through pass 1, a changed gallery
through pass 2, and either of those — or a bumped `prompt_version` after you
rewrite your preferences — through pass 3.

Only `anthropic` is implemented as a provider. It is a per-pass setting so that
a cheaper model can be put behind pass 1 or 2 later without touching anything
else; pass 1 is text-only by construction, so a provider with no vision can
serve it.

## Rate limiting

Defaults are deliberately slow — 4–9 s between requests, retries with backoff,
`Retry-After` honoured, and the run aborts after three consecutive blocks rather
than hammering. Two runs a day is a few hundred requests, in the range of a
person browsing for an hour. Speeding this up is how you get blocked.

## Documentation

- [`docs/COMMANDS.md`](docs/COMMANDS.md) — every command and flag.
- [`docs/DATABASE.md`](docs/DATABASE.md) — what is stored, how a save works,
  how to query it, and how to browse it without writing SQL.
- [`docs/COMMANDS.md#web`](docs/COMMANDS.md#web) — the web UI in detail.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The committed fixtures are synthetic: they prove the extraction and pipeline
logic, not that the selectors still match the live site. Add real saved pages as
`tests/fixtures/live_*.html` for that.
