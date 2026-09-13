"""Editing config.toml in place.

config.toml is a file people write in. The form could serialise a whole config
back out, but that would throw away every comment and blank line in it, so each
value is edited on the line it already occupies. These tests are mostly about
what must *survive* a save.
"""

import tomllib

import pytest

from karpm.web import tomledit

SAMPLE = '''# KARPM configuration
db_path = "data/karpm.db"

[[searches]]
name = "mt07"
url = "https://example.com/mt07"
max_ads = 100

[scrape]
# Deliberately slow - this is rate limiting.
min_delay_s = 4.0
max_delay_s = 9.0   # the upper end
retry_delays_s = [5, 10, 20, 40]

[images]
enabled = true
dir = "data/images"
'''


def loads(text):
    return tomllib.loads(text)


def test_a_value_is_changed_where_it_stands():
    out = tomledit.set_values(SAMPLE, {"scrape": {"min_delay_s": 1.5}})
    assert loads(out)["scrape"]["min_delay_s"] == 1.5
    assert "# Deliberately slow - this is rate limiting." in out


def test_comments_and_blank_lines_survive():
    out = tomledit.set_values(SAMPLE, {"images": {"dir": "/srv/photos"}})
    assert out.startswith("# KARPM configuration")
    assert loads(out)["images"]["dir"] == "/srv/photos"


def test_an_inline_comment_stays_on_its_line():
    out = tomledit.set_values(SAMPLE, {"scrape": {"max_delay_s": 6.0}})
    assert "max_delay_s = 6.0  # the upper end" in out


def test_a_hash_inside_a_string_is_not_a_comment():
    text = 'subject_prefix = "[KARPM #1]"\n'
    out = tomledit.set_values(text, {"": {"subject_prefix": "[KARPM #2]"}})
    assert loads(out)["subject_prefix"] == "[KARPM #2]"
    assert "#1" not in out


def test_a_missing_key_is_added_to_its_table():
    out = tomledit.set_values(SAMPLE, {"images": {"max_bytes": 1000}})
    assert loads(out)["images"]["max_bytes"] == 1000
    # and lands inside [images], not at the end of the file under nothing
    assert out.index("[images]") < out.index("max_bytes")


def test_a_missing_table_is_added():
    out = tomledit.set_values(SAMPLE, {"web": {"host": "127.0.0.1", "port": 8080}})
    assert loads(out)["web"] == {"host": "127.0.0.1", "port": 8080}


def test_two_new_keys_in_different_tables():
    out = tomledit.set_values(SAMPLE, {"images": {"max_bytes": 1}, "scrape": {"timeout_s": 30.0}})
    parsed = loads(out)
    assert parsed["images"]["max_bytes"] == 1 and parsed["scrape"]["timeout_s"] == 30.0


def test_keys_inside_a_search_table_are_never_touched():
    """A key inside [[searches]] shares its name with nothing above it. Editing a
    top-level key must not reach into the block that happens to use that name."""
    out = tomledit.set_values(SAMPLE, {"": {"max_ads": 7}})
    parsed = loads(out)
    assert parsed["searches"][0]["max_ads"] == 100, "the search's own value stands"
    assert parsed["max_ads"] == 7, "and the new top-level key went in above it"


def test_a_top_level_key_is_not_swallowed_by_a_table():
    out = tomledit.set_values(SAMPLE, {"": {"db_path": "moved.db"}})
    assert loads(out)["db_path"] == "moved.db"


def test_removing_a_key_restores_the_default():
    out = tomledit.remove_keys(SAMPLE, {"scrape": {"max_delay_s"}})
    assert "max_delay_s" not in out
    assert loads(out)["scrape"]["min_delay_s"] == 4.0


def test_removing_does_not_reach_into_a_search():
    out = tomledit.remove_keys(SAMPLE, {"": {"max_ads"}, "images": {"enabled"}})
    parsed = loads(out)
    assert parsed["searches"][0]["max_ads"] == 100
    assert "enabled" not in parsed["images"]


