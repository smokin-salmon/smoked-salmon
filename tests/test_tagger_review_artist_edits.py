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
