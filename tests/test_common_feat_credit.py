"""RE_FEAT, the featured credit five store parsers read from a title (#910)."""

import pytest

from salmon.common import RE_FEAT


@pytest.mark.parametrize(
    ("title", "guests", "plain"),
    [
        pytest.param("Song (feat. A (UK))", "A (UK)", "Song", id="a bracket inside the credit"),
        pytest.param("Song [feat. A (UK)]", "A (UK)", "Song", id="in square brackets"),
        pytest.param("Song feat. A (UK)", "A (UK)", "Song", id="no bracket round the credit"),
        pytest.param("Song (feat. A (UK)) [Remix]", "A (UK)", "Song [Remix]", id="a bracket after the credit"),
        pytest.param("Song (feat. A (B (C)))", "A (B (C))", "Song", id="brackets two deep"),
        # A bare credit before a version: the guest gets its bracket, and the title keeps the version, as on master.
        pytest.param("Song feat. A (Live) [Remix]", "A (Live)", "Song [Remix]", id="bare, then two versions"),
        pytest.param("Song feat. A (Live) - 2011 Remaster", "A (Live)", "Song - 2011 Remaster", id="bare, then a dash"),
        pytest.param(
            "Song ft. A & B (Radio Edit) (Remastered 2011)",
            "A & B (Radio Edit)",
            "Song (Remastered 2011)",
            id="bare, then versions",
        ),
    ],
)
def test_a_featured_credit_runs_to_its_own_closing_bracket(title, guests, plain) -> None:
    match = RE_FEAT.search(title)

    assert match is not None
    assert match[1] == guests
    assert RE_FEAT.sub("", title) == plain


@pytest.mark.parametrize(
    ("title", "guests", "plain"),
    [
        pytest.param("Song (feat. A)", "A", "Song", id="in brackets"),
        pytest.param("Song (ft. A & B)", "A & B", "Song", id="ft."),
        pytest.param("Song [featuring A]", "A", "Song", id="featuring"),
        pytest.param("Song feat. A", "A", "Song", id="no bracket"),
        pytest.param("Song (feat. A) [Remix]", "A", "Song [Remix]", id="a bracket after"),
        pytest.param("Song (feat. A) (Live)", "A", "Song (Live)", id="another bracket after"),
        pytest.param("Song (with. A)", "A", "Song", id="with."),
        # A bracket that doesn't close, or guests starting with one, are read as before.
        pytest.param("Song feat. A (UK", "A (UK", "Song", id="a bracket left open"),
        pytest.param("Song (feat. A (B (C (D))))", "A (B (C (D", "Song)))", id="brackets three deep"),
        pytest.param("Song feat. (Remix)", "(Remix", "Song", id="guests starting with a bracket"),
    ],
)
def test_a_featured_credit_is_read_as_before(title, guests, plain) -> None:
    match = RE_FEAT.search(title)

    assert match is not None
    assert match[1] == guests
    assert RE_FEAT.sub("", title) == plain


def test_a_title_without_a_featured_credit_has_none() -> None:
    assert RE_FEAT.search("Song (Live) [Remastered]") is None