@pytest.mark.parametrize("value, expected", [
    (True, "true"), (False, "false"), (3, "3"), (4.0, "4.0"), (0.5, "0.5"),
    ("plain", '"plain"'), ('a "b"', '"a \\"b\\""'),
    ("back\\slash", '"back\\\\slash"'),
    ([1, 2.5], "[1, 2.5]"), (["a", "b"], '["a", "b"]'), ([], "[]"),
])
def test_values_are_rendered_as_the_toml_that_means_them(value, expected):
    assert tomledit.literal(value) == expected


def test_a_bool_is_not_written_as_a_number():
    """bool subclasses int, so the order of those branches matters."""
    assert tomledit.literal(True) == "true"


def test_a_quote_in_a_value_round_trips():
    out = tomledit.set_values('prefix = "x"\n', {"": {"prefix": 'a "b" c'}})
    assert loads(out)["prefix"] == 'a "b" c'


def test_searches_are_replaced_wholesale():
    out = tomledit.set_searches(SAMPLE, [
        {"name": "z900", "url": "https://example.com/z900", "enabled": True},
        {"name": "r1200", "url": "https://example.com/r1200", "enabled": False,
         "make": "BMW", "max_ads": 50},
    ])
    parsed = loads(out)
    assert [s["name"] for s in parsed["searches"]] == ["z900", "r1200"]
    assert parsed["searches"][1]["max_ads"] == 50
    # the rest of the file is untouched
    assert parsed["scrape"]["min_delay_s"] == 4.0
    assert "# Deliberately slow - this is rate limiting." in out


def test_removing_every_search_is_allowed():
    out = tomledit.set_searches(SAMPLE, [])
    assert "searches" not in loads(out)
    assert loads(out)["images"]["enabled"] is True


def test_an_edited_file_can_be_edited_again():
    """A save must not degrade the file a little each time."""
    out = SAMPLE
    for port in (8080, 9090, 7070):
        out = tomledit.set_values(out, {"web": {"port": port}})
    assert loads(out)["web"]["port"] == 7070
    assert out.count("[web]") == 1
    assert out.count("port =") == 1


def test_a_commented_out_search_survives_a_rewrite():
    """Search blocks are regenerated, so their settings cannot be preserved -
    but a commented-out search is a template someone keeps on purpose."""
    text = SAMPLE.replace("[[searches]]", '# [[searches]]\n# name = "spare"\n\n[[searches]]', 1)
    out = tomledit.set_searches(text, [{"name": "z900", "url": "u", "enabled": True}])
    assert '# name = "spare"' in out
    assert loads(out)["searches"] == [{"name": "z900", "url": "u", "enabled": True}]


def test_the_comment_above_the_searches_stays_where_it_is():
    text = SAMPLE.replace("[[searches]]", "# paste search URLs below\n[[searches]]", 1)
    out = tomledit.set_searches(text, [{"name": "z900", "url": "u"}])
    assert "# paste search URLs below" in out
    assert loads(out)["searches"][0]["name"] == "z900"


def test_searches_stay_where_they_are_in_the_file():
    """Rewriting the blocks must not relocate them to the bottom of the file."""
    out = tomledit.set_searches(SAMPLE, [{"name": "z900", "url": "u", "enabled": True}])
    assert out.index("[[searches]]") < out.index("[scrape]")
    assert loads(out)["scrape"]["min_delay_s"] == 4.0


def test_a_first_search_in_a_file_without_any_is_appended():
    text = "db_path = \"x.db\"\n\n[scrape]\nmin_delay_s = 4.0\n"
    out = tomledit.set_searches(text, [{"name": "z900", "url": "u"}])
    assert loads(out)["searches"][0]["name"] == "z900"
    assert loads(out)["scrape"]["min_delay_s"] == 4.0
