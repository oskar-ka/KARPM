"""Argument resolution for the commands that accept a URL or a named search."""

import pytest

from karpm.cli import SearchNotFound, _search_by_name, main
from karpm.config import Config, SearchConfig


@pytest.fixture
def conf():
    cfg = Config()
    cfg.searches = [
        SearchConfig(name="bmw-r1200gs", url="https://example.invalid/bmw", model="R 1200 GS"),
        SearchConfig(name="mt07", url="https://example.invalid/mt07"),
    ]
    return cfg


def test_search_by_name_returns_the_configured_search(conf):
    found = _search_by_name(conf, "bmw-r1200gs", "config.toml")
    assert found.url == "https://example.invalid/bmw"
    assert found.model == "R 1200 GS"


def test_unknown_search_name_lists_what_is_available(conf):
    with pytest.raises(SearchNotFound) as excinfo:
        _search_by_name(conf, "typo", "config.toml")
    message = str(excinfo.value)
    assert "typo" in message
    assert "bmw-r1200gs" in message and "mt07" in message


def test_unknown_search_name_with_no_searches_configured():
    with pytest.raises(SearchNotFound) as excinfo:
        _search_by_name(Config(), "anything", "config.toml")
    assert "none" in str(excinfo.value)


@pytest.mark.parametrize("argv", [
    ["raw"],                                        # neither
    ["raw", "https://example.invalid", "--search", "mt07"],   # both
])
def test_raw_rejects_ambiguous_arguments(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert "raw" in capsys.readouterr().err


def test_raw_accepts_a_bare_url(tmp_path):
    """A URL alone must still work - --search is an addition, not a replacement.

    Validation happens before the config is read, so reaching the missing-config
    error proves the arguments were accepted.
    """
    with pytest.raises(FileNotFoundError):
        main(["-c", str(tmp_path / "absent.toml"), "raw", "https://example.invalid/x"])


def test_raw_accepts_search_alone(tmp_path):
    with pytest.raises(FileNotFoundError):
        main(["-c", str(tmp_path / "absent.toml"), "raw", "--search", "mt07"])


# --- pacing -------------------------------------------------------------------

import argparse  # noqa: E402

from karpm.cli import FAST_BY_DEFAULT, _resolve_pace  # noqa: E402


def _args(command, fast=False, polite=False):
    return argparse.Namespace(command=command, fast=fast, polite=polite)


@pytest.mark.parametrize("command", sorted(FAST_BY_DEFAULT))
def test_testing_commands_are_fast_by_default(command):
    assert _resolve_pace(_args(command)) is True


@pytest.mark.parametrize("command", ["scrape", "run", "daemon", "score", "digest", "images"])
def test_unattended_commands_stay_polite_by_default(command):
    """These run for months on a schedule; a fast default there is what would
    actually get the Pi blocked."""
    assert _resolve_pace(_args(command)) is False


def test_flags_override_the_default_either_way():
    assert _resolve_pace(_args("scrape", fast=True)) is True
    assert _resolve_pace(_args("trial", polite=True)) is False


def test_at_pace_does_not_mutate_the_original():
    from karpm.config import ScrapeConfig
    polite = ScrapeConfig()
    fast = polite.at_pace(True)
    assert fast.page_delay_range == (polite.fast_min_delay_s, polite.fast_max_delay_s)
    assert polite.page_delay_range == (4.0, 9.0)
    assert polite.at_pace(False) is polite


# --- trial scope flags --------------------------------------------------------

def _run_trial_capturing(monkeypatch, tmp_path, extra_argv):
    """Invoke cmd_trial for real, capturing the search it builds.

    The banner and scope resolution live in cmd_trial, which the trial tests
    never reach because they call run_trial directly - a broken banner shipped
    once because of exactly that gap.
    """
    captured = {}

    class FakeReport:
        counts = {"pages": 1, "listed": 0, "seen": 0, "new": 0}
        rows = []
        coverage = {}
        image_stats = {"urls": 0, "downloaded": 0, "failed": 0, "bytes": 0,
                       "listings_with_images": 0, "attempted": True, "saved_this_run": 0}
        warnings = []
        ok = True

    def fake_run_trial(conf, conn, search, fetcher=None, download_images=True):
        captured["search"] = search
        captured["max_per_listing"] = conf.images.max_per_listing
        captured["delays"] = conf.scrape.page_delay_range
        return FakeReport()

    monkeypatch.setattr("karpm.trial.run_trial", fake_run_trial)
    monkeypatch.setattr("karpm.trial.render", lambda report, limit_note="": "RENDERED")

    config = tmp_path / "config.toml"
    config.write_text('db_path = "x.db"\n', encoding="utf-8")
    main(["-c", str(config), "trial", "--url", "https://example.invalid/s",
          "--db", str(tmp_path / "t.db"), *extra_argv])
    return captured


def test_trial_defaults_to_the_first_five_ads(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, ["--no-images"])
    assert got["search"].max_listings == 5
    assert got["search"].max_pages is None, "pages are walked until the ad limit is met"


def test_max_ads_sets_the_cap(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, ["--max-ads", "40", "--no-images"])
    assert got["search"].max_listings == 40
    assert got["search"].max_pages is None


def test_max_ads_accepts_the_underscore_spelling(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, ["--max_ads", "7", "--no-images"])
    assert got["search"].max_listings == 7


def test_all_ads_removes_the_cap(monkeypatch, tmp_path):
    for flag in ("--all-ads", "--all_ads"):
        got = _run_trial_capturing(monkeypatch, tmp_path, [flag, "--no-images"])
        assert got["search"].max_listings is None
        assert got["search"].max_pages is None


@pytest.mark.parametrize("argv", [
    ["trial", "--max-ads", "5", "--all-ads"],     # the value equals the default
    ["trial", "--max-ads", "7", "--all-ads"],
    ["trial", "--max_ads", "7", "--all_ads"],
])
def test_max_ads_and_all_ads_are_mutually_exclusive(argv):
    """Parsed through the parser directly: a regression here used to let the
    command through and make real requests."""
    from karpm.cli import build_parser
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(argv)
    assert excinfo.value.code == 2


def test_max_ads_default_is_resolved_in_code_not_by_argparse():
    """argparse skips the exclusion check when a value equals its default, so
    the default has to be None and applied afterwards."""
    from karpm.cli import build_parser
    assert build_parser().parse_args(["trial"]).max_ads is None


def test_all_images_lifts_the_per_listing_cap(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, ["--all-images"])
    assert got["max_per_listing"] is None


def test_without_all_images_the_configured_cap_applies(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, [])
    assert got["max_per_listing"] == 12


def test_make_and_model_apply_when_using_a_named_search(monkeypatch, tmp_path):
    """They used to be accepted alongside --search and then quietly ignored."""
    captured = _run_trial_capturing(
        monkeypatch, tmp_path, ["--make", "BMW", "--model", "R 1200 GS", "--no-images"])
    assert captured["search"].make == "BMW"
    assert captured["search"].model == "R 1200 GS"


def test_trial_runs_at_the_testing_pace_but_polite_overrides(monkeypatch, tmp_path):
    fast = _run_trial_capturing(monkeypatch, tmp_path, ["--no-images"])
    assert fast["delays"] == (0.5, 1.5)
    polite = _run_trial_capturing(monkeypatch, tmp_path, ["--no-images", "--polite"])
    assert polite["delays"] == (4.0, 9.0)
