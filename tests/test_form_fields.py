"""The config form's field spec, checked against the dataclasses it describes.

The point of these is drift: a setting added to karpm/config.py but not to the
form would silently never be editable, and one removed would leave a field that
writes a key nothing reads.
"""

import typing

import pytest

from karpm import config
from karpm.web import fields

HOLDERS = {
    "": config.Config, "scrape": config.ScrapeConfig, "trial": config.TrialConfig,
    "images": config.ImageConfig, "scoring": config.ScoringConfig,
    "email": config.EmailConfig, "schedule": config.ScheduleConfig,
    "web": config.WebConfig,
}

# Config.searches is edited on its own page, not as a config section. The other
# Config fields are the sections themselves, which have their own entries here.
NESTED = set(HOLDERS.values())
NOT_ON_THE_CONFIG_PAGE = {("", "searches")}

KIND_FOR = {
    str: "text", int: "int", float: "float", bool: "bool",
    list[str]: "lines", list[float]: "numbers",
    int | None: "int", str | None: "text",
}


def declared(cls):
    hints = typing.get_type_hints(cls)
    return {name: hints[name] for name in cls.__dataclass_fields__}


def test_every_section_has_a_dataclass():
    assert {s.name for s in fields.SECTIONS} == set(HOLDERS)


@pytest.mark.parametrize("section", fields.SECTIONS, ids=lambda s: s.name or "top-level")
def test_every_setting_has_a_field(section):
    expected = {name for name, hint in declared(HOLDERS[section.name]).items()
                if hint not in NESTED
                and (section.name, name) not in NOT_ON_THE_CONFIG_PAGE}
    assert {spec.key for spec in section.fields} == expected


@pytest.mark.parametrize("section", fields.SECTIONS, ids=lambda s: s.name or "top-level")
def test_every_field_matches_its_declared_type(section):
    hints = declared(HOLDERS[section.name])
    for spec in section.fields:
        if spec.kind == "choice":
            assert hints[spec.key] is str
            continue
        assert spec.kind == KIND_FOR[hints[spec.key]], f"{section.name}.{spec.key}"


@pytest.mark.parametrize("section", fields.SECTIONS, ids=lambda s: s.name or "top-level")
def test_optional_matches_whether_none_is_allowed(section):
    """A blank in a non-optional field is an error; in an optional one it means
    "unset". Getting that backwards writes an empty string where a number goes."""
    hints = declared(HOLDERS[section.name])
    for spec in section.fields:
        allows_none = type(None) in typing.get_args(hints[spec.key])
        assert spec.optional == allows_none, f"{section.name}.{spec.key}"


def test_search_fields_match_searchconfig():
    hints = declared(config.SearchConfig)
    assert {spec.key for spec in fields.SEARCH_FIELDS} == set(hints)
    for spec in fields.SEARCH_FIELDS:
        assert spec.kind == KIND_FOR[hints[spec.key]], spec.key
        assert spec.optional == (type(None) in typing.get_args(hints[spec.key])), spec.key


def test_kinds_are_ones_the_template_knows():
    every = [s for section in fields.SECTIONS for s in section.fields]
    every += list(fields.SEARCH_FIELDS)
    for spec in every:
        assert spec.kind in fields.KINDS
        assert (spec.kind == "choice") == bool(spec.choices)


def test_choices_include_the_current_default():
    for section in fields.SECTIONS:
        for spec in section.fields:
            if spec.kind == "choice":
                current = getattr(HOLDERS[section.name](), spec.key)
                assert current in spec.choices
