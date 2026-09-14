# KARPM command reference

Every command and flag. Run `karpm <command> --help` for the same thing inline.

> Keep this file in step with the CLI. Any change to a command, flag, default or
> exit code belongs here in the same commit.

## Synopsis

```
karpm [-c CONFIG] [-v] <command> [options]
```

## Global options

| Flag | Default | Meaning |
|---|---|---|
| `-c`, `--config PATH` | `config.toml` | Path to the config file. |
| `-v`, `--verbose` | off | Debug logging, including every URL fetched. Use this when something is behaving oddly. |
| `--fast` | see below | Testing pace: short delays between requests. |
| `--polite` | see below | Production pace: the delays in `[scrape]`. |
| `-h`, `--help` | — | Help, at top level or for any command. |

Either pace flag works before or after the command (`karpm --fast trial …` and
`karpm trial --fast` are the same).

### Pacing

The delay between requests is about **rate limiting, not looking human**.
Kleinanzeigen's bot protection reacts to how fast a single IP asks, and the
penalty is a captcha wall for a few hours — which is why the unattended
commands stay slow.

| | Between pages | Between images |
|---|---|---|
| Production (`[scrape]`) | `min_delay_s`–`max_delay_s`, default 4–9 s | `image_min_delay_s`–`image_max_delay_s`, default 0.4–1.2 s |
| Testing (`[trial]`) | same keys, default 0.5–1.5 s | same keys, default 0.1–0.3 s |

Retries are configured too: `scrape.retry_delays_s` (default `[5, 10, 20, 40]`)
is how long to wait before each further attempt at a request worth retrying.
Permanent failures — a 400 or a 404 — are never retried, since the server will
only say the same thing again.

**`trial`, `probe` and `raw` use the testing pace by default**; `scrape`, `run`,
`score`, `digest`, `daemon` and `images` use the production pace. Forcing a
production command fast logs a warning — it is fine for a one-off, but a
schedule running for months at that rate is what would get the Pi blocked.

Images are paced separately because they come from a static CDN rather than the
search backend, and they are the large majority of a run's requests.

`home_plz` is your own postcode. Distance to each listing is worked out from it
and shown to the scoring model; leave it out and distance is simply unknown
rather than guessed. The postcode table is shipped with the package (GeoNames,
CC BY 4.0), so nothing is looked up over the network.

Secrets are **not** in the config file. They are read from the environment, or
from a `.env` file in the working directory: `ANTHROPIC_API_KEY` for scoring and
`RESEND_API_KEY` for email.

## Command summary

