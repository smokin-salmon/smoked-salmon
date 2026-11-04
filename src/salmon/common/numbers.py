"""Track and disc numbers: how a tag's value or a store's text reads as one, and how such text sorts.

``TagFile`` gives a number field as text, or None when absent. Only tests' hand-built tags hold an int or a list. A
policy built on these, such as which numbers a rename refuses or whether a store's numbers can be used, stays with
the code it decides for.
"""

import re
from typing import Any


def number_text(value: Any) -> str:
    """The number part of a tag's value, as written: "03/12" gives "03", and " 3" gives "3"."""
    return str(value).split("/")[0].strip()


def read_number(value: Any) -> int | None:
    """A tag's value as a number (a list's first item), or None when it is absent or isn't one.

    ``isdecimal()``, not ``isdigit()``: ``isdigit()`` is also true for some digits (superscript "2") ``int()`` refuses.
    """
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        text = number_text(value)
        return int(text) if text.isdecimal() else None
    return value if isinstance(value, int) else None


def tag_value(tags: Any, field: str) -> Any:
    """A tag object's or dict's value for ``field``; None when it has none."""
    return tags.get(field) if isinstance(tags, dict) else getattr(tags, field, None)


def tag_number(tags: Any, field: str) -> int | None:
    """The tag's number, or None when it is absent or isn't one (unlike ``tag_number_or_one``, no default)."""
    return read_number(tag_value(tags, field))


def tag_number_or_one(tags: Any, field: str) -> int:
    """The tag's number, defaulting to 1."""
    number = tag_number(tags, field)
    return 1 if number is None else number


def tag_text(tags: Any, field: str) -> str:
    """The tag's value as written (its first, if a list); "" when it has none."""
    value = tag_value(tags, field)
    if isinstance(value, list):
        value = value[0] if value else None
    return "" if value is None else str(value)


def unreadable_number(tags: Any, field: str) -> str | None:
    """A disc or track number that is there but can't be read as one ("CD1"), as written; None otherwise."""
    text = tag_text(tags, field)
    return text if text.strip() and tag_number(tags, field) is None else None


def natural_key(value: Any) -> tuple[tuple[int, int | str], ...]:
    """Sort key that compares digit runs as numbers: CD2 sorts before CD10, and track A2 before A10."""
    return tuple(
        (0, int(part)) if part.isdecimal() else (1, part.lower()) for part in re.split(r"(\d+)", str(value)) if part
    )


def leading_number_key(name: str) -> tuple[int, int | str]:
    """Sort key for a file name or relative path; only its start counts: a leading number, before the rest by name."""
    match = re.match(r"\d+", name)
    return (0, int(match.group())) if match else (1, name.lower())


def padded_number(value: Any) -> str:
    """A number as a tracklist or file name gives it, two digits at least; anything else ("A1", None) as its text."""
    text = str(value)
    return f"{int(text):02d}" if text.isdecimal() else text
