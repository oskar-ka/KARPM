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

## The order a run works in

1. **Enumerate.** Walk the search result pages only. This yields the search's
   own total, how many pages there are, and each ad's photo count from its
   thumbnail badge.
2. **Classify.** Compare against what is stored: new, price changed, due a
   refresh, or unchanged.
3. **Fetch.** Open the ad pages of the first three groups only. Unchanged ads
   just have `last_seen_at` bumped.
4. **Images**, then **reconcile** anything that was not in the results.

Doing it in this order means a run knows exactly how much work it faces before
starting any of it, and the delisting check in step 4 gets the complete set of
ads the search returned rather than a partial one. A run that stopped early
(`max_pages`, `max_listings`) is marked truncated and skips step 4 entirely —
it never saw the whole search, so an ad's absence proves nothing.

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

**Where the URLs come from.** An ad page lists its photos in up to four places:
the Astro hydration payload (`data.imageDetails.imageList`, on the newer page
shape, where the markup carries only one photo), JSON-LD `ImageObject` blocks,
the gallery `<img>` elements, and sometimes a `Product` block naming one image. All three are read and merged,
keyed on the photo's path so the same image is not collected once per
rendition. Every earlier version of this treated one source as authoritative
and skipped the others, which stored one photo per ad for whichever ads
happened to hit that path.
Anything inside an `article[data-adid]` is skipped — those are the "similar
ads" cards showing other people's bikes.

The search page states each ad's photo count on its thumbnail, so a run that
finds far fewer on the ad page says so rather than quietly fetching a fraction,
and saves the first few such pages into `scrape.dump_dir` so the cause can be
looked at rather than guessed at.

**Renditions.** The CDN serves each photo under a `rule` naming a size and
format: the gallery links `$_59.AUTO`, while the page's own JSON-LD links
`$_59.JPG` for the same image. Not every rendition exists for every photo, so a
404 on one says nothing about the others — the download tries the alternatives
before giving up, and remembers which one worked so a CDN that has dropped a
rendition does not cost a wasted request on every photo. A photo with no working
rendition leaves its row with a NULL `local_path`, and the failure names the ad
it belongs to. After `images.give_up_after_failures` photos of one ad fail in a
row the rest are skipped — an ad with broken images has all of them broken.

Each folder also gets an `ad.txt` naming the listing id, title and URL, so a
directory of JPEGs is not a dead end without the database.

## Talking to the daemon: `commands` and `app_state`

The web UI (`karpm web`) runs as a separate process from the daemon, and the
two never talk directly — they meet in these two tables. That is what lets the
UI ask for a scrape without holding any privilege the daemon has, and why
restarting one never disturbs the other.

**`commands`** is a queue. The UI inserts a row; the daemon claims the oldest
pending one on its next poll, runs it, and writes back what happened.

| Column | Meaning |
|---|---|
| `command` | `scrape`, `digest` or `rescore` — the set is `db.COMMANDS`. |
| `params_json` | Options, e.g. `{"all": true}` to re-score from scratch. |
| `status` | `pending` → `running` → `done` or `failed`. |
| `requested_at`, `started_at`, `finished_at` | Timestamps for each transition. |
| `result` | What the run returned, or the exception if it failed. |

A command left `running` means the daemon died holding it, so
`reset_stale_commands()` marks those `failed` at startup rather than leaving
them to look busy forever. A failed command keeps its row and its error: a
request that went nowhere must not look like one that succeeded.

**`app_state`** is a small key/value table for things that are true *now*
rather than events worth keeping:

| Key | Meaning |
|---|---|
| `heartbeat` | Last time the daemon checked in. The UI calls it dead after five minutes. |
| `paused` | `"1"` while the schedule is paused. Queued commands still run. |
| `next_scrape`, `next_digest` | When the next slot fires. |

The heartbeat is written by its own thread, every 30 seconds, on its own
connection — a scrape holds the main loop for an hour at a time, and a
heartbeat that stopped whenever the daemon was busiest would report it dead
exactly when it was working hardest.

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

