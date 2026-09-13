# How the database works

Everything KARPM collects lives in one SQLite file plus a folder of images:

```
data/
├── karpm.db          ← 7 tables + 1 view
└── images/
    └── 2847612345/   ← one folder per ad
        ├── 00_27e7b6bfddf6.jpg
        └── 01_27e7b6bfddf6.jpg
```

SQLite was chosen over a server database on purpose: a few years of motorcycle
listings is tens of thousands of rows, which is small. There is no daemon to
keep alive on the Pi, and a backup is `cp data/karpm.db somewhere-else`.

## The central idea: mutable state, append-only history

| Table | Rows | Behaviour |
|---|---|---|
| `listings` | one per ad | **Overwritten** — always the current state |
| `listing_history` | many per ad | **Append-only** — how it got that way |
| `scores` | many per ad | **Append-only** — every verdict ever given |
| `images` | many per ad | metadata; the bytes live on disk |
| `notifications` | one per ad per kind | so you are never mailed the same thing twice |
| `runs` | one per scrape/score/digest | audit trail |
| `searches` | one per `config.toml` entry | synced at startup |

`listings` answers *"what is this bike, right now?"*. The log tables answer
*"what happened to it?"*. If an ad is cut in price three times, `listings`
holds the latest number and `listing_history` holds all four with timestamps.

Nothing is ever deleted. A sold bike with a known final price and a known
time-on-market is the most valuable row in the database — it is real evidence
about what things sell for, as opposed to what people ask for.

## What happens on every save

Every ad goes through `db.upsert_listing()` (`karpm/db.py`). It has three jobs.

### 1. New or known?

The Kleinanzeigen ad id is the primary key, so re-seeing an ad updates it in
place instead of creating a duplicate.

### 2. Record what changed

Before writing, the incoming data is compared with the stored row and a history
row is appended. A real two-run example — ad #1 was reduced, ad #2 was taken
down:

```
listing_id   event            prev    new    observed_at
2847612345   created                  5900   ...07:06:45   ← run 1
2847698888   created                  2100   ...07:06:45   ← run 1
2847612345   price_change     5900    5400   ...07:06:45   ← run 2
2847698888   delisted                        ...07:06:45   ← run 2
```

Events are `created`, `price_change`, `edited`, `relisted` and `delisted`.
Price changes are caught by comparing the number. Text edits are caught with
`content_hash`, a SHA-256 of title + description + price, so a rewritten
description registers even when the price is unchanged.

### 3. Never lose good data to a bad parse

```python
merged = {c: (values[c] if values[c] is not None else existing[c]) for c in LISTING_COLUMNS}
```

If Kleinanzeigen changes its markup and mileage stops parsing, that field
arrives as `None` and the merge keeps the `18400` already stored. A parser
regression degrades to stale data, never to data loss. Pinned by
`test_parse_failure_does_not_null_out_good_data`.

Each row also carries `parse_warnings`, so a markup change shows up *in the
data* as `["missing:km"]` rather than silently becoming NULL.

### Delisting requires evidence, not absence

An ad vanishing from the search results does **not** mean it was sold. Results
get re-ranked, `max_pages` caps how deep the run goes, and a price change can
push an ad outside the search's own price filter. Treating absence as deletion
loses live listings.

So absence only starts an investigation. `reconcile_missing()` fetches each
missing listing's own page and classifies it:

| Outcome | What it means | What happens |
|---|---|---|
| `gone` | 404, or a redirect away from `/s-anzeige/`, or a page that does not parse as an ad *and* says the ad was removed | `is_active = 0`, `delisted_reason = 'verified_gone'`, history row |
| `live` | The page still parses as this advert | Stays active; the page in hand is stored, so a price change gets picked up |
| `unknown` | Blocked, unreadable, network error, or a different ad at that URL | **Nothing.** Stays active, re-checked next run |

Order matters in that classification: a page that parses as a real advert is
live no matter what its text says. Sellers write "das Zubehör ist nicht mehr
verfügbar" in perfectly live listings, and matching the raw text alone would
delete them.

Three columns track the state in between:

| Column | Meaning |
|---|---|
| `missing_since` | First run in which it was absent from the results |
| `missing_count` | How many consecutive runs it has been absent |
| `last_verified_at` | Last time its own page was fetched to check |

`recheck_missing_after_hours` stops a listing that is permanently outside the
search filter from being re-fetched on every single run, and
`max_delist_checks` caps how many of these extra requests one run may spend —
anything over the cap keeps its active status and waits for the next run.

Setting `verify_delisting = false` restores the old assume-it-is-gone
behaviour, which records `delisted_reason = 'assumed'`.

## Scores

Append-only, with three keys that make re-scoring sane:

```
listing_id      2847612345
overall         5          fit  5      value  4
fair_price_eur  6800
content_hash    9b3a15bf…   ← the listing state this verdict refers to
prompt_version  v1          ← the preferences that produced it
input_tokens    4820        output_tokens  512
```

- `content_hash` — a listing whose price dropped no longer matches its stored
  score, so it is re-scored automatically.