| Command | Touches the network? | Costs money? | Sends email? |
|---|---|---|---|
| [`init`](#init) | no | no | no |
| [`raw`](#raw) | yes (one request) | no | no |
| [`trial`](#trial) | yes (scrape) | no | no |
| [`probe`](#probe) | optional | no | no |
| [`scrape`](#scrape) | yes | no | no |
| [`images`](#images) | yes | no | no |
| [`extract`](#extract) | yes (API) | **yes** | no |
| [`extract-one`](#extract-one) | yes (API) | **yes** | no |
| [`score`](#score) | yes (API) | **yes** | **yes** (instant alerts) |
| [`score-one`](#score-one) | yes (API) | **yes** | no |
| [`run`](#run) | yes | **yes** | **yes** |
| [`digest`](#digest) | no | no | **yes** |
| [`daemon`](#daemon) | yes | **yes** | **yes** |
| [`web`](#web) | no (serves locally) | no | no |
| [`stats`](#stats) | no | no | no |
| [`top`](#top) | no | no | no |

---

## `init`

Create the database, apply the schema, and register the searches from
`config.toml`. Safe to re-run: it never drops data.

```bash
karpm init
```

No options. Run it once after editing `config.toml`; every other command does
this implicitly anyway.

---

## `trial`

**Dry run.** Scrapes and parses a search into a throwaway database, downloads
its images, then stops. No scoring, no Claude API calls, no email — this is the
command to use when checking whether the scraper still works.

```bash
karpm trial --url "https://www.kleinanzeigen.de/s-motorraeder-roller/..." \
            --limit 5 --make BMW --model "R 1200 GS"
```

| Flag | Default | Meaning |
|---|---|---|
| `--url URL` | — | Search URL to try. Either this or `--search`. |
| `--search NAME` | `trial` | Use a search already defined in `config.toml` instead of `--url`. |
| `--max-ads N` | `5` | Stop after N ads, walking as many pages as that needs. |
| `--all-ads` | off | Every ad in the search, to the last page. |
| `--all-images` | off | Every photo per ad. Without it, `images.max_per_listing` applies. |
| `--save-pages DIR` | — | Write every search page walked into DIR, including the one that ends the walk. |
| `--db PATH` | `data/trial.db` | Throwaway database. Wiped at the start of each run unless `--keep`. |
| `--image-dir PATH` | `data/trial_images` | Where trial images are written. |
| `--make NAME` | — | Make to record on each listing. Overrides the search's own value. |
| `--model NAME` | — | Model to record, overriding the search's. Ads carry no *Modell* attribute, so without this listings cannot be grouped into price comparables. |
| `--no-images` | off | Skip image downloads (faster, fewer requests). |
| `--keep` | off | Append to the trial database instead of starting clean. |
| `--show-prompt` | off | Also print the scoring prompt for the first listing — without sending it. |

**Exit code 0** if every required field parsed on every listing and no image
download failed; **1** otherwise, so it works as a cron healthcheck.

The report separates two kinds of gap: **MISSING** means a field should have
parsed and did not — a bug worth reporting; *not stated* means the site never
provides it for these ads (motorcycle listings have no `owners` or `condition`).

There is no page flag: pages are walked until the ad limit is met, or until the
search runs out of ads. `--max-ads` and `--all-ads` cannot be combined.

When a walk ends, it says why — the ad limit, a page limit from the config, the
last page offering no next link, or a page that returned nothing parseable. If
it ends before the page count the search itself reported, that is flagged as a
warning, because it is the symptom of a pagination control the parser cannot
follow. `--save-pages DIR` then captures every page walked, and the last file
in it is the one to look at.

**The search pages are read first, then the ads.** Walking the result pages
costs a handful of requests and produces an exact plan before anything else is
fetched:

```
[bmw] plan:
  143 ad(s) in this search across 6 page(s)
  6 page(s) walked, 143 ad(s) collected, 1 wanted ad(s) skipped
  12 new, 3 with a new price, 0 due a refresh, 128 unchanged
  15 ad page(s) and 118 image(s) to fetch
```

Those are counts, not estimates: the search states its own total ("1 - 25 von
143") and each ad's thumbnail carries its photo count. Unchanged ads never have
their page fetched at all.

---

## `raw`

One request, no retries, no backoff — exactly what the server returned. `probe`
and `trial` go through the polite fetcher, which retries and sleeps on anything
suspicious; when *that* is the thing misbehaving, this bypasses it.

```bash
karpm raw --search bmw-r1200gs            # use a search from config.toml
karpm raw "https://www.kleinanzeigen.de/s-motorraeder-roller/..."
karpm raw --search bmw-r1200gs --save page.html
```

| Argument / flag | Default | Meaning |
|---|---|---|
| `url` | — | URL to fetch. Omit when using `--search`. |
| `--search NAME` | — | Fetch the URL of this search from `config.toml` instead of typing it. |
| `--save PATH` | — | Write the body here instead of printing a 600-character preview. |
| `--no-redirects` | off | Do not follow redirects — shows the first response as-is. |

Give it a URL **or** `--search`, not both. An unknown search name exits `2` and
lists the names that are configured.

Reports status, elapsed time, final URL and redirect chain, content type, size,
the header encoding, **which block markers matched** (the reason the fetcher
would back off), the page title, and how many `data-adid` attributes are
present — a healthy search page has one per ad.

Use this first whenever a run stalls or returns nothing.

---

## `probe`

Fetch or open **one** page and dump what the parsers extract from it. Touches
nothing else — no database writes. Use it to inspect a single page in detail
after `trial` points at a problem.

```bash
karpm probe --url "https://www.kleinanzeigen.de/s-anzeige/..." --save ad.html
karpm probe --file ad.html --brief
```

| Flag | Default | Meaning |
|---|---|---|
| `--url URL` | — | Page to fetch. Either this or `--file`. |
| `--file PATH` | — | Parse a saved HTML file instead of fetching. |
| `--save PATH` | — | Write the fetched HTML here (useful for bug reports and fixtures). |
| `--brief` | off | Omit the description from the output. |
| `--kind {auto,search,detail}` | `auto` | Force how the page is parsed. `auto` uses the URL, then ad-page markers. |

Saved pages named `tests/fixtures/live_*.html` are picked up automatically by
the test suite as regression fixtures.

---

## `scrape`

Walk every enabled search in `config.toml`, store what is found, download
images. Stops before scoring and email.

```bash
karpm scrape
```

No options — it takes its searches, page limits and delays from the config.
Unlike `trial` this writes to the **real** database.

Pages and images are paced separately — `min_delay_s`/`max_delay_s` for search
and ad pages, `image_min_delay_s`/`image_max_delay_s` for photos. Photos come
from a static CDN rather than the search backend and are the large majority of
the requests a run makes, so pacing them like search queries multiplies the
runtime without making the run any more polite to the site that matters.
Progress is logged per listing and per batch of images.

A listing that stops appearing in the search results is not assumed to be sold:
its own page is fetched, and it is only delisted if that page confirms the ad is
gone. A listing that is still live but has dropped out of the results (re-ranked,
below the `max_pages` cut, or pushed outside the search's price filter) stays
active and gets refreshed from the page that was just fetched. Inconclusive
checks change nothing and are retried next run. See `verify_delisting`,
`max_delist_checks` and `recheck_missing_after_hours` in `config.toml`, and
[`DATABASE.md`](DATABASE.md) for the detail.

---

## `images`

Download any image whose file is missing. Normally unnecessary — `scrape` does
this — but useful after an interrupted run or a disk mishap.

```bash
karpm images --limit 200
```

| Flag | Default | Meaning |
|---|---|---|
| `--limit N` | `500` | Maximum images to download in one go. |

---

## The three passes

Three separate model calls look at a listing, in this order. Each has its own
section in `config.toml`, its own model, its own `provider`, its own
`prompt_version` and its own `enabled`.

| Pass | Section | What it does | Goes stale when |
|---|---|---|---|
| 1 | `[extract_text]` | Reads the description and writes down what the seller claims: work done, faults admitted, what is included, what to ask about. | The ad's text or price changes, or its prompt does. |
| 2 | `[extract_photos]` | Looks at the photos, says what they show, and shortlists the few worth a second look. | A photo is added, removed or replaced, or its prompt changes. |
| 3 | `[scoring]` | Weighs the hard facts, what passes 1 and 2 found, the shortlisted photos and the comparable prices against `preferences.md`. | The ad changes, `preferences.md` changes, or `scoring.prompt_version` changes. |

All three are edited on the **prompts** page of the web UI — passes 1 and 2 as
the instructions they are given, pass 3 as what you want out of a bike. See
[The prompts page](#the-prompts-page).

They are separate because they are different jobs — a model that is good at
pulling `Reifen neu, Kette bei 40tkm` out of a paragraph need not be the one
you want judging whether a bike looks cared for — and because they go stale for
different reasons. A seller editing the text does not send the photos back
through pass 2.

Passes 1 and 2 never write into a listing's own fields. Their findings are
stored beside it, shown on the listing page under **read from the description**
and **read from the photos**, and given to pass 3 marked as a model's reading
rather than a fact.

`provider` names the API behind a pass. Only `anthropic` is implemented; it is
a setting so a cheaper model can be put behind pass 1 or 2 later without
touching the prompts. Pass 1 is text-only by construction, so a provider with
no vision can serve it; pass 2 refuses to run on one and says so.

### Choosing a model

`karpm/ai/models.py` is the list of models the config page offers, with what
each costs and what it accepts. The config page builds its dropdown from it and
the provider decides from it what to put in a request, so the two cannot drift
apart — which is the bug that made it a list: the page offered an effort level
for a model that rejects the parameter outright, and every listing in the queue
came back a 400.

**Not every model takes an effort level.** The newer ones think adaptively and
take `effort`; Haiku 4.5 takes neither and fails the whole request rather than
ignoring them. The config page hides the `effort` row for a model that has no
use for it, and the request is built without the setting rather than with one
the model would reject. The stored value is left alone either way, so switching
back brings it back.

**A model not in the list still works.** Write it into `config.toml` by hand and
the page offers it back marked *not in the list* rather than replacing it with
whichever option came first — a save about something else must not quietly
change which model you are paying. An unknown model is assumed to take adaptive
thinking and an effort level, since a model missing from the list is newer than
the list rather than older. If that assumption is wrong you get a 400 naming the
model, and the fix is to pick one from the dropdown.

Prices shown on the page are per million tokens as of the date in that file.
They are there to make the choice an informed one, not to bill anything.

### Why pass 2 shortlists

A gallery of twenty photographs is rarely twenty pieces of evidence. Five real
angles and fifteen near-duplicates is the usual shape, and sending all of them
to pass 3 spends the expensive model's attention — and your credits — on the
duplicates. Pass 2 looks at up to `extract_photos.max_photos_in` of them with a
cheap model and passes on at most `extract_photos.shortlist`. `scoring.max_images`
still caps what pass 3 is actually sent; the shortlist only decides *which*.

With pass 2 off, pass 3 falls back to the first few photos in the seller's
order, which is arbitrary.

---

## `extract`

Run passes 1 and 2 over whatever needs them. No scraping, no scoring, no email.
**Calls the API.**

```bash
karpm extract                  # both passes
karpm extract --text-only      # pass 1: the description
karpm extract --photos-only    # pass 2: the photos
```

| Flag | Default | Meaning |
|---|---|---|
| `--text-only` | off | Pass 1 only. |
| `--photos-only` | off | Pass 2 only. |

A listing is read when it has never been read, when what it was read from has
changed, or when that pass's `prompt_version` changed. A listing queued for
re-fetching is skipped: its stored text is known to be out of date, so reading
it now buys an answer about words that are about to be replaced. An ad with no
downloaded photos is skipped by pass 2 rather than read as having none.

Each pass stops at `max_per_run` listings, and re-reads the config before each
listing, so unticking a pass during a long run stops it at the next listing.

**Exit code 1** if any listing failed. A failure stores nothing — a row saying
"read, found nothing" would stop it ever being read again.

---

## `extract-one`

Read a single listing by id. The cheap way to try a change to a pass's prompt:
one ad, one call, and nothing written unless you say so.

```bash
karpm extract-one 3422210980 --show-prompt     # free: prints both prompts, no API call
karpm extract-one 3422210980                   # reads it and prints what it found
karpm extract-one 3422210980 --save            # ...and stores it
karpm extract-one 3422210980 --photos-only     # pass 2 alone
```

| Argument / flag | Default | Meaning |
|---|---|---|
| `listing_id` | required | Kleinanzeigen ad id, as stored in `listings.id`. |
| `--text-only` | off | Pass 1 only. |
| `--photos-only` | off | Pass 2 only. |
| `--show-prompt` | off | Print what would be sent and exit without calling the API. Costs nothing. |
| `--save` | off | Store what it finds. Without it the findings are printed only. |

Unlike [`extract`](#extract) it does not care whether the listing is due. Naming
one by id is the explicit instruction, and re-reading something already read is
most of the point when you are editing a prompt. For the same reason it stores
nothing by default: a trial that overwrites the stored reading is not a trial.

The findings go to stdout and everything else — token counts, "saved", why a
pass had nothing to look at — to stderr, so `karpm extract-one <id> | jq` works.

**Exit code 1** if the listing id is not in the database, or if a pass failed.
A pass with nothing to look at (an ad whose photos have not downloaded) says so
and is not a failure.

---

## `score`

Pass 3. Score every listing that needs it, then send an instant alert for
anything clearing the bar. **Calls the Claude API and may send email.**

```bash
karpm score
```

No options; behaviour comes from `[scoring]` and `[email]` in `config.toml`.
A listing is scored when it has no score, when its price or text changed since
the last one, or when `prompt_version` changed. It does not run passes 1 and 2
first — `karpm run`, the daemon and the dashboard do that.

**Exit code 1** if any instant alert failed to send. Scoring failures for an
individual listing are logged and skipped — one bad listing does not abort the
run.

---

## `score-one`

Score a single listing by id. The fastest way to tune `preferences.md` without
paying for a whole run.

```bash
karpm score-one 3422210980 --show-prompt     # free: prints the prompt, no API call
karpm score-one 3422210980 --save            # scores it and stores the result
```

| Argument / flag | Default | Meaning |
|---|---|---|
| `listing_id` | required | Kleinanzeigen ad id, as stored in `listings.id`. |
| `--show-prompt` | off | Print the prompt and exit without calling the API. Costs nothing. |
| `--save` | off | Store the resulting score. Without it the score is printed only. |

**Exit code 1** if the listing id is not in the database.

---

## Turning the passes off

Each pass has its own `enabled`, so turning all three off is what stops the API
costing anything. `scoring.enabled = false` leaves passes 1 and 2 running: their
findings are worth having on the listing page whether or not anything is being
scored.

`scoring.enabled = false` means nothing is scored, and every command respects it:

- `run` scrapes, reads, and says "scoring is disabled in the config; skipping
  it" rather than quietly doing half of what its name suggests.
- The **re-score** buttons refuse, and — importantly — **re-score everything**
  does not delete the existing verdicts first. Wiping them and then finding
  scoring switched off would destroy what nothing could rebuild.
- A run already under way stops at the next listing. The config file is re-read
  before each one, so unticking the box during a long run stops it after the
  listing in flight rather than at the end of the queue. Every listing is an API
  call, so that is the difference between one more and a few hundred more.

Every command logs which config file it read and whether scoring is on:

```
INFO config: /home/pi/KARPM/config.toml (scoring off)
```

That line is worth reading when a setting seems not to apply. Two copies of
`config.toml` in different directories — one the web UI writes, one the CLI
reads — look identical until the paths are side by side, and the config page
shows its own full path for the same reason.

## `run`

One full cycle: `scrape`, then passes 1, 2 and 3, then instant alerts. This is
what the schedule triggers.

```bash
karpm run
```

No options.

If `schedule.score_at` names any times, this command only scrapes: setting those
times was a decision about when the money is spent, and passes 1 and 2 spend it
as surely as pass 3 does.

---

## `digest`

Send the digest email — everything new at or above `digest_min_score` that has
not been mailed yet.

```bash
karpm digest --dry-run     # show what would be sent, send nothing
karpm digest
```

| Flag | Default | Meaning |
|---|---|---|
| `--dry-run` | off | List the listings that would be included and exit without sending. |

An empty digest is skipped when `skip_empty_digest` is set. Listings already
sent as an instant alert are not repeated here.

**Exit code 1** if the send failed — distinct from exit `0` with "nothing new to
send". A digest is only marked as delivered once the provider accepts it, so a
failed send leaves those listings queued for the next attempt.

---

## `daemon`

Run continuously on the schedule in `[schedule]`, scraping, scoring and mailing
at the configured local times. This is what the systemd unit in `deploy/`
starts.

**An empty list of times means never, and is not an error.** With
`scrape_at = []` the daemon runs exactly as usual — heartbeat, command queue,
pause — and simply has no slot to fire, which is how you drive it from the web
UI alone. The status panel reads "not scheduled".

`score_at` works the other way round: **empty means the three AI passes ride
along with each scrape**, which is what you want when the point is to hear about
a good listing quickly. Setting times separates the two, so scraping keeps its
own schedule and the API spending happens only in those slots. The slot gates
all three passes, not just the scoring: pass 3 scoring on findings pass 1 never
made would be the worst of both, the cost of three models and the evidence of
one.

```bash
karpm daemon
```

No options. It reads run history from the database, so a restart does not
repeat a slot it already completed. Stops cleanly on `SIGTERM`/`SIGINT`.

It reports a heartbeat on the interval in `schedule.heartbeat_s` (default 120
seconds; anything from 1 second up) — one line in the terminal and one timestamp in the database, so a
terminal running it does not look hung. The first arrives immediately:

```
heartbeat - next scrape 19:30, digest 08:00 tomorrow, score with each scrape
heartbeat - next scrape 19:30, digest 08:00 tomorrow, scoring off, 4 to re-score
heartbeat - next scrape 07:30 tomorrow, digest 08:00 tomorrow, SCHEDULE PAUSED
```

Anything outstanding is on the end of the line: listings waiting to be re-fetched
or re-scored, commands queued from the web UI, and whether the schedule is
paused — which is the commonest answer to "why has it not scraped".

It is written by its own thread, so it keeps arriving through a scrape that
holds the daemon for an hour — which is exactly when you want to know it is
still alive. The web UI waits three missed beats before calling the daemon dead,
so lengthening the interval does not make a healthy daemon look stopped.

**The config is re-read on every beat**, so changing `heartbeat_s` takes effect
on the next one rather than at the next restart — and the daemon never sleeps
longer than a heartbeat, so a short one makes the queued buttons responsive too.
A short interval is a fine way to watch what it is doing while you set it up.

A queued command wakes the daemon within a second rather than waiting out the
poll interval, so **scrape now** means now.

To see which config file the daemon actually read, look at the `config:` line it
logs at startup — that is what settles the two-config-files question.

---

## `web`

Serve the web UI: the daemon's status, a sortable listings table, the photos and
history behind each listing, and forms for the searches, the prompts and
every setting in `config.toml`.

```bash
karpm web
```

| Flag | Default | Meaning |
|---|---|---|
| `--lan` | off | Bind every interface, so other devices on your network can open it. Same as `--host 0.0.0.0`. |
| `--host ADDR` | `web.host`, `127.0.0.1` | Address to bind. Not with `--lan`. |
| `--port N` | `web.port`, `8080` | Port to listen on. |
| `--debug` | off | Flask debug mode and the auto-reloader. Development only. |

On startup it prints the address to open. Bound to every interface that is the
machine's own address on the network — `http://192.168.1.42:8080`, not
`http://0.0.0.0:8080`, which is not an address anything can browse to.

It is a **separate process from the daemon** and holds no privilege the daemon
has: the two meet only in the database. The buttons queue a command in the
`commands` table, and the daemon picks it up on its next poll — within a minute,
or after whatever it is currently doing finishes.

| Button | What it queues |
|---|---|
| `scrape now` | One full scrape — the same work a scheduled slot does. |
| `send digest` | A digest of everything above `email.digest_min_score`. |
| `read new listings` | Passes 1 and 2 for anything unread. **Costs money.** |
| `score new listings` | Pass 3 for anything unscored. **Costs money.** |
| `re-score everything` | The same, after deleting every existing score. Asks first, and costs a great deal more. |
| `pause schedule` | Stops the timed slots firing. Queued commands still run, so the buttons keep working. |
| `clear the database` | Deletes every listing, its history, scores and downloaded photos, and asks first. Red, and there is no undo. |

**`clear the database`** puts you back at a fresh install: every listing, its
price history, scores, run log and downloaded photos are gone. `config.toml` and
`preferences.md` are not touched, and your searches are registered again from
the config on the next open, so the thing starts collecting from scratch rather
than from nothing. A pause you had set survives — silently un-pausing would let
a scrape start that you had deliberately stopped. It refuses while the daemon is
mid-command, since a wipe would be undone by the rows that command is about to
write.

Each listing's own page has three more: **ignore** keeps a listing out of every
email without deleting it — for one that scored well but is not for you —
**read the page again** queues a re-fetch, and **score it again** queues a new
verdict. The dashboard shows how many of each are outstanding, and the listings
filter has a view for each.

The dashboard's **to read** line says how many listings each of the two reading
passes still owes a look at, or that the pass is switched off. A pass that has
quietly stopped and one that has read everything look the same from the outside
unless the page says which.

A listing's page shows what those passes found in two panels of their own —
**read from the description** and **read from the photos** — each naming the
model that said it and when. They are kept apart from the parsed fields on
purpose: a model's summary of what a seller claims is useful, but mixed in with
the mileage read off the page it would be indistinguishable from something
checked. Photos pass 2 shortlisted are outlined in the gallery, with its note on
each photo underneath.

### The prompts page

Everything the three passes are told, in the order they run.

Passes 1 and 2 get one text box each — their instructions, saved to the file named
by `extract_text.prompt_file` and `extract_photos.prompt_file`. With no file
there, or an empty one, the pass uses the prompt built into the code, so a fresh
install works without writing anything and emptying the box puts the original
back.

**Editing a prompt re-reads every listing.** The prompt's own text is part of
the key that decides whether stored findings are still current, so a changed
prompt makes them all stale without anyone having to remember to bump
`prompt_version`. That costs credits, and the page says how many listings it
just queued. `karpm extract-one <id>` is the cheap way to try a wording on one
ad first.

Pass 3 is not a prompt but a description of what you want, and it is five boxes
rather than one: **About the bike**, **What it needs**, **What I would like**,
**What is not important**, and **Logistics**. They are compiled into
`preferences.md` — the same file as before, with one `##` heading per box — and
that whole file goes to the scoring pass with every listing. An empty box keeps
its heading: "I do not care about this" is itself worth telling the model.

A `preferences.md` written by hand, or written before this page existed, does not
fit those five headings. Nothing is thrown away: whatever is not under a heading
we know appears in a sixth box, **the rest of the file**, still goes to the
model, and can be moved into the boxes above whenever you feel like it.

### Editing the settings

**Config** is a field per setting, grouped by the table it lives in, with the
setting's name on the left and a note on what it does on the right. A setting
that means nothing for the model a pass is pointed at is hidden rather than
offered — see [Choosing a model](#choosing-a-model). **Searches**
is the same, one block per `[[searches]]` entry, with a button to add another
and a link to remove one.

Numbers are checked as numbers and choices against their options, so a typo is
pointed at rather than saved; when anything is rejected nothing is written at
all and the page comes back with what you typed. As a last check the whole file
is loaded as a config before it replaces the real one, so a save that would stop
KARPM from starting never lands.

Values are edited on the line they already occupy, and only the ones you
actually changed are touched, which means **the comments and layout in your
`config.toml` survive a save**. A `.bak` is written beside any file the UI
changes.

Searches are the exception: a `[[searches]]` block is regenerated rather than
edited line by line, since the form can add and remove them. Comments inside a
block are kept, but they end up collected above the blocks rather than between
the settings they were next to — so keep your own notes above the searches.

A field that may be left blank says so in its note — blank removes the setting
so its built-in default applies, which is not the same as an empty value. Edits
take effect without a restart, since the daemon rereads its config every cycle;
only `web.port` needs `karpm web` restarted.

### Reaching it from another device

**There is no login.** Bound to `127.0.0.1` — the default — that is fine,
because only the machine itself can reach it. There are two ways to change that,
and they are not equivalent.

**An SSH tunnel** keeps the server on localhost and forwards one port to the
device you are sitting at. Nothing is exposed, and it works from outside the
house if you can already SSH in:

```bash
ssh -N -L 8080:localhost:8080 pi@raspberrypi.local   # then open http://localhost:8080
```

**`karpm web --lan`** opens it to every device on your network — every phone
and laptop on that Wi-Fi, guests included. It is not reachable from the internet
unless you also forward the port on your router, which is a bad idea for a page
with no login. On a home network you trust this is the convenient option; on
shared or student Wi-Fi, use the tunnel.

To make it permanent, set `host = "0.0.0.0"` under `[web]` and restart — the
host and port are read at startup, so a change there needs `karpm web` restarted
where the rest of the config does not.

The machine's address can change when the router hands out new DHCP leases; a
static lease (a DHCP reservation, in the router's admin page) pins it. On a Pi,
`raspberrypi.local` usually works from phones and Macs, less reliably from
Windows.

`deploy/karpm-web.service` runs it under systemd alongside `karpm.service`.

The server is Flask's own, which is right for one person on localhost and not
meant for anything exposed.

---

## `stats`

Summarise what has been collected: totals, score distribution, recent price
drops. Reads only.

```bash
karpm stats
```

---

## `top`

The best-scoring active listings.

```bash
karpm top --min-score 4 --limit 20
```

| Flag | Default | Meaning |
|---|---|---|
| `--min-score N` | `4` | Lowest overall score to show. |
| `--limit N` | `20` | Maximum listings to show. |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. For `digest`, that includes "there was nothing new to send". |
| `1` | `trial`: a required field failed to parse, or an image download failed. `score-one`: listing id not in the database. `digest`: the send failed. `score`: an instant alert failed to send. |
| `1` | `raw`: the request itself failed. |
| `2` | Bad arguments: a missing or contradictory flag, or `--search` naming a search that is not in the config (the message lists the valid names). |

`daemon` and `web` only exit on a signal, and exit `0`. The daemon's per-run
failures are logged and recorded in the `runs` table rather than ending the
process; a queued command that fails is recorded in the `commands` table with
its error.

## Typical sequences

```bash
# first run on a new machine
cp config.example.toml config.toml && cp .env.example .env
cp preferences.example.md preferences.md
karpm init
karpm trial --url "<your search url>" --limit 5 --make BMW --model "R 1200 GS"

# tune the preferences without spending anything
karpm extract-one <id> --show-prompt
karpm score-one <id> --show-prompt

# once happy
karpm run
karpm top

# leave it running, with the web UI alongside it
sudo systemctl enable --now karpm
sudo systemctl enable --now karpm-web    # http://localhost:8080 on the Pi
```
