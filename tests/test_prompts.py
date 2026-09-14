"""preferences.md as five boxes: splitting it and compiling it back.

The file is still one markdown document that goes to pass 3 whole. Only the way
it is edited changed - so what has to hold is that a file this page wrote
survives the round trip unchanged, and that a save which changes nothing writes
nothing.
"""

import pytest

from karpm import preferences as pr

FILLED = {
    "about": "A do-everything travel enduro.",
    "needs": "- Under 60,000 km\n- Full service history",
    "likes": "- Panniers",
    "unimportant": "- Colour",
    "logistics": "- Up to 300 km",
}


def test_the_boxes_compile_into_headings():
    written = pr.compile(FILLED)
    assert written.startswith("## About the bike\n\nA do-everything travel enduro.")
    for part in pr.PARTS:
        assert f"## {part.heading}" in written


def test_what_we_write_we_can_read_back():
    assert pr.split(pr.compile(FILLED)) == FILLED


def test_compiling_is_stable():
    """A save that changes nothing must not rewrite the file - otherwise every
    visit to the page looks like an edit."""
    once = pr.compile(FILLED)
    assert pr.compile(pr.split(once)) == once


def test_an_empty_box_keeps_its_heading():
    """"I do not care about this" is worth telling the model, and a missing
    section reads as an oversight rather than as an answer."""
    written = pr.compile({**FILLED, "unimportant": ""})
    assert "## What is not important" in written


def test_a_heading_we_know_is_picked_up_from_a_hand_written_file():
    values = pr.split("## Logistics\n\n- 300 km\n\n## Nonsense\n\nsomething\n")
    assert values["logistics"] == "- 300 km"


def test_only_the_five_headings_survive_a_save():
    """The boxes are the file. Anything else it held is not shown and is not
    written back - the .bak beside the file is what that leans on."""
    values = pr.split("Loose prose.\n\n## Logistics\n\n- 300 km\n\n## Mine\n\ngone\n")
    written = pr.compile(values)
    assert "- 300 km" in written
    assert "Loose prose." not in written and "## Mine" not in written


def test_the_same_heading_twice_keeps_both():
    """Dropping one would lose something the owner meant to say."""
    values = pr.split("## Logistics\n\n- 300 km\n\n## Logistics\n\n- and a van\n")
    assert "- 300 km" in values["logistics"] and "- and a van" in values["logistics"]


@pytest.mark.parametrize("text", ["", None, "   \n\n"])
def test_an_empty_file_is_empty_boxes_not_a_crash(text):
    values = pr.split(text)
    assert all(values[part.key] == "" for part in pr.PARTS)
