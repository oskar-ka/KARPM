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

### Delisting is detected by elimination

`mark_delisted()` takes the set of ad ids seen during a run and flags anything
previously active for that search but absent now: `is_active = 0` plus a
`delisted_at` timestamp. The row stays.

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

## Durability

`connect()` sets `journal_mode=WAL` and `synchronous=NORMAL` — a good trade on
an SD card, and the database survives the Pi losing power mid-write. Foreign
keys are on, so deleting a listing takes its images, history and scores with it.

## Changing the schema

`schema.sql` is applied with `CREATE TABLE IF NOT EXISTS` on every start, so
adding a *new table* is free. Adding a *column to an existing table* needs an
`ALTER TABLE` — `CREATE TABLE IF NOT EXISTS` will not do it for a table that
already exists. The `listing_current` view is dropped and recreated on every
init, so changing it only means editing `schema.sql`.
