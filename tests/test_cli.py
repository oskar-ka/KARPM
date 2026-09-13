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
