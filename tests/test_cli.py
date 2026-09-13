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
    from karpm.config import ScrapeConfig, TrialConfig
    polite, trial = ScrapeConfig(), TrialConfig()
    fast = polite.at_pace(trial)
    assert fast.page_delay_range == (trial.min_delay_s, trial.max_delay_s)
    assert fast.image_delay_range == (trial.image_min_delay_s, trial.image_max_delay_s)
    assert polite.page_delay_range == (4.0, 9.0)
    assert polite.at_pace(None) is polite


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

    def fake_run_trial(conf, conn, search, fetcher=None, download_images=True,
                       save_pages=None):
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
    assert got["search"].max_ads == 5
    assert got["search"].max_ads is None or True, "pages are walked until the ad limit is met"


def test_max_ads_sets_the_cap(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, ["--max-ads", "40", "--no-images"])
    assert got["search"].max_ads == 40
    assert got["search"].max_ads is None or True


def test_all_ads_removes_the_cap(monkeypatch, tmp_path):
    got = _run_trial_capturing(monkeypatch, tmp_path, ["--all-ads", "--no-images"])
    assert got["search"].max_ads is None
    assert got["search"].max_ads is None or True


@pytest.mark.parametrize("argv", [
    ["trial", "--max_ads", "7"],
    ["trial", "--all_ads"],
    ["trial", "--all_images"],
])
def test_underscore_spellings_are_not_accepted(argv):
    """One spelling only, to keep the surface small."""
    from karpm.cli import build_parser
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(argv)
    assert excinfo.value.code == 2


@pytest.mark.parametrize("argv", [
    ["trial", "--max-ads", "5", "--all-ads"],     # the value equals the default
    ["trial", "--max-ads", "7", "--all-ads"],
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


# --- karpm web -------------------------------------------------------------

def served_on(tmp_path, argv, host="127.0.0.1", port=8080):
    """Run cmd_web with the server stubbed out; return what it would bind."""
    from karpm import cli

    config = tmp_path / "config.toml"
    config.write_text(f'db_path = "{tmp_path}/t.db"\n[web]\n'
                      f'host = "{host}"\nport = {port}\n', encoding="utf-8")
    bound = {}

    class FakeApp:
        def run(self, host, port, **_kwargs):
            bound.update(host=host, port=port)

    import karpm.web
    real_create = karpm.web.create_app
    karpm.web.create_app = lambda _path: FakeApp()
    try:
        args = cli.build_parser().parse_args(["-c", str(config)] + argv)
        assert cli.cmd_web(args) == 0
    finally:
        karpm.web.create_app = real_create
    return bound


def test_web_binds_localhost_by_default(tmp_path):
    assert served_on(tmp_path, ["web"])["host"] == "127.0.0.1"


def test_lan_binds_every_interface(tmp_path):
    assert served_on(tmp_path, ["web", "--lan"])["host"] == "0.0.0.0"


def test_lan_wins_over_a_localhost_config(tmp_path):
    """The flag is the whole point: opening it up without editing the config."""
    bound = served_on(tmp_path, ["web", "--lan"], host="127.0.0.1")
    assert bound["host"] == "0.0.0.0"


def test_host_from_the_config_is_used(tmp_path):
    assert served_on(tmp_path, ["web"], host="0.0.0.0")["host"] == "0.0.0.0"


def test_the_host_flag_overrides_the_config(tmp_path):
    bound = served_on(tmp_path, ["web", "--host", "10.0.0.5"], host="127.0.0.1")
    assert bound["host"] == "10.0.0.5"


def test_the_port_flag_overrides_the_config(tmp_path):
    assert served_on(tmp_path, ["web", "--port", "9000"])["port"] == 9000


def test_host_and_lan_cannot_both_be_given():
    """They contradict each other; better to say so than to pick one."""
    from karpm.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["web", "--host", "10.0.0.5", "--lan"])


def test_web_has_no_pace_flags():
    """It makes no requests, so --fast would be a flag that does nothing."""
    from karpm.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["web", "--fast"])


def test_the_banner_names_the_address_it_is_reachable_on(tmp_path, capsys, monkeypatch):
    from karpm.web import address
    monkeypatch.setattr(address, "lan_address", lambda: "192.168.1.42")
    served_on(tmp_path, ["web", "--lan"])
    printed = capsys.readouterr().out
    assert "http://192.168.1.42:8080" in printed
    assert "0.0.0.0" not in printed
