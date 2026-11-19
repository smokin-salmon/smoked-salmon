"""The group description's tracklist leaves out a track's artist when it credits only the album's artists (#895).

A guest of the album's artists goes after the title, and one remixer of every track is said once (#971). A track that
credits anyone else, or a remix, keeps its artist, as do every track of a various-artists release, every track when
no album artists are given (a cross-upload), and every track of a compilation, anthology or DJ mix unless all of them
are listed without it (#971). salmon up reads the credits from the files' tags; salmon descgen (and the web
interface's Tools page) from the store's metadata.
"""

import re
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from helpers.tagger_metadata_corrections import _track
from helpers.tagger_multi_value_tags import _write_m4a
from mutagen.mp3 import MP3

import salmon.commands
from salmon import cfg
from salmon.commands import build_description
from salmon.tagger.retagger import create_artist_str, create_track_changes, retag_files
from salmon.tagger.tags import gather_tags
from salmon.uploader.description import generate_description


def _tracks(*tags: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Track data with these tags, one track each, numbered from 1 and 3:20 long, titled "Song N" unless given."""
    return {
        f"{number:02d}.flac": {
            "duration": 200,
            "t": SimpleNamespace(
                **{
                    "discnumber": "1/1",
                    "tracknumber": str(number),
                    "title": f"Song {number}",
                    "artists": None,
                    "remixer": None,
                    **tag,
                }
            ),
        }
        for number, tag in enumerate(tags, 1)
    }


def _album(*artists: tuple[str, str], rls_type: str = "Album") -> dict[str, Any]:
    return {"comment": None, "urls": None, "rls_type": rls_type, "artists": list(artists)}


def _lines(description: str) -> list[str]:
    return [line for line in description.splitlines() if line.startswith("[b]0")]


def test_a_one_artist_albums_tracklist_names_no_artist() -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, {"artist": ["Sullivan Murphy"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert description == (
        "[b][size=4]Tracklist[/size][/b]\n"
        "[b]01.[/b] Song 1 [i](3:20)[/i]\n"
        "[b]02.[/b] Song 2 [i](3:20)[/i]\n"
        "\n[b]Total length: [/b]6:40\n"
    )


@pytest.mark.parametrize(
    ("album", "tags", "line"),
    [
        # Each track keeps its artist when it credits anyone besides the album's and their guests.
        (
            # An AAC file has no ARTISTS: the title alone credits the guest.
            [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (feat. Dierdre Simpson)"},
            "Sullivan Murphy - Song 1 (feat. Dierdre Simpson)",
        ),
        (
            # Nor REMIXER: the title alone credits the remix.
            [("Sullivan Murphy", "main")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remi Xer Remix)"},
            "Sullivan Murphy - Song 1 (Remi Xer Remix)",
        ),
        (
            # A mix or an edit in the title names no remixer (#1061): only a remix, or REMIXER, does.
            [("Sullivan Murphy", "main")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remi Xer Mix)"},
            "Song 1 (Remi Xer Mix)",
        ),
        (
            [("Sullivan Murphy", "main")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remi Xer Edit)"},
            "Song 1 (Remi Xer Edit)",
        ),
        (
            [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 - Remi Xer Remix"},
            "Sullivan Murphy - Song 1 - Remi Xer Remix",
        ),
        (
            [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remixed by Remi Xer)"},
            "Sullivan Murphy - Song 1 (Remixed by Remi Xer)",
        ),
        (
            [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (with Dierdre Simpson)"},
            "Sullivan Murphy - Song 1 (with Dierdre Simpson)",
        ),
        ([("Sullivan Murphy", "main")], {"artist": ["Other Artist"]}, "Other Artist - Song 1"),
        ([("Alice", "main"), ("Bob", "main")], {"artist": ["Alice"]}, "Alice - Song 1"),
        ([("Alice", "main")], {"artist": ["Alice & Bob"]}, "Alice & Bob - Song 1"),
        (
            [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
            {"artist": ["Sullivan Murphy"], "remixer": ["Remi Xer"], "title": "Song 1 (Remi Xer Remix)"},
            "Sullivan Murphy - Song 1 (Remi Xer Remix)",
        ),
        (
            # REMIXER alone, the title naming no remix.
            [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
            {"artist": ["Sullivan Murphy"], "remixer": ["Remi Xer"], "title": "Song 1 (Club Mix)"},
            "Sullivan Murphy - Song 1 (Club Mix)",
        ),
        ([("Sullivan Murphy", "main")], {"artist": ["Sullivan Murphy (Remix by Remi Xer)"]}, None),
        ([("Sullivan Murphy", "main")], {"artist": ["sullivan murphy"]}, "sullivan murphy - Song 1"),
        (
            [("Orchestra", "main"), ("A Conductor", "conductor")],
            {"artist": ["Orchestra, Another Conductor"]},
            "Orchestra, Another Conductor - Song 1",
        ),
        (
            # ARTIST already credits the album: a band's members in ARTISTS credit no one besides it (#971).
            [("Rush", "main")],
            {"artist": ["Rush"], "artists": ["Geddy Lee", "Alex Lifeson", "Neil Peart"]},
            "Song 1",
        ),
        # A track that credits exactly the album's artists, however the tags spell the list, names none.
        ([("Alice", "main"), ("Bob", "main")], {"artist": ["Alice & Bob"]}, "Song 1"),
        ([("Alice", "main"), ("Bob", "main")], {"artist": ["Alice", "Bob"]}, "Song 1"),
        ([("Alice", "main"), ("Bob", "main")], {"artist": ["Bob & Alice"]}, "Song 1"),
        ([("Simon & Garfunkel", "main")], {"artist": ["Simon & Garfunkel"]}, "Song 1"),
        ([("Sullivan Murphy", "main")], {"artist": ["Sullivan Murphy"], "artists": ["Sullivan Murphy"]}, "Song 1"),
        # A version that names no remixer, and "with" outside a bracket.
        (
            [("Sullivan Murphy", "main")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Radio Edit)"},
            "Song 1 (Radio Edit)",
        ),
        (
            [("Sullivan Murphy", "main")],
            {"artist": ["Sullivan Murphy"], "title": "Dancing with Myself"},
            "Dancing with Myself",
        ),
        # "feat" and "remix" only as whole words.
        (
            [("Sullivan Murphy", "main")],
            {"artist": ["Sullivan Murphy"], "title": "Defeat in the Loft"},
            "Defeat in the Loft",
        ),
        (
            # A guest on the album but not on this track.
            [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")],
            {"artist": ["Sullivan Murphy"]},
            "Song 1",
        ),
        (
            # ARTIST leads with the conductors after the main artists.
            [("Orchestra", "main"), ("A Conductor", "conductor"), ("A Composer", "composer")],
            {"artist": ["Orchestra, A Conductor"], "artists": ["Orchestra"]},
            "Song 1",
        ),
    ],
    ids=[
        "a guest in the title alone",
        "a remix in the title alone",
        "a mix in the title",
        "an edit in the title",
        "a remix after a dash in the title",
        "remixed by in the title",
        "a guest with in the title",
        "another main artist",
        "one of two main artists",
        "one more main artist",
        "a remixer",
        "a remixer named only in REMIXER",
        "a remix credit in ARTIST",
        "the name in another case",
        "another conductor",
        "a band's members in ARTISTS",
        "two main artists",
        "two main artists as two values",
        "two main artists in another order",
        "a duo's name",
        "ARTISTS the same",
        "a title with feat inside a word",
        "a radio edit",
        "with outside a bracket",
        "an album guest not on the track",
        "a conductor",
    ],
)
def test_a_track_names_its_artist_only_when_it_credits_anyone_besides_the_albums(
    album: list[tuple[str, str]], tags: dict[str, Any], line: str | None
) -> None:
    description = generate_description(_tracks(tags), _album(*album))

    expected = line or f"{tags['artist'][0]} - Song 1"
    assert _lines(description) == [f"[b]01.[/b] {expected} [i](3:20)[/i]"]


@pytest.mark.parametrize(
    "tags",
    [
        {"artist": ["Sullivan Murphy (feat. Dierdre Simpson)"]},
        # guests_in_track_title: the guest is in the title, and in ARTISTS after the main artists.
        {
            "artist": ["Sullivan Murphy"],
            "artists": ["Sullivan Murphy", "Dierdre Simpson"],
            "title": "Song 1 (feat. Dierdre Simpson)",
        },
    ],
    ids=["a guest in ARTIST", "a guest in the title"],
)
def test_a_guest_of_the_album_artist_goes_after_the_title(tags: dict[str, Any]) -> None:
    # #971 item 6: only the album's artist goes, as on every other line; the guest stays on the line.
    description = generate_description(_tracks(tags), _album(("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")))

    assert _lines(description) == ["[b]01.[/b] Song 1 (feat. Dierdre Simpson) [i](3:20)[/i]"]


@pytest.mark.parametrize(
    "title",
    [
        "Song (with Strings)",
        "Song [with B]",
        "Song (vs. B)",
        "Song (vs B)",
        "Song ( WITH B)",
        "Song (with B",
        "Heroic Feat",
        "6 Ft Under",
        "Song (featuring B)",
        "Song - B Remix",
        "Song (Remixed by B)",
        "The Remixes",
        "Song - b remix",
    ],
)
def test_as_before_a_title_that_may_credit_someone_keeps_the_artist(title: str) -> None:
    # The tags may name no guest, as an AAC file's can't, so any with or vs. bracket counts here.
    description = generate_description(_tracks({"artist": ["Alice"], "title": title}), _album(("Alice", "main")))

    assert _lines(description) == [f"[b]01.[/b] Alice - {title} [i](3:20)[/i]"]


@pytest.mark.parametrize("title", ["Song with B", "Song (without B)", "Song (Live with B)", "Defeat", "Remixing"])
def test_as_before_a_title_crediting_no_one_leaves_the_artist_out(title: str) -> None:
    description = generate_description(_tracks({"artist": ["Alice"], "title": title}), _album(("Alice", "main")))

    assert _lines(description) == [f"[b]01.[/b] {title} [i](3:20)[/i]"]


def test_as_before_a_track_with_no_title_does_not_fail() -> None:
    generate_description(_tracks({"artist": ["Alice"], "title": None}), _album(("Alice", "main")))


COMPILATIONS = ["Compilation", "Anthology", "DJ Mix"]


@pytest.mark.parametrize("rls_type", COMPILATIONS)
def test_a_compilation_all_by_the_album_artist_names_no_artist(rls_type: str) -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, {"artist": ["Sullivan Murphy"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main"), rls_type=rls_type))

    assert _lines(description) == ["[b]01.[/b] Song 1 [i](3:20)[/i]", "[b]02.[/b] Song 2 [i](3:20)[/i]"]


@pytest.mark.parametrize("rls_type", COMPILATIONS)
def test_a_mix_name_in_a_title_doesnt_keep_every_artist_on_a_compilation(rls_type: str) -> None:
    # A mix's name credits no remixer (#1061), so it doesn't put the artist back on every line.
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, {"artist": ["Sullivan Murphy"], "title": "Song 2 (Disco Mix)"})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main"), rls_type=rls_type))

    assert _lines(description) == ["[b]01.[/b] Song 1 [i](3:20)[/i]", "[b]02.[/b] Song 2 (Disco Mix) [i](3:20)[/i]"]


def test_a_duos_compilation_names_no_artist() -> None:
    tracks = _tracks({"artist": ["Alice & Bob"]})

    description = generate_description(tracks, _album(("Alice", "main"), ("Bob", "main"), rls_type="Compilation"))

    assert _lines(description) == ["[b]01.[/b] Song 1 [i](3:20)[/i]"]


@pytest.mark.parametrize("rls_type", COMPILATIONS)
@pytest.mark.parametrize(
    ("other", "line"),
    [
        ({"artist": ["Other Artist"]}, "Other Artist - Song 2"),
        (
            {"artist": ["Sullivan Murphy"], "title": "Song 2 (Remi Xer Remix)"},
            "Sullivan Murphy - Song 2 (Remi Xer Remix)",
        ),
    ],
    ids=["another artist", "a remix in the title"],
)
def test_as_before_one_track_with_anyone_else_keeps_every_artist_on_a_compilation(
    rls_type: str, other: dict[str, Any], line: str
) -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, other)

    description = generate_description(tracks, _album(("Sullivan Murphy", "main"), rls_type=rls_type))

    assert _lines(description) == [
        "[b]01.[/b] Sullivan Murphy - Song 1 [i](3:20)[/i]",
        f"[b]02.[/b] {line} [i](3:20)[/i]",
    ]


@pytest.mark.parametrize("rls_type", COMPILATIONS)
def test_a_guest_of_the_album_artist_on_a_compilation_goes_after_the_title(rls_type: str) -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, {"artist": ["Sullivan Murphy (feat. Dierdre Simpson)"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main"), rls_type=rls_type))

    assert _lines(description) == [
        "[b]01.[/b] Song 1 [i](3:20)[/i]",
        "[b]02.[/b] Song 2 (feat. Dierdre Simpson) [i](3:20)[/i]",
    ]


def test_a_guest_in_artists_goes_after_the_title_when_artist_is_the_albums_credit() -> None:
    description = generate_description(
        _tracks({"artist": ["Alice"], "artists": ["Alice (feat. Bob)"]}), _album(("Alice", "main"))
    )

    assert _lines(description) == ["[b]01.[/b] Song 1 (feat. Bob) [i](3:20)[/i]"]


def test_as_before_an_album_keeps_the_artist_only_on_the_tracks_with_anyone_else() -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, {"artist": ["Other Artist"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == [
        "[b]01.[/b] Song 1 [i](3:20)[/i]",
        "[b]02.[/b] Other Artist - Song 2 [i](3:20)[/i]",
    ]


def test_as_before_a_various_artists_compilation_keeps_every_artist() -> None:
    tracks = _tracks({"artist": ["Various Artists"]})

    description = generate_description(tracks, _album(("Various Artists", "main"), rls_type="Compilation"))

    assert _lines(description) == ["[b]01.[/b] Various Artists - Song 1 [i](3:20)[/i]"]


def test_a_name_only_in_artists_doesnt_keep_the_artist_when_artist_credits_the_album() -> None:
    tags = {"artist": ["Sullivan Murphy"], "artists": ["Sullivan Murphy", "Dierdre Simpson"]}

    description = generate_description(_tracks(tags), _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01.[/b] Song 1 [i](3:20)[/i]"]


@pytest.mark.parametrize(
    ("album", "tags", "line"),
    [
        # ARTIST names only one of them, and ARTISTS completes the album's credit.
        (
            [("Alice", "main"), ("Bob", "main")],
            {"artist": ["Alice"], "artists": ["Alice", "Bob"]},
            "Song 1",
        ),
        (
            [("Alice", "main"), ("Bob", "main"), ("Carol", "main")],
            {"artist": ["Alice"], "artists": ["Alice", "Bob"]},
            "Alice - Song 1",
        ),
        ([("Alice", "main")], {"artist": ["Other"], "artists": ["Alice"]}, "Other - Song 1"),
        ([("Alice", "main")], {"artist": ["Alice"], "artists": ["Alice (Remix by Bob)"]}, "Alice - Song 1"),
    ],
    ids=[
        "ARTISTS completes the credit",
        "ARTISTS still short of it",
        "ARTIST someone else",
        "a remix in ARTISTS",
    ],
)
def test_as_before_artists_counts_when_artist_alone_is_not_the_albums_credit(
    album: list[tuple[str, str]], tags: dict[str, Any], line: str
) -> None:
    description = generate_description(_tracks(tags), _album(*album))

    assert _lines(description) == [f"[b]01.[/b] {line} [i](3:20)[/i]"]


def test_every_track_of_an_album_with_various_main_artists_keeps_its_artist() -> None:
    names = [f"Artist {n}" for n in range(cfg.upload.formatting.various_artist_threshold)]
    tracks = _tracks({"artist": names})

    description = generate_description(tracks, _album(*((name, "main") for name in names)))

    assert _lines(description) == [f"[b]01.[/b] {', '.join(names)} - Song 1 [i](3:20)[/i]"]


@pytest.mark.parametrize("credit", ["Various Artists", "Various"])
def test_every_track_of_an_album_credited_to_various_artists_keeps_its_artist(credit: str) -> None:
    tracks = _tracks({"artist": [credit]})

    description = generate_description(tracks, _album((credit, "main")))

    assert _lines(description) == [f"[b]01.[/b] {credit} - Song 1 [i](3:20)[/i]"]


def test_every_track_keeps_its_artist_when_no_album_artists_are_given() -> None:
    # A cross-upload's description: the tracklist from the tags alone.
    tracks = _tracks({"artist": ["Sullivan Murphy"]})

    description = generate_description(tracks, {"comment": None, "urls": []})

    assert _lines(description) == ["[b]01.[/b] Sullivan Murphy - Song 1 [i](3:20)[/i]"]


def test_artist_tags_wrap_only_the_artists_still_named(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.description, "artist_tags_in_tracklist", True)
    tracks = _tracks({"artist": ["Sullivan Murphy"]}, {"artist": ["Other Artist"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == [
        "[b]01.[/b] Song 1 [i](3:20)[/i]",
        "[b]02.[/b] [artist]Other Artist[/artist] - Song 2 [i](3:20)[/i]",
    ]


def test_a_multi_disc_tracklist_keeps_its_disc_numbers() -> None:
    tracks = _tracks(
        {"artist": ["Sullivan Murphy"], "discnumber": "1"}, {"artist": ["Sullivan Murphy"], "discnumber": "2"}
    )

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01-01.[/b] Song 1 [i](3:20)[/i]", "[b]02-02.[/b] Song 2 [i](3:20)[/i]"]


def _numbers(description: str) -> list[str]:
    """The "[b]NN.[/b]" numbers that start the tracklist's lines."""
    return re.findall(r"^\[b\](.+?)\.\[/b\] ", description, flags=re.MULTILINE)


def _numbers_listed(numbers: list[tuple[str | None, ...]]) -> list[str]:
    """The numbers the tracklist gives tracks whose files hold these (disc, track[, disc total]) tags as written."""
    tracks = _tracks(
        *(
            {"artist": ["Sullivan Murphy"], "discnumber": d, "tracknumber": t, "disctotal": rest[0] if rest else None}
            for d, t, *rest in numbers
        )
    )
    return _numbers(generate_description(tracks, _album(("Sullivan Murphy", "main"))))


@pytest.mark.parametrize(
    ("numbers", "listed"),
    [
        pytest.param([("2/2", "1"), ("1/2", "1")], ["02-01", "01-01"], id="disc with its total"),
        pytest.param([("02/02", "1"), ("01/02", "1")], ["02-01", "01-01"], id="zero-padded disc with its total"),
        pytest.param([("1/2", "1"), ("1/2", "2")], ["01-01", "01-02"], id="first of two discs alone"),
        pytest.param([("1", "1", "2"), ("1", "2", "2")], ["01-01", "01-02"], id="first of two discs alone, DISCTOTAL"),
        pytest.param([("1", "1"), (" 2 ", "1")], ["01-01", "02-01"], id="disc with spaces"),
        pytest.param([("2", "1"), (None, "1")], ["02-01", "01-01"], id="one disc number missing"),
        pytest.param([("A", "1"), ("2", "1")], ["A-01", "02-01"], id="a side among discs"),
        pytest.param([("²", "1"), ("2", "1")], ["²-01", "02-01"], id="superscript disc"),
        pytest.param([("1", "3/10")], ["03"], id="track with its total"),
        pytest.param([("1/2", "3/10"), ("2/2", "1/10")], ["01-03", "02-01"], id="both with totals"),
        pytest.param([("1", "1²")], ["1²"], id="superscript track"),
        pytest.param([("CD1", "1"), ("CD2", "1")], ["CD1-01", "CD2-01"], id="discs named, not numbered"),
        pytest.param([("A", "1"), ("B", "1")], ["A-01", "B-01"], id="sides alone"),
        pytest.param([("CD1", "1"), ("CD1", "2")], ["01", "02"], id="one disc, named"),
        pytest.param([("1", "1"), ("CD2", "1")], ["01-01", "CD2-01"], id="one disc numbered, one named"),
    ],
)
def test_the_tracklist_numbers_each_track_from_its_tags_as_written(numbers, listed) -> None:
    # A FLAC keeps DISCNUMBER and TRACKNUMBER as written, a total too; each of these raised or was listed as
    # written ("1/2-01.") (#910).
    assert _numbers_listed(numbers) == listed


@pytest.mark.parametrize(
    ("numbers", "listed"),
    [
        pytest.param([("1/1", "1"), ("1/1", "2")], ["01", "02"], id="one of one disc"),
        pytest.param([("1", "1"), ("1", "2")], ["01", "02"], id="one disc"),
        pytest.param([(None, "1"), (None, "2")], ["01", "02"], id="no disc number"),
        pytest.param([("0", "1"), ("1", "1"), ("2", "1")], ["00-01", "01-01", "02-01"], id="disc 0 beside disc 1"),
        pytest.param([("0", "1"), ("1", "1")], ["01", "01"], id="discs 0 and 1 alone"),
        pytest.param([("2", "A1"), ("2", "B1")], ["02-A1", "02-B1"], id="sides as track numbers"),
    ],
)
def test_the_tracklist_still_numbers_tracks_as_before(numbers, listed) -> None:
    assert _numbers_listed(numbers) == listed


def test_the_tracklist_numbers_m4a_tracks(tmp_path) -> None:
    # An M4A holds its numbers as (number, total) pairs (#908).
    _write_m4a(
        tmp_path / "01.m4a", trkn=[(1, 1)], disk=[(1, 2)], **{"\xa9nam": ["One"], "\xa9ART": ["Sullivan Murphy"]}
    )
    _write_m4a(
        tmp_path / "02.m4a", trkn=[(1, 1)], disk=[(2, 2)], **{"\xa9nam": ["Two"], "\xa9ART": ["Sullivan Murphy"]}
    )
    tracks = {name: {"duration": 200, "t": tag} for name, tag in gather_tags(str(tmp_path)).items()}

    assert _numbers(generate_description(tracks, _album(("Sullivan Murphy", "main")))) == ["01-01", "02-01"]


def test_the_tracklist_numbers_the_first_of_two_m4a_discs_on_its_own(tmp_path) -> None:
    # An M4A keeps its disc total apart, as `disctotal` (#908).
    _write_m4a(
        tmp_path / "01.m4a", trkn=[(1, 1)], disk=[(1, 2)], **{"\xa9nam": ["One"], "\xa9ART": ["Sullivan Murphy"]}
    )
    tracks = {name: {"duration": 200, "t": tag} for name, tag in gather_tags(str(tmp_path)).items()}

    assert _numbers(generate_description(tracks, _album(("Sullivan Murphy", "main")))) == ["01-01"]


def _descgen(monkeypatch, metadata: dict[str, Any]) -> list[str]:
    async def fake_run_metadata(url, return_source_name=False):
        return metadata, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: metadata)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda metadata: metadata)
    description = anyio.run(build_description, ["https://www.qobuz.com/album/x"])
    return _lines(description)


