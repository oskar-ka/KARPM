"""Config loading, and the type checks that stand between a typo and a NULL."""

import pytest

from karpm.config import ConfigError, load_config

# Appended last, because a bare key written after a table belongs to that table.
SEARCH = """
[[searches]]
name = "mt07"
url = "https://www.kleinanzeigen.de/s-motorraeder-roller/yamaha-mt-07/k0c305"
make = "Yamaha"
model = "MT-07"
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text + SEARCH, encoding="utf-8")
    return path


def test_example_config_loads():
    conf = load_config("config.example.toml")
    assert conf.searches and conf.searches[0].model == "MT-07"
    assert conf.web.host == "127.0.0.1"
    # The trial pace must actually be quicker than production, or --fast is a lie.
    assert conf.trial.max_delay_s < conf.scrape.max_delay_s


def test_unknown_keys_are_ignored(tmp_path):
    """An old or misspelled key should not stop the daemon from starting."""
    conf = load_config(write(tmp_path, "[scrape]\nmax_pages = 4\nmin_delay_s = 1.0\n"))
    assert conf.scrape.min_delay_s == 1.0


@pytest.mark.parametrize("text, expected", [
    ('db_path = 42\n', "db_path: expected text"),
    ('[scrape]\nmin_delay_s = "not a number"\n', "scrape.min_delay_s: expected a number"),
    ('[images]\nenabled = "yes"\n', "images.enabled: expected true or false"),
    ('[scrape]\nretry_delays_s = [5, "ten"]\n', "scrape.retry_delays_s[1]: expected a number"),
    ('[web]\nport = "8080"\n', "web.port: expected a whole number"),
    ('[email]\nto_addresses = "you@example.com"\n', "email.to_addresses: expected a list"),
])
def test_wrong_types_are_rejected(tmp_path, text, expected):
    """Dataclasses do not enforce annotations, so these would otherwise load and
    fail much later - or, worse, quietly write NULLs."""
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    assert expected in str(exc.value)


def test_search_entry_is_checked_with_its_index(tmp_path):
    """Which of several searches is wrong matters more than what is wrong."""
    path = write(tmp_path, "")
    path.write_text(path.read_text(encoding="utf-8")
                    + '\n[[searches]]\nname = "x"\nurl = "u"\nmax_ads = "lots"\n',
                    encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "searches[1].max_ads" in str(exc.value)


def test_a_search_without_a_url_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[searches]]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_int_is_accepted_where_a_float_is_declared(tmp_path):
    """`min_delay_s = 2` is TOML for an integer, and is not a mistake."""
    conf = load_config(write(tmp_path, "[scrape]\nmin_delay_s = 2\n"))
    assert conf.scrape.min_delay_s == 2.0
    assert isinstance(conf.scrape.min_delay_s, float)


def test_bool_is_not_an_int(tmp_path):
    """bool subclasses int, so the bool check has to come first."""
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "[scoring]\nmax_images = true\n"))
