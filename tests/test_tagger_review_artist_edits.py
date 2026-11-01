"""[a]rtists carries its edit to the tracks (#854, #868), and trac[k]s keeps the release's order (#868)."""

import anyio

import salmon.tagger.review as review_module
from salmon.tagger.sources.base import generate_artists

GARY, JAN, PER = "Gary Novak", "Jan Gunnar Hoff", "Per Mathisen"


def _metadata(*tracks: list[tuple[str, str]]) -> dict:
    """A release whose tracks credit these artists, its own artists built from them as the combine step does."""
    metadata = {
        "tracks": {
            "1": {
                str(n): {"title": f"Song {n}", "artists": list(artists), "disc#": "1", "track#": str(n)}
                for n, artists in enumerate(tracks, 1)
            }
        }
    }
    metadata["artists"], metadata["tracks"] = generate_artists(metadata["tracks"])
    return metadata


def _track(metadata: dict, number: int) -> list[tuple[str, str]]:
    return metadata["tracks"]["1"][str(number)]["artists"]


def _run(monkeypatch, edit, metadata: dict, editor) -> list[str]:
    """Run `edit` with the editor returning `editor(text)`; returns what salmon printed."""
    printed: list[str] = []

    async def fake_edit(text, **_kwargs):
        return editor(text)

    monkeypatch.setattr(review_module.interaction, "edit", fake_edit)
    monkeypatch.setattr(review_module.click, "secho", lambda message, **_kwargs: printed.append(str(message)))
    anyio.run(edit, metadata)
    return printed


def _edit_artists(monkeypatch, metadata: dict, lines: list[str]) -> list[str]:
    """Save [a]rtists as `lines`; returns what salmon printed after the editor's intro line."""
    return _run(monkeypatch, review_module._edit_artists, metadata, lambda _text: "\n".join(lines))[1:]


def _remix_metadata() -> dict:
    # The release's artists: Dree Low (main), Owen (guest), Jamm (main), Dree Low (remixer).
    return _metadata([("Dree Low", "main"), ("Owen", "guest")], [("Jamm", "main"), ("Dree Low", "remixer")])


# #854: the roles, by its five rules.


def test_an_unchanged_save_changes_nothing_and_says_nothing(monkeypatch) -> None:
    # Rule 1. Before, a remixer who was also a main artist became main on the remixed track.
    metadata = _remix_metadata()
    before = {"artists": list(metadata["artists"]), "1": _track(metadata, 1), "2": _track(metadata, 2)}

    printed = _run(monkeypatch, review_module._edit_artists, metadata, lambda text: text)

    assert metadata["artists"] == before["artists"]
    assert _track(metadata, 1) == before["1"]
    assert _track(metadata, 2) == before["2"]
    assert printed[1:] == []


def test_one_role_changed_for_another_moves_only_that_roles_credits(monkeypatch) -> None:
    # Rule 2.
    metadata = _remix_metadata()

    _edit_artists(monkeypatch, metadata, ["Dree Low (main)", "Owen (guest)", "Jamm (main)", "Dree Low (guest)"])

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "guest")]


def test_a_guest_made_main_is_main_on_its_tracks(monkeypatch) -> None:
    # Rule 2.
    metadata = _remix_metadata()

    _edit_artists(monkeypatch, metadata, ["Dree Low (main)", "Owen (main)", "Jamm (main)", "Dree Low (remixer)"])

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "main")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "remixer")]


def test_a_line_changed_to_a_role_the_name_already_has_moves_its_credits_there(monkeypatch) -> None:
    # Rule 2, by lines: the remixer line now says main, so Dree Low is main on track 2. By roles alone, Dree Low
    # lost remixer and gained nothing, which rule 3 would read as the remixer line deleted.
    metadata = _remix_metadata()

    printed = _edit_artists(
        monkeypatch, metadata, ["Dree Low (main)", "Owen (guest)", "Jamm (main)", "Dree Low (main)"]
    )

    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "main")]
    assert metadata["artists"] == [("Dree Low", "main"), ("Owen", "guest"), ("Jamm", "main")]
    assert printed == []


def test_a_deleted_role_line_removes_that_credit_from_the_tracks_and_says_where(monkeypatch) -> None:
    # Rule 3; salmon names the artist as the tracks spell it, whatever the edit typed.
    metadata = _remix_metadata()

    printed = _edit_artists(monkeypatch, metadata, ["DREE LOW (main)", "Owen (guest)", "Jamm (main)"])

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert _track(metadata, 2) == [("Jamm", "main")]
    assert printed == ["Removed Dree Low (remixer) from Disc 1 Track 2."]


