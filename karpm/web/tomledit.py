"""Edit config.toml in place, value by value.

The form could serialise a whole config back out, but config.toml is a file
people write in, and round-tripping it through a parser would throw away every
comment and every blank line they put there. So each value is edited on the
line it already occupies, and only keys that are genuinely new are appended.
"""

from __future__ import annotations

import re

SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]")
ARRAY_RE = re.compile(r"^\s*\[\[([^\[\]]+)\]\]")


def escape(value: str) -> str:
    """A TOML basic string. Control characters are not worth supporting here."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    return out.replace("\n", " ").replace("\r", "").replace("\t", " ")


def literal(value) -> str:
    """Render a Python value as the TOML that means it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # repr keeps the dot that makes TOML read it as a float.
        return repr(float(value))
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    return f'"{escape(str(value))}"'


def _comment_at(line: str) -> int:
    """Index of the comment marker, or -1. A # inside a string is not one."""
    quote, skip = None, False
    for index, char in enumerate(line):
        if skip:
            skip = False
        elif quote:
            if char == "\\":
                skip = True             # whatever follows is escaped, not a delimiter
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return index
    return -1


def _key_pattern(key: str) -> re.Pattern:
    return re.compile(rf"^(\s*){re.escape(key)}\s*=")


def set_values(text: str, updates: dict[str, dict]) -> str:
    """Apply {section: {key: value}} to `text`. "" is the top-level table.

    A key already in the file is rewritten where it stands, keeping its
    indentation and any trailing comment. A key that is missing is appended to
    its table, and a table that is missing is added at the end.
    """
    lines = text.splitlines()
    remaining = {name: dict(values) for name, values in updates.items() if values}
    # Last line index belonging to each table, so a new key lands inside it.
    ends: dict[str, int] = {}
    current, in_array = "", False

    for index, line in enumerate(lines):
        array = ARRAY_RE.match(line)
        header = None if array else SECTION_RE.match(line)
        if array:
            current, in_array = array.group(1).strip(), True
            continue
        if header:
            current, in_array = header.group(1).strip(), False
            ends.setdefault(current, index)
            continue

        if in_array:
            continue                    # [[searches]] tables are rewritten wholesale
        if line.strip():
            ends[current] = index

        values = remaining.get(current)
        if not values:
            continue
        for key in list(values):
            match = _key_pattern(key).match(line)
            if not match:
                continue
            comment = _comment_at(line)
            trailing = f"  {line[comment:]}" if comment != -1 else ""
            lines[index] = f"{match.group(1)}{key} = {literal(values.pop(key))}{trailing}"
            break

    # Anything left is new: put it at the end of its table, or in a new one.
    for name in list(remaining):
        values = remaining[name]
        if not values:
            continue
        new = [f"{key} = {literal(value)}" for key, value in values.items()]
        if name in ends:
            at = ends[name] + 1
            lines[at:at] = new
            # Everything after this table has shifted down.
            ends = {k: (v + len(new) if v >= at else v) for k, v in ends.items()}
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{name}]")
            lines.extend(new)
            ends[name] = len(lines) - 1

    return "\n".join(lines).rstrip() + "\n"


def remove_keys(text: str, keys: dict[str, set]) -> str:
    """Drop {section: {key, ...}} from the file, so the built-in default applies."""
    lines, keep = text.splitlines(), []
    current, in_array = "", False
    for line in lines:
        array = ARRAY_RE.match(line)
        header = None if array else SECTION_RE.match(line)
        if array:
            current, in_array = array.group(1).strip(), True
        elif header:
            current, in_array = header.group(1).strip(), False
        elif not in_array and any(_key_pattern(k).match(line) for k in keys.get(current, ())):
            continue
        keep.append(line)
    return "\n".join(keep).rstrip() + "\n"


def set_searches(text: str, entries: list[dict]) -> str:
    """Replace every [[searches]] table, leaving the rest of the file alone."""
    lines, kept, notes, skipping = text.splitlines(), [], [], False
    at = None                           # where the searches were, so they stay put
    for line in lines:
        if ARRAY_RE.match(line) and ARRAY_RE.match(line).group(1).strip() == "searches":
            if at is None:
                at = len(kept)
            skipping = True
            continue
        if skipping and SECTION_RE.match(line) and not ARRAY_RE.match(line):
            skipping = False
        if skipping:
            # The settings in a search block are regenerated, but a comment in
            # among them was written by hand - usually a commented-out search
            # kept as a template. Those are carried over rather than dropped.
            if line.strip().startswith("#"):
                notes.append(line)
            continue
        kept.append(line)

    rendered = list(notes)
    if notes:
        rendered.append("")
    for entry in entries:
        rendered.append("[[searches]]")
        for key, value in entry.items():
            if value is None or value == "":
                continue
            rendered.append(f"{key} = {literal(value)}")
        rendered.append("")

    if at is None:                      # no searches yet: they go at the end
        body = "\n".join(kept).rstrip()
        return (body + "\n\n" + "\n".join(rendered)).strip() + "\n"

    kept[at:at] = rendered
    # Collapse the run of blank lines the removal can leave behind.
    out, blank = [], False
    for line in kept:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(line)
    return "\n".join(out).strip() + "\n"
