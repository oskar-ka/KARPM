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
| `-h`, `--help` | — | Help, at top level or for any command. |

Secrets are **not** in the config file. They are read from the environment, or
from a `.env` file in the working directory: `ANTHROPIC_API_KEY` for scoring and
`RESEND_API_KEY` for email.

## Command summary

| Command | Touches the network? | Costs money? | Sends email? |
|---|---|---|---|
| [`init`](#init) | no | no | no |
| [`trial`](#trial) | yes (scrape) | no | no |
| [`probe`](#probe) | optional | no | no |
| [`scrape`](#scrape) | yes | no | no |
| [`images`](#images) | yes | no | no |
| [`score`](#score) | yes (API) | **yes** | **yes** (instant alerts) |
| [`score-one`](#score-one) | yes (API) | **yes** | no |
| [`run`](#run) | yes | **yes** | **yes** |
| [`digest`](#digest) | no | no | **yes** |
| [`daemon`](#daemon) | yes | **yes** | **yes** |
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
| `--limit N` | `5` | Stop after N listings. Every listing is a real request — keep this small. |
| `--pages N` | `1` | Maximum search-result pages to walk. |
| `--db PATH` | `data/trial.db` | Throwaway database. Wiped at the start of each run unless `--keep`. |
| `--image-dir PATH` | `data/trial_images` | Where trial images are written. |
| `--make NAME` | — | Make to record on each listing, as in `config.toml`. |
| `--model NAME` | — | Model to record. Ads carry no *Modell* attribute, so without this listings cannot be grouped into price comparables. |
| `--no-images` | off | Skip image downloads (faster, fewer requests). |
| `--keep` | off | Append to the trial database instead of starting clean. |
| `--show-prompt` | off | Also print the scoring prompt for the first listing — without sending it. |

**Exit code 0** if every required field parsed on every listing and no image
download failed; **1** otherwise, so it works as a cron healthcheck.

The report separates two kinds of gap: **MISSING** means a field should have
parsed and did not — a bug worth reporting; *not stated* means the site never
provides it for these ads (motorcycle listings have no `owners` or `condition`).

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

Run continuously on the schedule in `[schedule]`, scraping and mailing at the
configured local times. This is what the systemd unit in `deploy/` starts.

```bash
karpm daemon
```

No options. It reads run history from the database, so a restart does not
repeat a slot it already completed. Stops cleanly on `SIGTERM`/`SIGINT`.

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
| `2` | Bad arguments — for example `trial --search` naming a search that is not in the config. |

`daemon` only exits on a signal, and exits `0`; per-run failures are logged and
recorded in the `runs` table rather than ending the process.

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

# leave it running
sudo systemctl enable --now karpm
```