def _store_album(*tracks: list[tuple[str, str]], rls_type: str = "Album", credited: bool = True) -> dict[str, Any]:
    """A store's release, its remixers credited as a parser credits the remixers the store names, unless not
    `credited` (read from a title, as the merge and Tidal's parser read them)."""
    artists = list(dict.fromkeys(credit for track in tracks for credit in track))
    return {
        "artists": artists,
        "rls_type": rls_type,
        "tracks": {
            "1": {
                str(n): {
                    "artists": track,
                    "title": f"Song {n}",
                    "credited_remixers": [name for name, role in track if role == "remixer" and credited],
                }
                for n, track in enumerate(tracks, 1)
            }
        },
        "comment": None,
        "urls": [],
    }


def test_descgen_names_no_artist_on_a_one_artist_album(monkeypatch) -> None:
    album = _store_album([("Sullivan Murphy", "main")], [("Sullivan Murphy", "main"), ("A Composer", "composer")])

    lines = _descgen(monkeypatch, album)

    assert lines == ["[b]01.[/b] Song 1", "[b]02.[/b] Song 2"]


def test_descgen_names_the_artist_of_a_remix_and_lists_a_guest_after_the_title(monkeypatch) -> None:
    guest = [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")]
    remix = [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")]
    album = _store_album([("Sullivan Murphy", "main")], guest, remix)

    lines = _descgen(monkeypatch, album)

    assert lines == [
        "[b]01.[/b] Song 1",
        "[b]02.[/b] Song 2 (feat. Dierdre Simpson)",
        f"[b]03.[/b] {create_artist_str(remix)} - Song 3",
    ]


def test_descgen_names_no_artist_on_a_compilation_all_by_the_album_artist(monkeypatch) -> None:
    album = _store_album([("Sullivan Murphy", "main")], [("Sullivan Murphy", "main")], rls_type="Compilation")

    lines = _descgen(monkeypatch, album)

    assert lines == ["[b]01.[/b] Song 1", "[b]02.[/b] Song 2"]


def test_as_before_descgen_keeps_every_artist_on_a_compilation_with_anyone_else(monkeypatch) -> None:
    other = [("Other Artist", "main")]
    album = _store_album([("Sullivan Murphy", "main")], [("Sullivan Murphy", "main")], other, rls_type="Compilation")

    lines = _descgen(monkeypatch, album)

    assert lines == [
        "[b]01.[/b] Sullivan Murphy - Song 1",
        "[b]02.[/b] Sullivan Murphy - Song 2",
        "[b]03.[/b] Other Artist - Song 3",
    ]


def _write_flac(path: Path) -> None:
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


def _write_mp3(path: Path) -> None:
    path.write_bytes((bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)) * 3)
    mut = MP3(path)
    mut.add_tags()
    mut.save()


@pytest.mark.parametrize("guests_in_track_title", [False, True], ids=["guest in ARTIST", "guest in the title"])
# Not AAC: the tracklist can't read an M4A's track number, an int, yet (a separate bug). The title rule covers its
# guests and remixers, which it has no ARTISTS or REMIXER tag for.
@pytest.mark.parametrize(("extension", "write"), [(".flac", _write_flac), (".mp3", _write_mp3)], ids=["FLAC", "MP3"])
def test_files_salmon_tagged_are_listed_by_the_same_rule_in_each_format(
    monkeypatch, tmp_path, extension, write, guests_in_track_title: bool
) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "guests_in_track_title", guests_in_track_title)
    credits = [
        [("Sullivan Murphy", "main")],
        [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")],
        [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
        [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
        [("Other Artist", "main")],
    ]
    # Track 3's remixer is in its title, as salmon puts one there; track 4's is credited by the store, so salmon writes
    # REMIXER, and the title names no remix.
    titles = ["Song 1", "Song 2", "Song 3 (Remi Xer Remix)", "Song 4", "Song 5"]
    for number in range(1, 6):
        write(tmp_path / f"0{number}{extension}")
    tracks = {
        str(number): {**_track(str(number), artists), "tracktotal": "5", "title": title}
        for number, (artists, title) in enumerate(zip(credits, titles, strict=True), 1)
    }
    tracks["4"]["credited_remixers"] = ["Remi Xer"]
    album = {
        "artists": [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest"), ("Remi Xer", "remixer")],
        "rls_type": "Album",
        "tracks": {"1": tracks},
        "comment": None,
        "urls": None,
    }
    retag_files(str(tmp_path), {}, create_track_changes(gather_tags(str(tmp_path)), album))

    tags = gather_tags(str(tmp_path))
    assert tags[f"04{extension}"].remixer == ["Remi Xer"]
    description = generate_description({name: {"duration": 200, "t": tag} for name, tag in tags.items()}, album)

    # With the setting on or off, a guest of the album's artist goes after the title (#971 item 6).
    assert _lines(description) == [
        "[b]01.[/b] Song 1 [i](3:20)[/i]",
        "[b]02.[/b] Song 2 (feat. Dierdre Simpson) [i](3:20)[/i]",
        "[b]03.[/b] Sullivan Murphy - Song 3 (Remi Xer Remix) [i](3:20)[/i]",
        "[b]04.[/b] Sullivan Murphy - Song 4 [i](3:20)[/i]",
        "[b]05.[/b] Other Artist - Song 5 [i](3:20)[/i]",
    ]


def test_descgen_names_no_artist_on_a_track_by_the_albums_orchestra_and_conductor(monkeypatch) -> None:
    track = [("Orchestra", "main"), ("A Conductor", "conductor"), ("A Composer", "composer")]
    guest = [("Orchestra", "main"), ("A Conductor", "conductor"), ("A Soloist", "guest")]
    album = _store_album(track, guest)

    lines = _descgen(monkeypatch, album)

    assert lines == ["[b]01.[/b] Song 1", "[b]02.[/b] Song 2 (feat. A Soloist)"]


def test_descgen_names_the_artist_of_a_track_whose_title_alone_credits_a_guest(monkeypatch) -> None:
    # A store may give the guest only in the title.
    album = _store_album([("Sullivan Murphy", "main")], [("Sullivan Murphy", "main")])
    album["tracks"]["1"]["2"]["title"] = "Song 2 (feat. Dierdre Simpson)"

    lines = _descgen(monkeypatch, album)

    assert lines == [
        "[b]01.[/b] Song 1",
        "[b]02.[/b] Sullivan Murphy - Song 2 (feat. Dierdre Simpson)",
    ]


def test_descgen_compares_with_the_albums_artists_after_the_tracks_are_cleaned(monkeypatch) -> None:
    # B, credited as main artist and remixer on track 2, is its remixer alone once cleaned, as salmon up does it:
    # the album is by A, so track 1 names no artist.
    remix = [("A", "main"), ("B", "main"), ("B", "remixer")]
    album = _store_album([("A", "main")], remix)
    album["tracks"]["1"]["2"]["title"] = "Song 2 (B Remix)"

    async def fake_run_metadata(url, return_source_name=False):
        return album, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: album)
    description = anyio.run(build_description, ["https://www.qobuz.com/album/x"])

    assert _lines(description) == ["[b]01.[/b] Song 1", "[b]02.[/b] A - Song 2 (B Remix)"]


def test_one_remixer_on_every_track_is_said_once() -> None:
    # #971 item 5: the remix is the album's, so the lines leave the artist out and say it once.
    tracks = _tracks(
        {"artist": ["Sullivan Murphy"], "remixer": ["Remi Xer"], "title": "Song 1 (Remi Xer Remix)"},
        {"artist": ["Sullivan Murphy"], "remixer": ["Remi Xer"], "title": "Song 2 (Remi Xer Remix)"},
    )

    description = generate_description(tracks, _album(("Sullivan Murphy", "main"), ("Remi Xer", "remixer")))

    assert description == (
        "[b][size=4]Tracklist[/size][/b]\n"
        "[b]01.[/b] Song 1 (Remi Xer Remix) [i](3:20)[/i]\n"
        "[b]02.[/b] Song 2 (Remi Xer Remix) [i](3:20)[/i]\n"
        "\nAll tracks remixed by Remi Xer.\n"
        "\n[b]Total length: [/b]6:40\n"
    )


def test_one_remixer_named_only_in_every_title_is_said_once() -> None:
    # An AAC file has no REMIXER: the title alone credits the remix.
    tracks = _tracks(
        {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remi Xer Remix)"},
        {"artist": ["Sullivan Murphy"], "title": "Song 2 (Remi Xer Remix)"},
    )

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert description == (
        "[b][size=4]Tracklist[/size][/b]\n"
        "[b]01.[/b] Song 1 (Remi Xer Remix) [i](3:20)[/i]\n"
        "[b]02.[/b] Song 2 (Remi Xer Remix) [i](3:20)[/i]\n"
        "\nAll tracks remixed by Remi Xer.\n"
        "\n[b]Total length: [/b]6:40\n"
    )


@pytest.mark.parametrize(
    "second",
    [
        {"artist": ["Sullivan Murphy"], "remixer": ["Other Mixer"], "title": "Song 2 (Other Mixer Remix)"},
        {"artist": ["Sullivan Murphy"], "title": "Song 2"},
    ],
    ids=["another remixer", "no remix"],
)
def test_as_before_a_remix_on_some_tracks_keeps_their_artist(second: dict[str, Any]) -> None:
    tracks = _tracks(
        {"artist": ["Sullivan Murphy"], "remixer": ["Remi Xer"], "title": "Song 1 (Remi Xer Remix)"}, second
    )

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    lines = _lines(description)

    assert lines[0] == "[b]01.[/b] Sullivan Murphy - Song 1 (Remi Xer Remix) [i](3:20)[/i]"
    assert len(lines) == 2
    assert "remixed by" not in description


def _descgen_description(monkeypatch, metadata: dict[str, Any]) -> str:
    async def fake_run_metadata(url, **_options):
        return metadata, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: metadata)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda metadata: metadata)
    return anyio.run(build_description, ["https://www.qobuz.com/album/x"])


def test_descgen_says_one_remixer_on_every_track_once(monkeypatch) -> None:
    remix = [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")]
    album = _store_album(remix, remix)

    description = _descgen_description(monkeypatch, album)

    assert description.splitlines()[1:] == [
        "[b]01.[/b] Song 1",
        "[b]02.[/b] Song 2",
        "",
        "All tracks remixed by Remi Xer.",
    ]


REMIXED = {"remixer": ["Remi Xer"], "title": "Song (Remi Xer Remix)"}


@pytest.mark.parametrize(
    ("album", "second"),
    [
        ([("Various Artists", "main")], {"artist": ["Other Artist"], **REMIXED}),
        ([("Sullivan Murphy", "main")], {"artist": ["Other Artist"], **REMIXED}),
    ],
    ids=["various artists", "another artist on one track"],
)
def test_as_before_one_remixer_is_not_said_once_when_a_line_keeps_its_artist(
    album: list[tuple[str, str]], second: dict[str, Any]
) -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"], **REMIXED}, second)

    description = generate_description(tracks, _album(*album))

    assert _lines(description) == [
        "[b]01.[/b] Sullivan Murphy - Song (Remi Xer Remix) [i](3:20)[/i]",
        "[b]02.[/b] Other Artist - Song (Remi Xer Remix) [i](3:20)[/i]",
    ]
    assert "remixed by" not in description


def test_a_guest_goes_before_the_albums_one_remix() -> None:
    tracks = _tracks(
        {"artist": ["Sullivan Murphy (feat. Dierdre Simpson)"], **REMIXED},
        {"artist": ["Sullivan Murphy"], **REMIXED},
    )

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == [
        "[b]01.[/b] Song (feat. Dierdre Simpson) (Remi Xer Remix) [i](3:20)[/i]",
        "[b]02.[/b] Song (Remi Xer Remix) [i](3:20)[/i]",
    ]
    assert "All tracks remixed by Remi Xer." in description


@pytest.mark.parametrize(
    "tags",
    [
        ({"artist": ["Sullivan Murphy"], **REMIXED},),
        (
            {"artist": ["Sullivan Murphy"], "title": "Song (Remix)"},
            {"artist": ["Sullivan Murphy"], "title": "Song (Remix)"},
        ),
    ],
    ids=["one track", "a remix naming no one"],
)
def test_as_before_no_remixer_is_said_once_without_one_on_several_tracks(tags: tuple[dict[str, Any], ...]) -> None:
    description = generate_description(_tracks(*tags), _album(("Sullivan Murphy", "main")))

    lines = _lines(description)
    assert len(lines) == len(tags)
    assert all("Sullivan Murphy - " in line for line in lines)
    assert "remixed by" not in description


def test_every_guest_is_named_after_the_title() -> None:
    # The tags name each guest, so the line does, however many: no various-artists word.
    tracks = _tracks({"artist": ["Sullivan Murphy (feat. Ann, Bea, Cat & Dee)"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01.[/b] Song 1 (feat. Ann, Bea, Cat, Dee) [i](3:20)[/i]"]


def test_a_title_naming_only_some_of_the_guests_keeps_the_artist() -> None:
    # Carol would be named nowhere if the line left the artist out.
    tracks = _tracks({"artist": ["Sullivan Murphy (feat. Bob & Carol)"], "title": "Song (feat. Bob)"})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01.[/b] Sullivan Murphy (feat. Bob & Carol) - Song (feat. Bob) [i](3:20)[/i]"]


def test_a_title_crediting_one_guest_in_a_with_bracket_keeps_the_artist() -> None:
    # The line would name Bob twice: "Song (with Bob) (feat. Bob & Carol)".
    tracks = _tracks({"artist": ["Sullivan Murphy (feat. Bob & Carol)"], "title": "Song (with Bob)"})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01.[/b] Sullivan Murphy (feat. Bob & Carol) - Song (with Bob) [i](3:20)[/i]"]


@pytest.mark.parametrize("title", ["Song (feat. Bob)", "Sullivan Murphy's Song (feat. Bob)"])
def test_an_artists_name_the_title_does_not_credit_is_no_guest(title: str) -> None:
    # ARTISTS repeats the album's artist; the title credits someone else, whom the tags don't name, if it names the
    # artist outside its credit.
    tracks = _tracks({"artist": ["Sullivan Murphy"], "artists": ["Sullivan Murphy"], "title": title})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == [f"[b]01.[/b] Sullivan Murphy - {title} [i](3:20)[/i]"]


def test_a_title_crediting_a_guest_the_tags_dont_keeps_the_artist() -> None:
    # The tags credit Bob; the title credits Carol too.
    tracks = _tracks({"artist": ["Sullivan Murphy (feat. Bob)"], "title": "Song (feat. Bob & Carol)"})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01.[/b] Sullivan Murphy (feat. Bob) - Song (feat. Bob & Carol) [i](3:20)[/i]"]


def test_a_guests_name_inside_a_title_word_is_not_named() -> None:
    # "Ann" in "Anniversary" is no credit: Ann would be named nowhere if the line left the artist out.
    tracks = _tracks({"artist": ["Sullivan Murphy (feat. Bob & Ann)"], "title": "Anniversary (feat. Bob)"})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == [
        "[b]01.[/b] Sullivan Murphy (feat. Bob & Ann) - Anniversary (feat. Bob) [i](3:20)[/i]"
    ]


def test_as_before_a_guests_track_with_no_title_keeps_its_artist() -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy (feat. Dierdre Simpson)"], "title": None})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert _lines(description) == ["[b]01.[/b] Sullivan Murphy (feat. Dierdre Simpson) - None [i](3:20)[/i]"]


def test_the_remix_line_wraps_its_names_as_the_lines_do(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.description, "artist_tags_in_tracklist", True)
    tracks = _tracks({"artist": ["Sullivan Murphy"], **REMIXED}, {"artist": ["Sullivan Murphy"], **REMIXED})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main")))

    assert "\nAll tracks remixed by [artist]Remi Xer[/artist].\n" in description


def _remixed_album(title: str, remixer: list[str] | None = None) -> str:
    """Two tracks of a one-artist album, each a remix titled "Song N (title)", with these REMIXER values."""
    tags = [{"artist": ["Sullivan Murphy"], "remixer": remixer, "title": f"Song {n} {title}"} for n in (1, 2)]
    return generate_description(_tracks(*tags), _album(("Sullivan Murphy", "main")))


@pytest.mark.parametrize(
    "title",
    [
        "(2021 Remix)",
        "(Stereo Remix)",
        "(Mono Remix)",
        "(Dolby Atmos Remix)",
        "(2009 Stereo Remix)",
        "(50th Anniversary Remix)",
        "(Original Stereo Remix)",
        "(New Remix)",
        "(Spatial Audio Remix)",
        "(2009 Digital Remaster Remix)",
        "(Single Version Remix)",
        "(US Remix)",
        "(Alternate Remix)",
    ],
)
def test_a_year_or_the_kind_of_remix_in_every_title_is_no_remixer(title: str) -> None:
    description = _remixed_album(title)

    assert "remixed by" not in description
    assert all(line.startswith(f"[b]0{n}.[/b] Sullivan Murphy - ") for n, line in enumerate(_lines(description), 1))


@pytest.mark.parametrize(
    "title",
    [
        "(Steven Wilson Stereo Remix)",
        "(Steven Wilson Stereo 2021 Remix)",
        "(Steven Wilson 2021 Stereo Remix)",
        "(Steven Wilson Dolby Atmos Remix)",
    ],
)
def test_a_remixer_named_with_the_kind_of_remix_is_said_without_it(title: str) -> None:
    assert "\nAll tracks remixed by Steven Wilson.\n" in _remixed_album(title)


@pytest.mark.parametrize(
    "name",
    ["Thomas Dolby", "The 1975", "2000 And One", "Stereo MC's", "Brand New", "The Alternative", "Deluxe Edition"],
)
def test_as_before_a_remixer_s_name_holding_a_year_or_a_kind_of_remix_is_said_whole(name: str) -> None:
    assert f"\nAll tracks remixed by {name}.\n" in _remixed_album(f"({name} Remix)")


@pytest.mark.parametrize(
    ("remixer", "title"),
    [
        (["Steven Wilson"], "(2021 Remix)"),
        (["Remi Xer"], "(REMI XER Remix)"),
        (["Remi Xer"], "(Remi Xer's Remix)"),
        (["X & Y"], "(X & Y Remix)"),
        (["X"], "(X & Y Remix)"),
        (["Mono"], "(Mono Remix)"),
    ],
)
def test_a_remixer_in_remixer_is_said_and_the_title_not_read(remixer: list[str], title: str) -> None:
    assert f"\nAll tracks remixed by {', '.join(remixer)}.\n" in _remixed_album(title, remixer)


def test_as_before_every_remixer_in_remixer_is_said_when_the_title_names_one() -> None:
    assert "\nAll tracks remixed by X, Y.\n" in _remixed_album("(X Remix)", ["X", "Y"])


def test_a_remixer_in_remixer_on_one_track_and_in_the_title_of_another_is_said_once() -> None:
    tags = [
        {"artist": ["Sullivan Murphy"], "remixer": ["Steven Wilson"], "title": "Song 1 (2021 Remix)"},
        {"artist": ["Sullivan Murphy"], "title": "Song 2 (Steven Wilson Remix)"},
    ]

    description = generate_description(_tracks(*tags), _album(("Sullivan Murphy", "main")))

    assert "\nAll tracks remixed by Steven Wilson.\n" in description


@pytest.mark.parametrize(
    ("title", "line"),
    [("(2021 Remix)", None), ("(Steven Wilson Stereo Remix)", "All tracks remixed by Steven Wilson.")],
)
def test_descgen_reads_a_remixer_the_merge_read_from_the_titles_as_the_titles_are(
    monkeypatch, title: str, line: str | None
) -> None:
    # With several URLs, the merge credits what it reads in a later store's titles: "2021" is no one here either.
    remixer = title[1 : -len(" Remix)")]
    album = _store_album(*([("Sullivan Murphy", "main"), (remixer, "remixer")] for _ in (1, 2)), credited=False)
    for track in album["tracks"]["1"].values():
        track["title"] += f" {title}"

    description = _descgen_description(monkeypatch, album)

    assert (line in description.splitlines()) if line else ("remixed by" not in description)


def test_descgen_says_a_remixer_the_store_credits_whatever_the_title_reads(monkeypatch) -> None:
    album = _store_album(*([("Sullivan Murphy", "main"), ("Mono", "remixer")] for _ in (1, 2)))
    for track in album["tracks"]["1"].values():
        track["title"] += " (Mono Remix)"

    assert "All tracks remixed by Mono." in _descgen_description(monkeypatch, album).splitlines()
