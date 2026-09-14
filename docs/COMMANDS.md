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

## `score`

Score every listing that needs it, then send an instant alert for anything
clearing the bar. **Calls the Claude API and may send email.**

```bash
karpm score
```

No options; behaviour comes from `[scoring]` and `[email]` in `config.toml`.
A listing is scored when it has no score, when its price or text changed since
the last one, or when `prompt_version` changed.

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

## Scoring, and turning it off

`scoring.enabled = false` means nothing is sent to the API, and every command
respects it:

- `run` scrapes and says "scoring is disabled in the config; this run only
  scrapes" rather than quietly doing half of what its name suggests.
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

One full cycle: `scrape`, then `score`, then instant alerts. This is what the
schedule triggers.

```bash
karpm run
```

No options.

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

`score_at` works the other way round: **empty means scoring rides along with
each scrape**, which is what you want when the point is to hear about a good
listing quickly. Setting times separates the two, so scraping keeps its own
schedule and the API spending happens only in those slots.

```bash
karpm daemon
```

No options. It reads run history from the database, so a restart does not
repeat a slot it already completed. Stops cleanly on `SIGTERM`/`SIGINT`.

It says it is alive every five minutes, so a terminal running it does not look
hung — the first line comes immediately:

```
alive - next scrape 19:30, digest 08:00 tomorrow, score with each scrape
alive - next scrape 19:30, digest 08:00 tomorrow, scoring off, 4 to re-score
alive - next scrape 07:30 tomorrow, digest 08:00 tomorrow, SCHEDULE PAUSED
```

Anything outstanding is on the end of the line: listings waiting to be re-fetched
or re-scored, commands queued from the web UI, and whether the schedule is
paused — which is the commonest answer to "why has it not scraped".

Five minutes is the same interval after which the web UI calls the daemon dead,
so a terminal quiet for longer than one of these has actually gone quiet. The
heartbeat itself is written every 30 seconds; `-v` logs each one.

---

## `web`

Serve the web UI: the daemon's status, a sortable listings table, the photos and
history behind each listing, and forms for the searches, `preferences.md` and
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
| `score new listings` | Scoring for anything unscored. **Costs money.** |
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

### Editing the settings

**Config** is a field per setting, grouped by the table it lives in, with the
setting's name on the left and a note on what it does on the right. **Searches**
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
karpm score-one <id> --show-prompt

# once happy
karpm run
karpm top

# leave it running, with the web UI alongside it
sudo systemctl enable --now karpm
sudo systemctl enable --now karpm-web    # http://localhost:8080 on the Pi
```
