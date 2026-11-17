"""The bad-split repair (construct_replacement_list, fix_artists_list): a name a source split in two, as "Simon" and
"Garfunkel" beside "Simon & Garfunkel" on another track, is joined back where it was split, and only there."""

from salmon.tagger.sources.base import generate_artists


def _tracks(*credits: list[tuple[str, str]]) -> dict:
    return {"1": {str(n): {"artists": list(c)} for n, c in enumerate(credits, 1)}}


def _repaired(*credits: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    _artists, tracks = generate_artists(_tracks(*credits))
    return [track["artists"] for track in tracks["1"].values()]


SPLIT = [("Prince", "main"), ("Simon", "guest"), ("Garfunkel", "guest")]
WHOLE = [("Simon & Garfunkel", "main")]


def test_a_repaired_name_is_not_added_to_a_track_that_never_had_it() -> None:
    assert _repaired(SPLIT, [("Prince", "main"), ("Dixon", "remixer")], WHOLE)[1] == [
        ("Prince", "main"),
        ("Dixon", "remixer"),
    ]


def test_a_repaired_name_takes_only_the_role_its_parts_had() -> None:
    assert _repaired(SPLIT, WHOLE)[0] == [("Prince", "main"), ("Simon & Garfunkel", "guest")]


def test_as_before_a_name_split_in_two_is_joined_back() -> None:
    assert _repaired([("Earth", "main"), ("Wind & Fire", "main")], [("Earth, Wind & Fire", "main")]) == [
        [("Earth, Wind & Fire", "main")],
        [("Earth, Wind & Fire", "main")],
    ]


def test_as_before_a_track_already_holding_the_name_keeps_it_once() -> None:
    assert _repaired([("Simon", "main"), ("Garfunkel", "main"), ("Simon & Garfunkel", "main")])[0] == [
        ("Simon & Garfunkel", "main")
    ]


def test_a_name_whose_longer_part_comes_first_is_joined_back() -> None:
    assert _repaired([("Leslie Odom", "main"), ("Jr.", "main")], [("Leslie Odom, Jr.", "main")])[0] == [
        ("Leslie Odom, Jr.", "main")
    ]


def test_a_part_two_names_share_joins_only_the_name_whose_parts_are_all_there() -> None:
    repaired = _repaired(
        [("Sammy Davis", "guest"), ("Jr.", "guest")],
        [("Leslie Odom", "main"), ("Jr.", "main")],
        [("Sammy Davis, Jr.", "main"), ("Leslie Odom, Jr.", "main")],
    )

    assert repaired[:2] == [[("Sammy Davis, Jr.", "guest")], [("Leslie Odom, Jr.", "main")]]


def test_a_name_credited_in_two_roles_is_not_taken_for_two_parts() -> None:
    credits = [("Duran", "main"), ("Duran", "remixer")]

    assert _repaired(credits, [("Duran Duran", "main")]) == [credits, [("Duran Duran", "main")]]


def test_a_name_of_punctuation_only_is_not_taken_for_a_part() -> None:
    credits = [("Prince", "main"), ("!!!", "remixer")]

    assert _repaired(credits, [("!!!", "main")]) == [credits, [("!!!", "main")]]


def test_the_release_s_artists_are_its_tracks_artists_once_repaired() -> None:
    artists, tracks = generate_artists(_tracks([("Simon", "guest")], [("Garfunkel", "guest")], WHOLE))

    assert artists == [("Simon", "guest"), ("Garfunkel", "guest"), ("Simon & Garfunkel", "main")]
    assert [track["artists"] for track in tracks["1"].values()] == [
        [("Simon", "guest")],
        [("Garfunkel", "guest")],
        WHOLE,
    ]


def test_a_name_of_punctuation_only_beside_another_in_one_role_is_kept() -> None:
    credits = [("Prince", "main"), ("!!!", "main")]

    assert _repaired(credits) == [credits]


def test_a_release_whose_mains_include_a_name_of_punctuation_only_keeps_every_credit() -> None:
    credits = [("$$", "main"), ("Alice", "main"), ("Bob", "main")]

    artists, tracks = generate_artists(_tracks(credits))

    assert (artists, tracks["1"]["1"]["artists"]) == (credits, credits)


def test_a_part_two_names_share_joins_each_name_only_where_its_own_parts_are() -> None:
    assert _repaired(
        [("DJ", "main"), ("Shadow", "main")],
        [("DJ", "main"), ("Premier", "main")],
        [("DJ Shadow", "main")],
        [("DJ Premier", "main")],
    )[:2] == [[("DJ Shadow", "main")], [("DJ Premier", "main")]]


def test_a_name_joined_back_takes_its_first_part_s_place_on_the_track_and_the_release() -> None:
    artists, tracks = generate_artists(
        _tracks([("Simon", "main"), ("Garfunkel", "main"), ("Prince", "guest")], [("Prince", "guest")], WHOLE)
    )

    assert tracks["1"]["1"]["artists"] == [("Simon & Garfunkel", "main"), ("Prince", "guest")]
    assert artists == [("Simon & Garfunkel", "main"), ("Prince", "guest")]


def test_a_name_joined_back_beside_its_whole_self_takes_the_first_place_either_held() -> None:
    parts_first = [("Simon", "main"), ("Garfunkel", "main"), ("Prince", "guest"), ("Simon & Garfunkel", "main")]
    whole_first = [("Simon & Garfunkel", "main"), ("Prince", "guest"), ("Simon", "main"), ("Garfunkel", "main")]

    assert _repaired(parts_first, whole_first) == [[("Simon & Garfunkel", "main"), ("Prince", "guest")]] * 2


def test_a_name_joined_beside_its_whole_self_in_another_performing_role_is_credited_once() -> None:
    assert _repaired([("Tyler, The Creator", "main"), ("Tyler", "guest"), ("The Creator", "guest")]) == [
        [("Tyler, The Creator", "main")]
    ]
