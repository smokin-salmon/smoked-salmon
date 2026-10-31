import pytest

from salmon.tagger.pre_data import parse_artists


def test_feat_keeps_the_disambiguation_bracket_balanced() -> None:
    # Tag of Discogs release 2520452, track 4-11
    assert parse_artists("Shawfire feat. Ultra (27) & Tullisa") == [
        ("Ultra", "guest"),
        ("Tullisa", "guest"),
        ("Shawfire", "main"),
    ]


def test_numbered_suffix_is_stripped_from_main_artists() -> None:
    assert parse_artists("Selena (5) & Other (12)") == [("Selena", "main"), ("Other", "main")]


def test_numbered_suffix_is_stripped_inside_a_joined_credit() -> None:
    # Tag of Discogs release 2520452, track 1-16: " vs. " does not split artists
    assert parse_artists("Selena (5) vs. X-Men") == [("Selena vs. X-Men", "main")]


def test_numbered_suffix_is_stripped_from_guest_in_brackets() -> None:
    assert parse_artists("Lead (feat. Guest (3))") == [("Guest", "guest"), ("Lead", "main")]


def test_bracketed_feat_forms_still_work() -> None:
    assert parse_artists("Artist (feat. Guest)") == [("Guest", "guest"), ("Artist", "main")]
    assert parse_artists("Artist [ft. Guest]") == [("Guest", "guest"), ("Artist", "main")]
    assert parse_artists("Artist (feat. One & Two) & Other") == [
        ("One", "guest"),
        ("Two", "guest"),
        ("Artist", "main"),
        ("Other", "main"),
    ]


def test_plain_feat_form_still_works() -> None:
    assert parse_artists("Artist feat. One & Two") == [("One", "guest"), ("Two", "guest"), ("Artist", "main")]


def test_non_numeric_bracket_is_kept() -> None:
    assert parse_artists("Artist (UK)") == [("Artist (UK)", "main")]
    assert parse_artists("Artist (UK) feat. Guest (US)") == [("Guest (US)", "guest"), ("Artist (UK)", "main")]


def test_list_of_artists_is_parsed_per_item() -> None:
    assert parse_artists(["Selena (5)", "X-Men"]) == [("Selena", "main"), ("X-Men", "main")]


@pytest.mark.parametrize(
    "tag",
    [
        "Dree Low (remixed by Owen)",
        "Dree Low remixed by Owen",
        "Dree Low (remixed Owen)",
        "Dree Low (remix Owen)",
        "Dree Low (Remixed By Owen)",
        "Dree Low (Remixed by Owen)",
        "Dree Low (remixed By Owen)",
        "Dree Low (remix by Owen)",
        "Dree Low (Remix by Owen)",
    ],
)
def test_a_remix_credit_names_the_remixer(tag) -> None:
    assert parse_artists(tag) == [("Owen", "remixer"), ("Dree Low", "main")]


def test_a_name_with_remix_in_it_stays_one_artist() -> None:
    assert parse_artists("The Remix Project") == [("The Remix Project", "main")]
