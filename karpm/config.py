"""Configuration loading: config.toml for settings, .env for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
    max_pages: int = 10
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
    timeout_s: float = 30.0
    max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    accept_language: str = "de-DE,de;q=0.9,en;q=0.5"
    # Stop the run entirely after this many consecutive blocked responses.
    block_threshold: int = 3
    # Re-fetch the detail page of a known listing at most this often (hours).
    refresh_after_hours: int = 24


@dataclass
class ImageConfig:
    enabled: bool = True
    dir: str = "data/images"
    max_per_listing: int = 12
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
class ScheduleConfig:
    # Local times (HH:MM) at which the daemon scrapes and mails.
    scrape_at: list[str] = field(default_factory=lambda: ["07:30", "19:30"])
    digest_at: list[str] = field(default_factory=lambda: ["08:00"])


@dataclass
class Config:
    db_path: str = "data/karpm.db"
    searches: list[SearchConfig] = field(default_factory=list)
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    images: ImageConfig = field(default_factory=ImageConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def resend_api_key(self) -> str | None:
        return os.environ.get("RESEND_API_KEY")


def _subset(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    load_dotenv()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.toml to {path} and edit it."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg = Config(
        db_path=raw.get("db_path", Config.db_path),
        searches=[_subset(SearchConfig, s) for s in raw.get("searches", [])],
        scrape=_subset(ScrapeConfig, raw.get("scrape", {})),
        images=_subset(ImageConfig, raw.get("images", {})),
        scoring=_subset(ScoringConfig, raw.get("scoring", {})),
        email=_subset(EmailConfig, raw.get("email", {})),
        schedule=_subset(ScheduleConfig, raw.get("schedule", {})),
    )
    return cfg
