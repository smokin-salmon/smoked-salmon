"""Code patterns that have already caused a bug here may not spread (#932).

No isdigit() in src/salmon at all. Each other pattern's uses are held to at most its entry in ALLOWED. A new use fails:
write what the entry says instead. A fix that removes a use passes, so two PRs can't turn master red between them.
Lower an entry when convenient.
"""

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _isdigit_uses(path: Path) -> list[int]:
    """The lines using ``isdigit``, called or passed; a mention in a docstring or a comment isn't one."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "isdigit"]


def _type_word_cuts(path: Path) -> list[int]:
    """The lines of a regex that takes "EP" or "Single" from the middle of a title; the bare words are type names."""

    def cuts(text: str) -> bool:
        try:
            lowered = text.lower()
            return text not in ("EP", "Single") and bool(
                ("ep" in lowered and re.search(text, "An EP Thing"))
                or ("single" in lowered and re.search(text, "A Single Thing"))
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


def test_no_isdigit_in_src() -> None:
    uses = _uses(_isdigit_uses)
    assert not uses, f'isdigit() is true for "12²", which int() refuses (#910). Use isdecimal() or numeric_id: {uses}'


def test_no_known_bug_pattern_spreads() -> None:
    for pattern, (find, allowed, instead) in ALLOWED.items():
        uses = _uses(find)
        assert len(uses) <= allowed, f"A new use of {pattern}. {instead} Uses now: {uses}"


def test_the_isdigit_ban_finds_a_use_not_a_mention(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text(
        'x.isdigit()\nx.isascii() and x.isdigit()\n# x.isdigit()\n"x.isdigit()"\nfilter(str.isdigit, x)\n',
        encoding="utf-8",
    )

    assert sorted(_isdigit_uses(source)) == [1, 2, 5]


def test_the_isdigit_ban_fails_on_a_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src/salmon").mkdir(parents=True)
    (tmp_path / "src/salmon/source.py").write_text("x.isdigit()\n", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)

    with pytest.raises(AssertionError, match="source.py:1"):
        test_no_isdigit_in_src()


def test_the_type_word_finder_counts_a_regex_that_cuts_from_the_middle(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text(
        'CUT = r"(?:EP|Single)\\b"\nANCHORED = r"(?:EP|Single)$"\nONE = r"Single\\b"\nTYPE = "EP"\n'
        'ANY_CASE = r"(?i)single\\b"\n',
        encoding="utf-8",
    )

    assert _type_word_cuts(source) == [1, 3, 5]