`karpm web` is the built-in answer: a listings table you can sort and filter, a
page per listing with its photos, history and scores, and the daemon's status.
See [COMMANDS.md](COMMANDS.md#web).

The tools below open `data/karpm.db` directly, and show every table rather than
the curated view. Reading while the daemon is running is safe — WAL mode allows
readers and a writer at the same time.

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
| 3 | `commands` and `app_state` — the web UI's queue and the daemon's status, added with `karpm web`. |
| 4 | No schema change. Descriptions stored as markup are rewritten as text, and their `content_hash` recomputed. |
| 5 | `listings.parser_version`, `needs_refetch`, `needs_rescore`, `ignored` — the staleness flags. |
| 6 | `listings.color`, `fuel_type`, `drive_type`, `transmission`, `equipment_json`, `plate`, `plate_season` — filled from the syndicated spec block. `PARSER_VERSION` 2. |

## Two kinds of fact

A row holds two different things, and the listing page and the scoring prompt
both keep them apart.

The listing page shows them as three panels — **specifications**, **derived
figures**, **miscellaneous figures** — and the first two show the same rows for
every listing, whether or not the ad filled them in. A row that vanishes when it
is empty makes two listings impossible to compare, and hides the fact that the
ad never said. A specification that is missing says "not stated"; a derived
figure says what it would have needed.

**Read off the page** are the typed columns and `attributes_json`. The typed
columns are what can be sorted and filtered on; `attributes_json` is the raw
`{label: value}` capture behind them, kept so that a label Kleinanzeigen starts
emitting is noticed rather than dropped. A raw attribute is listed separately only when it has no field at all;
`parse.fields.mapped_column()` decides that, and the page and the prompt share
it, which is what keeps them from drifting apart as they once did. When a label
*does* map somewhere but its value would not parse, the field's own row shows
what the ad said — "HU: Neu" appears against `HU until`, not as "not stated",
because the ad did say something and reporting otherwise loses it twice.

**Worked out** is `karpm/derived.py`: km per year, age, months of HU left, days
on the market, the total price cut, distance from `home_plz`. None of it needs
another request, and each is closer to what a person actually judges than the
fields it comes from. Every one may be None, meaning "cannot say" — and is left
off rather than shown as a zero, which would read as a fact.

### The block inside the description

An ad cross-posted from mobile.de carries a spec sheet in its description text:
owner count, final drive, colour, an equipment list. `parse/syndicated.py` lifts
that into attributes and leaves the seller's own words behind.

The risk is eating a description that was never a block. A seller listing
"Reifen: neu / Kette: neu / Bremsen: neu" has written three label lines, and
cutting those out would lose exactly what is worth scoring. So a run of label
lines is not enough on its own: something only mobile.de writes has to be there
too — its attribution footer, the `Ausstattung` heading, or one of its own
labels.

Where the block and the page disagree, the page wins, since it is structured at
the source — except when the block is more specific. "Erstzulassung: 2004"
against "8/2004" is eight months vaguer, and that feeds straight into the bike's
age; "Art: Motorräder" is true of every ad in a motorcycle search, where the
block's "Enduro/Reiseenduro" says something.

## Going stale without changing

A listing can stop being trustworthy without the ad itself moving. The parser
changed and would now read the page differently; `preferences.md` changed, so
the verdict was reached against something you no longer want; or you looked at a
5/5 and decided it is not for you. None of that shows up as a price drop or an
edit, so it is recorded on the row rather than left to be noticed.

| Column | Set when | Cleared when |
|---|---|---|
| `parser_version` | Every save, to `db.PARSER_VERSION`. | — |
| `needs_refetch` | `parser_version` is behind, or you ask on the listing page. | The page is read again. |
| `needs_rescore` | `preferences.md` changes, `needs_refetch` is set, or you ask. | The listing is scored. |
| `ignored` | You dismiss it in the web UI. | You take it back. |

Two rules make these behave:

- **A listing waiting to be re-fetched is not scored.** Its stored text is known
  to be out of date, so a verdict on it buys an answer about text that is about
  to be replaced. `needs_refetch` holds it back until the scrape catches up.
- **`ignored` suppresses email, not scoring.** The listing keeps a current
  verdict — you can still open it and see what the model thought — it simply
  never reaches the digest or an instant alert.

`preferences.md` is watched by hashing it into `app_state.preferences_hash`.
The first sight of a file is not a change, or every new database would re-score
itself on day one. After that, any edit marks every active listing, which is
what makes the next scoring run redo them — and it costs credits, so both the
web UI and the log say how many were marked.

Marking everything is deliberately blunt. There is no way to tell which verdicts
a preferences edit would actually change without asking the model, which is the
expensive thing you were trying to decide about.

### Data migrations

Version 4 changed no columns. Two of the three parsing layers take the
description from a payload that carries it as HTML — `<br />` per line, entities
for punctuation — and it was being stored as it stood, so ads scraped from the
newer Kleinanzeigen pages held markup in the database, in the scoring prompt and
on the page. `db.repair_descriptions()` rewrites those rows once, on the first
open of an older database, and says how many it changed.

It recomputes `content_hash` for each row it touches, which means those listings
are scored again on the next scoring run — their existing scores were made from
the mangled text. Leaving the hash stale would only postpone that: the next
refresh of the ad would hash the clean text, record an `edited` event that never
happened, and re-score anyway.

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
