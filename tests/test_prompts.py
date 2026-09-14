"""preferences.md as five boxes: splitting it, compiling it, and losing nothing.

The file is still one markdown document that goes to pass 3 whole. Only the way
it is edited changed - so the only thing that really has to hold is that a file
survives the round trip, including one this code never wrote.
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
    written = pr.compile(FILLED)
    assert {k: v for k, v in pr.split(written).items() if k != pr.REST} == FILLED


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


def test_a_file_written_by_hand_is_kept_whole():
    """It is a file people wrote in long before it had boxes."""
    original = "I want a cheap GS.\n\n## My own heading\n\nkeep this\n"
    values = pr.split(original)
    assert values["about"] == "" and "I want a cheap GS." in values[pr.REST]
    assert "## My own heading" in values[pr.REST] and "keep this" in values[pr.REST]

    written = pr.compile(values)
    for line in ("I want a cheap GS.", "## My own heading", "keep this"):
        assert line in written


def test_a_heading_we_know_is_picked_up_from_a_hand_written_file():
    values = pr.split("## Logistics\n\n- 300 km\n\n## Nonsense\n\nkeep\n")
    assert values["logistics"] == "- 300 km"
    assert "keep" in values[pr.REST]


def test_the_same_heading_twice_keeps_both():
    """Dropping one would lose something the owner meant to say."""
    values = pr.split("## Logistics\n\n- 300 km\n\n## Logistics\n\n- and a van\n")
    assert "- 300 km" in values["logistics"] and "- and a van" in values["logistics"]


@pytest.mark.parametrize("text", ["", None, "   \n\n"])
def test_an_empty_file_is_empty_boxes_not_a_crash(text):
    values = pr.split(text)
    assert all(values[part.key] == "" for part in pr.PARTS)