def test_a_role_changed_while_another_line_is_deleted_still_moves_its_credits(monkeypatch) -> None:
    # Owen's line deleted and Dree Low's remixer line now guest: each name's lines are paired on their own.
    metadata = _remix_metadata()

    _edit_artists(monkeypatch, metadata, ["Dree Low (main)", "Jamm (main)", "Dree Low (guest)"])

    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "guest")]


def test_a_name_gone_from_the_list_stays_on_its_tracks_and_salmon_says_which(monkeypatch) -> None:
    # Rule 4: a track can't tell a typo fixed from an artist removed. Owen is on two tracks, in two roles on one.
    metadata = _metadata(
        [("Jamm", "main"), ("Owen", "guest")], [("Jamm", "main"), ("Owen", "guest"), ("Owen", "remixer")]
    )

    printed = _edit_artists(monkeypatch, metadata, ["Jamm (main)"])

    assert metadata["artists"] == [("Jamm", "main")]
    assert _track(metadata, 1) == [("Jamm", "main"), ("Owen", "guest")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Owen", "guest"), ("Owen", "remixer")]
    assert printed == [
        "Owen is no longer in the release's artists, but is still credited on Disc 1 Tracks 1, 2: remove it from a "
        "track in trac[k]s, or put it back here and rename it everywhere with artist a[L]iases."
    ]


def test_any_other_change_to_a_names_roles_leaves_its_credits_and_says_so(monkeypatch) -> None:
    # Rule 5: Dree Low loses main and remixer and gains guest; which credit becomes the guest can't be told.
    metadata = _remix_metadata()

    printed = _edit_artists(monkeypatch, metadata, ["Dree Low (guest)", "Owen (guest)", "Jamm (main)"])

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "remixer")]
    assert printed == [
        "Dree Low: more than one of its roles changed, so its credits on Disc 1 Tracks 1, 2 are left as they were: "
        "set them in trac[k]s."
    ]


def test_two_lines_of_one_name_changed_while_other_lines_move_are_left_and_salmon_says_so(monkeypatch) -> None:
    # Rule 5, by lines: Owen's guest line to main and remixer line to guest, while Dree Low and Jamm swap places. By
    # roles alone it reads as remixer to main, the wrong way round.
    metadata = _metadata([("Dree Low", "main"), ("Owen", "guest")], [("Jamm", "main"), ("Owen", "remixer")])

    printed = _edit_artists(monkeypatch, metadata, ["Owen (main)", "Jamm (main)", "Dree Low (main)", "Owen (guest)"])

    assert ("Owen", "guest") in _track(metadata, 1)
    assert ("Owen", "remixer") in _track(metadata, 2)
    assert printed == [
        "Owen: more than one of its roles changed, so its credits on Disc 1 Tracks 1, 2 are left as they were: "
        "set them in trac[k]s."
    ]


def test_a_role_added_changes_no_track_and_says_nothing(monkeypatch) -> None:
    # No track credits Owen as producer yet: trac[k]s is where a track gets it.
    metadata = _remix_metadata()
    lines = ["Dree Low (main)", "Owen (guest)", "Owen (producer)", "Jamm (main)", "Dree Low (remixer)"]

    printed = _edit_artists(monkeypatch, metadata, lines)

    assert ("Owen", "producer") in metadata["artists"]
    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert printed == []


def test_a_name_on_no_track_changed_in_more_than_one_way_says_nothing(monkeypatch) -> None:
    # Kai was added in an earlier edit, so no track credits him: there is nothing to leave alone.
    metadata = _remix_metadata()
    metadata["artists"] = [*metadata["artists"], ("Kai", "main"), ("Kai", "remixer")]

    printed = _edit_artists(
        monkeypatch,
        metadata,
        ["Dree Low (main)", "Owen (guest)", "Jamm (main)", "Dree Low (remixer)", "Kai (remixer)", "Kai (guest)"],
    )

    assert ("Kai", "guest") in metadata["artists"]
    assert printed == []