- `prompt_version` — bump it in `config.toml` after rewriting `preferences.md`
  and everything is re-scored against the new criteria. Old verdicts remain, so
  you can see how your own taste shifted.
- token counts — your actual spend, per listing.

The `listing_current` view joins each listing to its newest score. The emails
and `karpm top` read it, so nobody hand-writes the "latest score" subquery.

## Images

The `images` table holds the URL, local path, SHA-256 and byte size; the file
itself goes to `data/images/<ad_id>/<position>_<hash>.jpg`. Keeping the bytes
means a listing stays reviewable after it is taken down — which is exactly when
you most want to compare it against what is on the market now.

## Querying it

It is a plain SQLite file, so `sqlite3`, DB Browser for SQLite or pandas all
work.

```sql
-- worth contacting someone about
SELECT overall, price_eur, km, first_reg_year, title, url
FROM listing_current WHERE is_active = 1 AND overall >= 4
ORDER BY overall DESC, value DESC;

-- bikes that have been sitting, and dropping
SELECT l.title, h.prev_price_eur, h.price_eur,
       julianday('now') - julianday(l.first_seen_at) AS days_listed
FROM listing_history h JOIN listings l ON l.id = h.listing_id
WHERE h.event = 'price_change' ORDER BY h.observed_at DESC;

-- what actually disappears, and at what price (i.e. what sells)
SELECT price_eur, km, first_reg_year,
       julianday(delisted_at) - julianday(first_seen_at) AS days_to_sell
FROM listings WHERE is_active = 0 ORDER BY days_to_sell;

-- parser health: which fields are going missing
SELECT parse_warnings, COUNT(*) FROM listings
WHERE parse_warnings != '[]' GROUP BY parse_warnings ORDER BY 2 DESC;
```

Or use `karpm stats` and `karpm top --min-score 4`.

That third query is the long-term payoff of keeping history. The same data
already feeds the scoring prompt: `db.comparable_stats()` pulls price
percentiles for the model from your own rows, so "good value" is judged against
your real market rather than the model's recollection of one.

## Browsing it without writing SQL

All of these open `data/karpm.db` directly. Reading while the daemon is running
is safe — WAL mode allows readers and a writer at the same time.

**Datasette** — the best fit for a Pi. A local web UI: click a table, sort by
clicking a column, filter with dropdowns, no SQL anywhere.

```bash
pip install datasette
datasette serve data/karpm.db                  # then open http://127.0.0.1:8001
datasette serve data/karpm.db --host 0.0.0.0   # browse from your laptop instead
```

Serving on `0.0.0.0` exposes the database to anyone on your network. On an
untrusted network, leave it on localhost and use an SSH tunnel:
`ssh -L 8001:localhost:8001 pi@raspberrypi`.

**VisiData** — a spreadsheet in the terminal, ideal over SSH. Arrow keys to move,
`Enter` to open a table, `[` / `]` to sort, `/` to search, `q` to go back.

```bash
pip install visidata
vd data/karpm.db
```

**DB Browser for SQLite** — the familiar desktop GUI, if the machine has one.
Its "Browse Data" tab is a plain table view.

```bash
sudo apt install sqlitebrowser
```

**`sqlite3`** — always available, but you have to write SQL. Worth knowing two
dot-commands that make it readable:

```bash
sqlite3 data/karpm.db
sqlite> .mode box
sqlite> .headers on
sqlite> SELECT id, price_eur, km, title FROM listings LIMIT 5;
sqlite> .tables
sqlite> .schema listings
sqlite> .quit
```

`litecli` (`pip install litecli`) is the same thing with autocompletion and
syntax highlighting.

For the common questions there is no need to open the database at all —
`karpm stats` and `karpm top --min-score 4` already answer them.

## Durability

`connect()` sets `journal_mode=WAL` and `synchronous=NORMAL` — a good trade on
an SD card, and the database survives the Pi losing power mid-write. Foreign
keys are on, so deleting a listing takes its images, history and scores with it.

## Schema versions

`PRAGMA user_version` records the schema version; `db.MIGRATIONS` lists columns
added after v1 and `init_db()` applies any that a database is missing. Upgrading
is just running any command — existing rows and their history are preserved.

| Version | Change |
|---|---|
| 1 | Initial schema. |
| 2 | `listings.delisted_reason`, `missing_since`, `missing_count`, `last_verified_at` — added with delisting verification. |

## Changing the schema

`schema.sql` is applied with `CREATE TABLE IF NOT EXISTS` on every start, so
adding a *new table* is free. Adding a *column to an existing table* needs two
edits: the column in `schema.sql` (for fresh databases) **and** an entry in
`db.MIGRATIONS` (for existing ones) — `CREATE TABLE IF NOT EXISTS` will not add
a column to a table that already exists. Bump `SCHEMA_VERSION` and add a row to
the table above.

Migrations run *before* `schema.sql`, because `schema.sql` recreates the
`listing_current` view and a view can only reference columns that already
exist. The view is dropped and recreated on every init, so changing it only
means editing `schema.sql`.
