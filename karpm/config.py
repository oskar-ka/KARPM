"""Configuration loading: config.toml for settings, .env for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

try:                                   # tomllib is stdlib from Python 3.11
    import tomllib
except ModuleNotFoundError:            # 3.10 and older need the backport
    import tomli as tomllib

DEFAULT_CONFIG_PATH = Path("config.toml")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader. Existing environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class SearchConfig:
    name: str
    url: str
    enabled: bool = True
    # Stop after this many ads; None takes the whole search. Pages are walked
    # until this is met, so there is no separate page limit.
    max_ads: int | None = None
    # Kleinanzeigen motorcycle ads carry a "Marke" but no "Modell" attribute, so
    # the model cannot be parsed off the page. Since one search URL targets one
    # model anyway, declare it here: it is what groups listings into the price
    # comparables the scoring prompt relies on.
    make: str | None = None
    model: str | None = None


@dataclass
class ScrapeConfig:
    # Politeness. These defaults are deliberately slow: one Pi making a few
    # hundred requests twice a day should be indistinguishable from a person
    # browsing. Lower them at your own risk of getting blocked.
    min_delay_s: float = 4.0
    max_delay_s: float = 9.0
    # Images come from a static CDN (img.kleinanzeigen.de), not the search
    # backend, and a browser loads a whole gallery at once. Pacing them like
    # search queries makes them ~90% of a run's waiting for no benefit.
    image_min_delay_s: float = 0.4
    image_max_delay_s: float = 1.2
    # How long to wait before retrying a request that failed for a reason worth
    # retrying. One entry per attempt; the last is reused if there are more
    # attempts than entries. Permanent failures (a 400 or a 404) are not
    # retried at all, whatever this says.
    retry_delays_s: list[float] = field(default_factory=lambda: [5, 10, 20, 40])
    timeout_s: float = 30.0
    max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    accept_language: str = "de-DE,de;q=0.9,en;q=0.5"
    # Stop the run entirely after this many consecutive blocked responses.
    block_threshold: int = 3
    # Base backoff after a suspected block; grows with the square of the count.
    block_backoff_s: int = 60
    # Saved pages for diagnosis - block pages, and ad pages whose gallery
    # came up short - are written here.
    dump_dir: str = "data/debug"

    @property
    def image_delay_range(self) -> tuple[float, float]:
        return (self.image_min_delay_s, self.image_max_delay_s)

    @property
    def page_delay_range(self) -> tuple[float, float]:
        return (self.min_delay_s, self.max_delay_s)

    def at_pace(self, trial: "TrialConfig | None") -> "ScrapeConfig":
        """A copy paced for a trial run, or this one unchanged."""
        if trial is None:
            return self
        return replace(
            self,
            min_delay_s=trial.min_delay_s,
            max_delay_s=trial.max_delay_s,
            image_min_delay_s=trial.image_min_delay_s,
            image_max_delay_s=trial.image_max_delay_s,
        )
    # Re-fetch the detail page of a known listing at most this often (hours).
    refresh_after_hours: int = 24
    # A listing missing from the search results has its own page checked before
    # being delisted. Turning this off falls back to assuming absence means
    # gone, which is wrong often enough to lose live listings.
    verify_delisting: bool = True
    # Cap the extra requests one run will spend on those checks. Anything over
    # the cap keeps its active status and is checked next run.
    max_delist_checks: int = 25
    # Don't re-check a listing that was confirmed live this recently (hours).
    # A listing outside the search's price filter would otherwise be re-fetched
    # every single run, forever.
    recheck_missing_after_hours: int = 12


@dataclass
class TrialConfig:
    """Pacing for `trial`, `probe` and `raw`, and for --fast.

    The production pace exists for rate limiting, not to look human:
    Kleinanzeigen's bot protection reacts to how fast one IP asks, and the
    penalty is a captcha wall for a few hours. A short interactive run is far
    below that, which is why it gets its own numbers.
    """

    min_delay_s: float = 0.5
    max_delay_s: float = 1.5
    image_min_delay_s: float = 0.1
    image_max_delay_s: float = 0.3


@dataclass
class ImageConfig:
    enabled: bool = True
    dir: str = "data/images"
    # None means every photo the ad has.
    max_per_listing: int | None = 12
    # Give up on a listing's remaining photos after this many in a row fail.
    # An ad whose images are broken has all of them broken, and grinding
    # through twenty of those costs far more than it can ever return.
    give_up_after_failures: int = 3
    max_bytes: int = 5_000_000


@dataclass
class ScoringConfig:
    enabled: bool = True
    model: str = "claude-opus-5"
    effort: str = "medium"
    prompt_version: str = "v1"
    preferences_file: str = "preferences.md"
    # Photos carry real signal about condition, but cost ~1.5k input tokens
    # each. 0 disables vision entirely.
    max_images: int = 2
    # Re-score a listing when its price or text changed.
    rescore_on_change: bool = True
    max_per_run: int = 200


@dataclass
class EmailConfig:
    enabled: bool = True
    provider: str = "resend"
    from_address: str = "karpm@example.com"
    to_addresses: list[str] = field(default_factory=list)
    subject_prefix: str = "[KARPM]"
    # Instant alert when a listing scores at least this overall.
    instant_min_score: int = 5
    # ...or when it is at least this much below the model's fair-price estimate.
    instant_min_bargain_pct: int = 20
    # Digest includes listings scoring at least this.
    digest_min_score: int = 3
    digest_max_listings: int = 25
    # Don't send an empty digest.
    skip_empty_digest: bool = True


@dataclass
class WebConfig:
    """The web UI. It binds to localhost by default: reaching it from elsewhere
    is a question for SSH, Tailscale or a reverse proxy, not for this process.
    There is no login, so whoever can reach the port can change what the Pi
    scrapes and spend Claude credits by re-scoring."""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class ScheduleConfig:
    # Local times (HH:MM) at which the daemon scrapes and mails.
    scrape_at: list[str] = field(default_factory=lambda: ["07:30", "19:30"])
    digest_at: list[str] = field(default_factory=lambda: ["08:00"])


@dataclass
class Config:
    db_path: str = "data/karpm.db"
    # Your postcode. Distance to a listing is worked out from this - a bike 40 km
    # away is a Saturday morning, the same bike 500 km away is a weekend and a
    # trailer. Blank leaves every distance unknown rather than guessed.
    home_plz: str | None = None
    searches: list[SearchConfig] = field(default_factory=list)
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    trial: TrialConfig = field(default_factory=TrialConfig)
    images: ImageConfig = field(default_factory=ImageConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def resend_api_key(self) -> str | None:
        return os.environ.get("RESEND_API_KEY")


class ConfigError(ValueError):
    """A config file that parses as TOML but is not one KARPM can use."""


def _check(section: str, key: str, hint, value):
    """Verify one value against its declared type.

    Dataclasses do not enforce their annotations, so without this a typo like
    `min_delay_s = "4"` is accepted here and fails much later somewhere
    unhelpful - and the web UI's "does this load?" check would pass it.
    """
    where = f"{section}.{key}" if section else key
    origin, args = get_origin(hint), get_args(hint)
    if origin is Union or origin is UnionType:
        if value is None and type(None) in args:
            return None
        for candidate in (a for a in args if a is not type(None)):
            try:
                return _check(section, key, candidate, value)
            except ConfigError:
                continue
        raise ConfigError(f"{where}: expected {hint}, got {value!r}")

    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected a list, got {value!r}")
        return [_check(section, f"{key}[{i}]", args[0], v) for i, v in enumerate(value)] \
            if args else list(value)

    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false, got {value!r}")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected a whole number, got {value!r}")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)
    if hint is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected text, got {value!r}")
        return value
    return value


def _subset(cls, data: dict, section: str = ""):
    """Build a dataclass from a dict, ignoring unknown keys and checking types."""
    hints = get_type_hints(cls)
    known = {f.name for f in cls.__dataclass_fields__.values()}
    label = section or cls.__name__
    values = {}
    for key, value in data.items():
        if key not in known:
            continue
        values[key] = _check(label, key, hints.get(key, object), value)
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConfigError(f"{label}: {exc}") from exc


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    load_dotenv()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.toml to {path} and edit it."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    db_path = raw.get("db_path", Config.db_path)
    _check("", "db_path", str, db_path)
    home_plz = raw.get("home_plz", Config.home_plz)
    _check("", "home_plz", str | None, home_plz)
    return Config(
        db_path=db_path,
        home_plz=home_plz,
        searches=[_subset(SearchConfig, s, f"searches[{i}]")
                  for i, s in enumerate(raw.get("searches", []))],
        scrape=_subset(ScrapeConfig, raw.get("scrape", {}), "scrape"),
        trial=_subset(TrialConfig, raw.get("trial", {}), "trial"),
        images=_subset(ImageConfig, raw.get("images", {}), "images"),
        scoring=_subset(ScoringConfig, raw.get("scoring", {}), "scoring"),
        email=_subset(EmailConfig, raw.get("email", {}), "email"),
        schedule=_subset(ScheduleConfig, raw.get("schedule", {}), "schedule"),
        web=_subset(WebConfig, raw.get("web", {}), "web"),
    )
