# KARPM — notes for Claude

Scrapes Kleinanzeigen motorcycle listings, stores them with full history, scores
them with Claude against `preferences.md`, and emails the good ones. Runs
unattended on a Raspberry Pi.

## Standing conventions

- **`docs/COMMANDS.md` is the command reference and must stay in step with the
  CLI.** Any new command, flag, default or exit code goes in that file in the
  same commit that changes the behaviour.
- **`docs/DATABASE.md`** covers the schema and the save path — update it when
  the schema changes.
- **Bump `db.PARSER_VERSION` when a parser change means stored rows would now
  come out differently.** Every listing written by an older version is then
  re-fetched on the next scrape and scored again. Skip it only when the change
  can be applied to the stored rows in place (as `repair_descriptions` does),
  which is cheaper — and say which you did.
- Push directly to `main`; do not open pull requests unless asked.
- Python 3.10 is the floor (Raspberry Pi OS / Ubuntu 22.04 ship it).

## Things that are easy to get wrong here

- **Kleinanzeigen serves two different stacks, and is migrating between them.**
  Search results are an Astro app with Tailwind class names that carry no
  meaning and churn, so the search parser matches on the *shape of the text* (a
  price looks like `1.250 € VB`), not on class names. Ad pages exist in both
  shapes at once: the older `#viewad-*` markup, and an Astro version that ships
  one `<img>` and leaves the gallery to a hydration payload
  (`astro-island` props → `data.imageDetails.imageList`). Both routes must keep
  working; fixtures exist for each.
- **Encoding.** Responses arrive without a charset header, so `requests` falls
  back to Latin-1 and mangles every umlaut. `http.decode()` handles this; do not
  reach for `resp.text` directly.
- **Ads have no `Modell` attribute.** The model comes from the search config,
  and without it listings cannot be grouped into price comparables.
- **A failed parse must never overwrite a good value** with `None`, and must
  show up in `parse_warnings` rather than silently becoming NULL.
- **Check a fix against the page that actually failed.** Three attempts at one
  image bug were reasoned from the two ad pages on hand, and all three were
  wrong about the cause, because those pages were the old stack and the failing
  ones were not. `karpm trial` saves short-gallery pages into `scrape.dump_dir`
  and `karpm probe` prints a per-source photo count for exactly this.
- **Some things go stale without the ad changing.** A parser fix, an edit to
  `preferences.md`, a listing you have dismissed: none of those show up as a
  price drop or an edit, so each is recorded on the row (`needs_refetch`,
  `needs_rescore`, `ignored`) rather than left to be noticed. Anything that
  invalidates stored data belongs in one of those flags.
- **Silence is the enemy.** A scraper returning rows of NULLs, or a digest that
  could not send, must not look like success. Check `karpm trial` still passes.

## Verifying changes

`pytest` covers the parsers, the pipeline end to end against a fake fetcher, and
the email rendering. Fixtures under `tests/fixtures/live_*.html` are trimmed
captures of real pages — treat them as the source of truth about the markup.

Kleinanzeigen is not reachable from the Claude Code sandbox, so live behaviour
cannot be verified here. Say so rather than implying otherwise.
