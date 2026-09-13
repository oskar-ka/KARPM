"""What the config form shows: one entry per setting in config.toml.

The types here are checked against the dataclasses in `karpm.config` by a
test, so a field added, removed or retyped there fails the suite rather than
quietly disappearing from the page.

`help` is the note shown to the right of a field. Settings whose name says it
all get no note - a description of `port` that reads "the port" is worse than
white space.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

# Kinds map to a form control and to the way the value is written back as TOML:
#   text          a line of text
#   int, float    a number; blank means "leave unset" on an optional field
#   bool          a checkbox
#   choice        a <select> over `choices`
#   lines         a list of strings, one per line in a textarea
#   numbers       a list of numbers, comma separated
KINDS = ("text", "int", "float", "bool", "choice", "lines", "numbers")


@dataclass(frozen=True)
class Field:
    key: str
    kind: str
    help: str = ""
    choices: tuple[str, ...] = ()
    optional: bool = False          # blank is allowed and means "not set"
    placeholder: str = ""
    wide: bool = False


@dataclass(frozen=True)
class Section:
    name: str                       # the TOML table, "" for top-level keys
    title: str
    blurb: str = ""
    fields: tuple[Field, ...] = dc_field(default_factory=tuple)


SEARCH_FIELDS = (
    Field("enabled", "bool", "Off keeps the search and its listings but skips it on a run."),
    Field("name", "text", "Identifies the search in the database and the logs. Must be unique."),
    Field("url", "text", "Build the search in your browser with the filters you want - "
                         "make, model, radius, price - then paste the result here.",
          wide=True, placeholder="https://www.kleinanzeigen.de/s-…"),
    Field("make", "text", "", optional=True, placeholder="Yamaha"),
    Field("model", "text", "Ads carry a Marke but no Modell, so the model has to come from "
                           "here. Without it, listings cannot be grouped into the price "
                           "comparables the scoring prompt uses.",
          optional=True, placeholder="MT-07"),
    Field("max_ads", "int", "Blank takes the whole search. Pages are walked until this is "
                            "met, so there is no separate page limit.",
          optional=True, placeholder="all"),
)


SECTIONS = (
    Section("", "general", "", (
        Field("db_path", "text",
              "One SQLite file: listings, their history, images and scores.", wide=True),
        Field("home_plz", "text", "Your postcode. How far away a listing is gets "
                                  "worked out from this; blank leaves every distance "
                                  "unknown rather than guessed.",
              optional=True, placeholder="22765"),
    )),

    Section("scrape", "scrape", (
        "Pacing is rate limiting, not an attempt to look human. Kleinanzeigen's bot "
        "protection reacts to how fast one IP asks, and the penalty is a captcha wall "
        "for a few hours."), (
        Field("min_delay_s", "float", "Seconds to wait between page requests - a random "
                                      "value in this range each time."),
        Field("max_delay_s", "float"),
        Field("image_min_delay_s", "float", "The same for photos, which come from a static "
                                            "CDN rather than the search backend and are "
                                            "most of a run's requests."),
        Field("image_max_delay_s", "float"),
        Field("retry_delays_s", "numbers", "Seconds before each further attempt at a failed "
                                           "request, one per attempt. A 400 or a 404 is "
                                           "never retried."),
        Field("timeout_s", "float"),
        Field("max_retries", "int"),
        Field("block_threshold", "int", "Abort the whole run after this many blocked "
                                        "responses in a row, rather than hammering."),
        Field("block_backoff_s", "int", "Base wait after a suspected block. It grows with "
                                        "the square of the count."),
        Field("dump_dir", "text", "Pages saved for diagnosis land here: block pages, and "
                                  "ad pages whose gallery came up short.", wide=True),
        Field("refresh_after_hours", "int", "How often to re-open an ad already in the "
                                            "database, to pick up edits."),
        Field("verify_delisting", "bool", "Open a listing's own page before marking it gone. "
                                          "Ads drop out of search results for reasons that "
                                          "have nothing to do with being sold."),
        Field("max_delist_checks", "int", "Extra requests one run spends on those checks. "
                                          "The rest stay active and wait for the next run."),
        Field("recheck_missing_after_hours", "int",
              "Skip re-checking a listing confirmed live this recently."),
        Field("user_agent", "text", "", wide=True),
        Field("accept_language", "text", "", wide=True),
    )),

    Section("trial", "trial", (
        "Pacing for `trial`, `probe` and `raw`, and for `--fast` on any command. "
        "A short interactive run is nowhere near what gets an IP blocked."), (
        Field("min_delay_s", "float"),
        Field("max_delay_s", "float"),
        Field("image_min_delay_s", "float"),
        Field("image_max_delay_s", "float"),
    )),

    Section("images", "images", "", (
        Field("enabled", "bool", "Off collects listings without their photos. Scoring can "
                                 "then only judge from the text."),
        Field("dir", "text", "One folder per ad, with an ad.txt naming the listing it "
                             "belongs to.", wide=True),
        Field("max_per_listing", "int", "Photos to keep per ad. Blank falls back to 12; "
                                        "`karpm images --all-images` takes every one.",
              optional=True, placeholder="12"),
        Field("give_up_after_failures", "int",
              "Stop on an ad's remaining photos after this many fail in a row. An ad whose "
              "images are broken has all of them broken, and each failure costs several "
              "requests."),
        Field("max_bytes", "int", "Largest single photo to keep, in bytes."),
    )),

    Section("scoring", "scoring", "", (
        Field("enabled", "bool", "Off collects and stores listings but never calls the API, "
                                 "so nothing costs money."),
        Field("model", "text"),
        Field("effort", "choice", "How hard the model thinks about each listing.",
              choices=("low", "medium", "high", "xhigh", "max")),
        Field("prompt_version", "text", "Bump this after editing your preferences to score "
                                        "everything again against them."),
        Field("preferences_file", "text", "", wide=True),
        Field("max_images", "int", "Photos sent with each listing. Rust, crash damage and "
                                   "worn tyres are visible and not mentioned in the text, "
                                   "but each costs about 1.5k input tokens. 0 scores on "
                                   "text alone."),
        Field("rescore_on_change", "bool", "Score a listing again when its price or text "
                                           "changes."),
        Field("max_per_run", "int", "Most listings to score in one run - the ceiling on what "
                                    "a single run can spend."),
    )),

    Section("email", "email", "", (
        Field("enabled", "bool", "Off still scores listings; it just never mails you."),
        Field("provider", "text", "Only `resend` is implemented."),
        Field("from_address", "text", "Must be on a domain you have verified with Resend.",
              wide=True),
        Field("to_addresses", "lines", "One address per line.", wide=True),
        Field("subject_prefix", "text", "Makes the mail easy to filter on."),
        Field("instant_min_score", "int", "Mail the moment a listing scores this or better. "
                                          "Good bikes sell the same day."),
        Field("instant_min_bargain_pct", "int",
              "…or when a 4+ listing is at least this far under the estimated fair price."),
        Field("digest_min_score", "int", "Lowest score the daily digest includes."),
        Field("digest_max_listings", "int"),
        Field("skip_empty_digest", "bool", "Send nothing rather than an empty digest."),
    )),

    Section("schedule", "schedule", "", (
        Field("scrape_at", "lines", "Local times on the Pi, HH:MM, one per line.",
              placeholder="07:30"),
        Field("digest_at", "lines", "", placeholder="08:00"),
    )),

    Section("web", "web", "", (
        Field("host", "text", "127.0.0.1 means only this machine can open the UI. There is "
                              "no login, so binding wider hands the controls - and your "
                              "Claude credits - to everyone on the network. Use an SSH "
                              "tunnel instead."),
        Field("port", "int", "Takes effect when `karpm web` is restarted."),
    )),
)


def input_name(section: str, key: str) -> str:
    """The form name and id for one setting.

    Not "section.key": a dot in an id makes `#scrape.min_delay_s` read as a
    class selector, which is a trap for any CSS or JavaScript added later. No
    section or key contains a double underscore, so this stays unambiguous.
    """
    return f"{section}__{key}" if section else key


def section(name: str) -> Section:
    for candidate in SECTIONS:
        if candidate.name == name:
            return candidate
    raise KeyError(name)
