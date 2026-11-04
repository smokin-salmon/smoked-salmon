"""Code patterns that have already caused a bug here may not spread (#932).

Each pattern's uses in src/salmon are held to its entry in ALLOWED, in the way tests/test_complexity.py holds
complexity. A new use fails: write what the entry says instead. A fix that removes a use fails too, until the entry
comes down with it, so the list only tightens.
"""

import ast
import re
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _isdigit_uses(path: Path) -> list[int]:
    """The lines calling ``isdigit()`` with no ``isascii()`` on the same line; a guard on another line still counts."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "isdigit"
        and "isascii" not in lines[node.lineno - 1]
    ]


def _type_word_cuts(path: Path) -> list[int]:
    """The lines of a regex that takes "EP" or "Single" from the middle of a title."""

    def cuts(text: str) -> bool:
        try:
            return (
                "EP" in text
                and "Single" in text
                and bool(re.search(text, "A Single Thing") or re.search(text, "An EP Thing"))
            )
        except re.error:
            return False

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and cuts(node.value)
    ]


# Pattern: (how to find it, the uses when the entry was last set, what showed the bug and what to write instead).
ALLOWED: dict[str, tuple[Callable[[Path], list[int]], int, str]] = {
    "isdigit() without isascii()": (
        _isdigit_uses,
        13,
        'It is true for "12²", which int() refuses (#910). Use isdecimal() or numeric_id.',
    ),
    # The one left, in _title_words, gives words that are only compared, never searched.
    "a release type cut from a title": (
        _type_word_cuts,
        1,
        'It took part of "The Singles" (#916). Anchor it at the end.',
    ),
}


def _uses(find: Callable[[Path], list[int]]) -> list[str]:
    return [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sorted((ROOT / "src/salmon").rglob("*.py"))
        for line in find(path)
    ]


def test_no_known_bug_pattern_spreads() -> None:
    for pattern, (find, allowed, instead) in ALLOWED.items():
        uses = _uses(find)
        assert len(uses) <= allowed, f"A new use of {pattern}. {instead} Uses now: {uses}"


def test_the_allowed_counts_come_down_with_each_fix() -> None:
    for pattern, (find, allowed, _) in ALLOWED.items():
        uses = _uses(find)
        assert len(uses) >= allowed, f"{pattern} has fewer uses now: lower its entry to {len(uses)}"


def test_the_isdigit_finder_counts_a_call_with_no_guard_on_its_line(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text(
        'x.isdigit()\nx.isascii() and x.isdigit()\n# x.isdigit()\n"x.isdigit()"\n(x.isascii()\n and x.isdigit())\n',
        encoding="utf-8",
    )

    assert _isdigit_uses(source) == [1, 6]


def test_the_type_word_finder_counts_a_regex_that_cuts_from_the_middle(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text('CUT = r"(?:EP|Single)\\b"\nANCHORED = r"(?:EP|Single)$"\n', encoding="utf-8")

    assert _type_word_cuts(source) == [1]