def test_a_names_lines_only_moved_change_nothing_and_say_nothing(monkeypatch) -> None:
    metadata = _remix_metadata()

    printed = _edit_artists(
        monkeypatch, metadata, ["Dree Low (remixer)", "Dree Low (main)", "Owen (guest)", "Jamm (main)"]
    )

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "remixer")]
    assert printed == []


def test_a_role_changed_while_a_line_is_added_leaves_its_credits_and_says_so(monkeypatch) -> None:
    # Rule 5: Dree Low loses main and gains guest and producer; which credit became which can't be told.
    metadata = _remix_metadata()
    lines = ["Dree Low (guest)", "Dree Low (producer)", "Owen (guest)", "Jamm (main)", "Dree Low (remixer)"]

    printed = _edit_artists(monkeypatch, metadata, lines)

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert printed == [
        "Dree Low: more than one of its roles changed, so its credits on Disc 1 Tracks 1, 2 are left as they were: "
        "set them in trac[k]s."
    ]


def test_only_what_this_edit_left_alone_is_said(monkeypatch) -> None:
    # Owen was deleted by an earlier edit, or by a list set elsewhere (the AI review's suggestion sets only the
    # release's list): this save leaves him on track 1 and says nothing more about him.
    metadata = _remix_metadata()
    metadata["artists"] = [("Dree Low", "main"), ("Jamm", "main"), ("Dree Low", "remixer")]

    printed = _edit_artists(monkeypatch, metadata, ["Dree Low (main)", "Jamm (main)", "Dree Low (guest)"])

    assert _track(metadata, 1) == [("Dree Low", "main"), ("Owen", "guest")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Low", "guest")]
    assert printed == []


def test_a_credit_moved_to_a_role_the_track_already_holds_is_kept_once(monkeypatch) -> None:
    # A self-remix: A is main and remixer on track 1, and the remixer line now says main.
    metadata = _metadata([("A", "main"), ("A", "remixer")], [("B", "main")])

    _edit_artists(monkeypatch, metadata, ["A (main)", "A (main)", "B (main)"])

    assert _track(metadata, 1) == [("A", "main")]


def test_a_credit_moved_to_main_beside_a_guest_of_the_same_name_keeps_main_in_the_guests_place(monkeypatch) -> None:
    # "Jamm feat. Owen (Owen Remix)": Owen's remixer line is now main. Main and guest are one credit per name (the
    # ARTIST credit, as filter_artists keeps them), and a guest kept here would win at the next trac[k]s save.
    metadata = _metadata([("Jamm", "main"), ("Owen", "guest"), ("Owen", "remixer")])

    _edit_artists(monkeypatch, metadata, ["Jamm (main)", "Owen (guest)", "Owen (main)"])

    assert _track(metadata, 1) == [("Jamm", "main"), ("Owen", "main")]


def test_a_name_typed_in_another_case_or_without_accents_matches_and_the_tracks_keep_their_spelling(
    monkeypatch,
) -> None:
    metadata = _metadata([("Dree Löw", "main")], [("Jamm", "main"), ("Dree Löw", "remixer")])

    _edit_artists(monkeypatch, metadata, ["DREE LOW (main)", "Jamm (main)", "dree low (guest)"])

    assert _track(metadata, 1) == [("Dree Löw", "main")]
    assert _track(metadata, 2) == [("Jamm", "main"), ("Dree Löw", "guest")]


def test_a_name_is_read_without_the_spaces_around_it(monkeypatch) -> None:
    # "Dree Low " would match no track credit, and read as Dree Low gone from the list.
    metadata = _remix_metadata()

    printed = _edit_artists(
        monkeypatch, metadata, ["Dree Low  (main)", "Owen (guest)", "Jamm (main)", "Dree Low (remixer)"]
    )

    assert metadata["artists"][0] == ("Dree Low", "main")
    assert printed == []


def test_a_line_given_twice_is_kept_once(monkeypatch) -> None:
    metadata = _remix_metadata()

    _edit_artists(
        monkeypatch, metadata, ["Dree Low (main)", "Owen (guest)", "Jamm (main)", "Jamm (main)", "Dree Low (remixer)"]
    )

    assert metadata["artists"] == [("Dree Low", "main"), ("Owen", "guest"), ("Jamm", "main"), ("Dree Low", "remixer")]


def test_the_tracks_a_message_names_are_grouped_by_disc() -> None:
    assert review_module._track_list([("1", "2"), ("1", "4"), ("2", "1")]) == "Disc 1 Tracks 2, 4; Disc 2 Track 1"


def test_a_broken_line_is_refused_and_changes_nothing(monkeypatch) -> None:
    metadata = _remix_metadata()
    before = (list(metadata["artists"]), _track(metadata, 2))
    texts = iter(["Dree Low main", ""])
    confirms: list[str] = []

    async def fake_confirm(message, **_kwargs):
        confirms.append(str(message))
        return True

    monkeypatch.setattr(review_module.interaction, "confirm", fake_confirm)
    _run(monkeypatch, review_module._edit_artists, metadata, lambda _text: next(texts))

    assert (metadata["artists"], _track(metadata, 2)) == before
    assert "The artists text is invalid" in confirms[0]


# #868: [a]rtists sets the order.


def _gladiator() -> dict:
    # The store's per-track order, alphabetical, where the album's is Per, Jan, Gary.
    trio = [(GARY, "main"), (JAN, "main"), (PER, "main")]
    return _metadata(trio, trio, [(JAN, "main"), (GARY, "main"), ("Owen", "guest")])


def test_reordering_the_artists_reorders_every_track(monkeypatch) -> None:
    metadata = _gladiator()

    _edit_artists(monkeypatch, metadata, [f"{PER} (main)", f"{JAN} (main)", f"{GARY} (main)", "Owen (guest)"])

    assert metadata["artists"] == [(PER, "main"), (JAN, "main"), (GARY, "main"), ("Owen", "guest")]
    assert _track(metadata, 1) == [(PER, "main"), (JAN, "main"), (GARY, "main")]
    assert _track(metadata, 2) == [(PER, "main"), (JAN, "main"), (GARY, "main")]
    assert _track(metadata, 3) == [(JAN, "main"), (GARY, "main"), ("Owen", "guest")]


def test_a_credit_the_list_does_not_hold_goes_after_the_reordered_ones(monkeypatch) -> None:
    metadata = _gladiator()

    # Owen deleted (the tracks keep him), and the rest reordered.
    _edit_artists(monkeypatch, metadata, [f"{PER} (main)", f"{GARY} (main)", f"{JAN} (main)"])

    assert _track(metadata, 3) == [(GARY, "main"), (JAN, "main"), ("Owen", "guest")]


def test_a_role_change_without_a_new_order_leaves_each_tracks_order(monkeypatch) -> None:
    # A track may rightly be led by another of its artists.
    metadata = _metadata([(GARY, "main"), (JAN, "main")], [(JAN, "main"), (GARY, "main")])

    _edit_artists(monkeypatch, metadata, [f"{GARY} (main)", f"{JAN} (guest)"])

    assert _track(metadata, 1) == [(GARY, "main"), (JAN, "guest")]
    assert _track(metadata, 2) == [(JAN, "guest"), (GARY, "main")]


def test_a_line_changed_and_moved_still_takes_its_change(monkeypatch) -> None:
    # B is a guest on track 1 and main on track 2; its guest line now says main and is moved to the top.
    metadata = _metadata([("A", "main"), ("B", "guest")], [("B", "main")])

    printed = _edit_artists(monkeypatch, metadata, ["B (main)", "A (main)", "B (main)"])

    assert metadata["artists"] == [("B", "main"), ("A", "main")]
    assert _track(metadata, 1) == [("B", "main"), ("A", "main")]
    assert printed == []


# #868: trac[k]s keeps the release's order.


def _gladiator_in_order() -> dict:
    # The store's per-track order, alphabetical; the release's, set in [a]rtists, is Per, Jan, Gary.
    trio = [(GARY, "main"), (JAN, "main"), (PER, "main")]
    metadata = _metadata(trio, trio, [(JAN, "main"), (GARY, "main"), ("Owen", "guest")])
    metadata["artists"] = [(PER, "main"), (JAN, "main"), (GARY, "main"), ("Owen", "guest")]
    return metadata


def _save_tracks(monkeypatch, metadata: dict, change=lambda text: text) -> None:
    _run(monkeypatch, review_module._edit_tracks, metadata, change)


def test_an_unchanged_tracks_save_keeps_the_releases_order(monkeypatch) -> None:
    metadata = _gladiator_in_order()

    _save_tracks(monkeypatch, metadata)

    assert metadata["artists"] == [(PER, "main"), (JAN, "main"), (GARY, "main"), ("Owen", "guest")]


def test_a_credit_added_in_tracks_goes_after_the_releases_own(monkeypatch) -> None:
    metadata = _gladiator_in_order()

    _save_tracks(
        monkeypatch, metadata, lambda text: text.replace(f"> {GARY} (main)", f"> Ane Brun (main)\n> {GARY} (main)", 1)
    )

    assert metadata["artists"] == [
        (PER, "main"),
        (JAN, "main"),
        (GARY, "main"),
        ("Owen", "guest"),
        ("Ane Brun", "main"),
    ]


def test_a_credit_removed_in_tracks_leaves_the_release(monkeypatch) -> None:
    metadata = _gladiator_in_order()

    _save_tracks(monkeypatch, metadata, lambda text: text.replace("> Owen (guest)\n", "").replace("> Owen (guest)", ""))

    assert metadata["artists"] == [(PER, "main"), (JAN, "main"), (GARY, "main")]


def test_a_tracks_save_keeps_the_place_of_a_name_it_respells(monkeypatch) -> None:
    metadata = _gladiator_in_order()

    # Every track now spells Per's name in capitals: the release takes that spelling, in Per's place.
    _save_tracks(monkeypatch, metadata, lambda text: text.replace(f"> {PER} (main)", f"> {PER.upper()} (main)"))

    assert metadata["artists"] == [(PER.upper(), "main"), (JAN, "main"), (GARY, "main"), ("Owen", "guest")]


def test_reordering_a_later_track_in_tracks_leaves_the_releases_order(monkeypatch) -> None:
    metadata = _gladiator_in_order()

    # Track 2's block: Gary, Jan, Per becomes Per, Gary, Jan.
    def reorder_track_2(text: str) -> str:
        head, _, rest = text.partition("Disc 1 Track 2")
        block, sep, tail = rest.partition("-----")
        block = block.replace(
            f"> {GARY} (main)\n> {JAN} (main)\n> {PER} (main)", f"> {PER} (main)\n> {GARY} (main)\n> {JAN} (main)"
        )
        return head + "Disc 1 Track 2" + block + sep + tail

    _save_tracks(monkeypatch, metadata, reorder_track_2)

    assert _track(metadata, 2) == [(PER, "main"), (GARY, "main"), (JAN, "main")]
    assert metadata["artists"] == [(PER, "main"), (JAN, "main"), (GARY, "main"), ("Owen", "guest")]


def test_a_role_changed_in_tracks_takes_the_place_of_the_credit_it_replaces(monkeypatch) -> None:
    metadata = _gladiator_in_order()
    metadata["artists"] = [("Owen", "guest"), (PER, "main"), (JAN, "main"), (GARY, "main")]

    _save_tracks(monkeypatch, metadata, lambda text: text.replace("> Owen (guest)", "> Owen (main)"))

    assert metadata["artists"] == [("Owen", "main"), (PER, "main"), (JAN, "main"), (GARY, "main")]


def _remix_in_order() -> dict:
    # The tracks: Dree Low (main), Owen (guest); Jamm (main), Dree Low (remixer). The release leads with Jamm.
    metadata = _metadata([("Dree Low", "main"), ("Owen", "guest")], [("Jamm", "main"), ("Dree Low", "remixer")])
    metadata["artists"] = [("Jamm", "main"), ("Dree Low", "main"), ("Owen", "guest"), ("Dree Low", "remixer")]
    return metadata


def test_a_role_changed_in_tracks_moves_no_other_credit(monkeypatch) -> None:
    # Dree Low's remixer credit is now producer: it stays last, and Owen keeps his place after Dree Low (main).
    metadata = _remix_in_order()

    _save_tracks(monkeypatch, metadata, lambda text: text.replace("> Dree Low (remixer)", "> Dree Low (producer)"))

    assert metadata["artists"] == [("Jamm", "main"), ("Dree Low", "main"), ("Owen", "guest"), ("Dree Low", "producer")]


def test_a_role_added_in_tracks_goes_last(monkeypatch) -> None:
    metadata = _remix_in_order()

    _save_tracks(
        monkeypatch, metadata, lambda text: text.replace("> Owen (guest)", "> Owen (guest)\n> Owen (producer)")
    )

    assert metadata["artists"] == [
        ("Jamm", "main"),
        ("Dree Low", "main"),
        ("Owen", "guest"),
        ("Dree Low", "remixer"),
        ("Owen", "producer"),
    ]
