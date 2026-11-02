"""The group description's tracklist leaves out a track's artist when it credits only the album's artists (#895).

A track that credits anyone else keeps its artist, as do every track of a compilation, anthology, DJ mix or
various-artists release, and every track when no album artists are given (a cross-upload). salmon up reads the
credits from the files' tags; salmon descgen (and the web interface's Tools page) from the store's metadata.
"""

import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from helpers.tagger_metadata_corrections import _track
from mutagen.mp3 import MP3

import salmon.commands
from salmon import cfg
from salmon.commands import build_description
from salmon.tagger.retagger import create_artist_str, create_track_changes, retag_files
from salmon.tagger.tags import gather_tags
from salmon.uploader.upload import generate_description


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
        # Each track keeps its artist when it credits anyone besides the album's.
        (
            [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")],
            {"artist": ["Sullivan Murphy (feat. Dierdre Simpson)"]},
            "Sullivan Murphy (feat. Dierdre Simpson) - Song 1",
        ),
        (
            # guests_in_track_title: the guest is in the title, and in ARTISTS after the main artists.
            [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")],
            {
                "artist": ["Sullivan Murphy"],
                "artists": ["Sullivan Murphy", "Dierdre Simpson"],
                "title": "Song 1 (feat. Dierdre Simpson)",
            },
            "Sullivan Murphy - Song 1 (feat. Dierdre Simpson)",
        ),
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
            # A remixer salmon reads from the title, which it writes to REMIXER only when a store credits them.
            [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remi Xer Mix)"},
            "Sullivan Murphy - Song 1 (Remi Xer Mix)",
        ),
        (
            [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")],
            {"artist": ["Sullivan Murphy"], "title": "Song 1 (Remi Xer Edit)"},
            "Sullivan Murphy - Song 1 (Remi Xer Edit)",
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
            # The band's members in ARTISTS name more than the album's credit.
            [("Rush", "main")],
            {"artist": ["Rush"], "artists": ["Geddy Lee", "Alex Lifeson", "Neil Peart"]},
            "Rush - Song 1",
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
        "a guest in ARTIST",
        "a guest in the title",
        "a guest in the title alone",
        "a remix in the title alone",
        "a remixer's mix in the title",
        "a remixer's edit in the title",
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


@pytest.mark.parametrize("rls_type", ["Compilation", "Anthology", "DJ Mix"])
def test_every_track_of_a_compilation_anthology_or_dj_mix_keeps_its_artist(rls_type: str) -> None:
    tracks = _tracks({"artist": ["Sullivan Murphy"]})

    description = generate_description(tracks, _album(("Sullivan Murphy", "main"), rls_type=rls_type))

    assert _lines(description) == ["[b]01.[/b] Sullivan Murphy - Song 1 [i](3:20)[/i]"]


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


def _descgen(monkeypatch, metadata: dict[str, Any]) -> list[str]:
    async def fake_run_metadata(url, return_source_name=False):
        return metadata, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: metadata)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda metadata: metadata)
    description = anyio.run(build_description, ["https://www.qobuz.com/album/x"])
    return _lines(description)


def _store_album(*tracks: list[tuple[str, str]], rls_type: str = "Album") -> dict[str, Any]:
    artists = list(dict.fromkeys(credit for track in tracks for credit in track))
    return {
        "artists": artists,
        "rls_type": rls_type,
        "tracks": {"1": {str(n): {"artists": track, "title": f"Song {n}"} for n, track in enumerate(tracks, 1)}},
        "comment": None,
        "urls": [],
    }


def test_descgen_names_no_artist_on_a_one_artist_album(monkeypatch) -> None:
    album = _store_album([("Sullivan Murphy", "main")], [("Sullivan Murphy", "main"), ("A Composer", "composer")])

    lines = _descgen(monkeypatch, album)

    assert lines == ["[b]01.[/b] Song 1", "[b]02.[/b] Song 2"]


def test_descgen_names_the_artist_of_a_track_that_credits_anyone_else(monkeypatch) -> None:
    guest = [("Sullivan Murphy", "main"), ("Dierdre Simpson", "guest")]
    remix = [("Sullivan Murphy", "main"), ("Remi Xer", "remixer")]
    album = _store_album([("Sullivan Murphy", "main")], guest, remix)

    lines = _descgen(monkeypatch, album)

    assert lines == [
        "[b]01.[/b] Song 1",
        f"[b]02.[/b] {create_artist_str(guest)} - Song 2",
        f"[b]03.[/b] {create_artist_str(remix)} - Song 3",
    ]


def test_descgen_names_every_tracks_artist_on_a_compilation(monkeypatch) -> None:
    album = _store_album([("Sullivan Murphy", "main")], rls_type="Compilation")

    lines = _descgen(monkeypatch, album)

    assert lines == ["[b]01.[/b] Sullivan Murphy - Song 1"]


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

    guest = (
        "Sullivan Murphy - Song 2 (feat. Dierdre Simpson)"
        if guests_in_track_title
        else ("Sullivan Murphy (feat. Dierdre Simpson) - Song 2")
    )
    assert _lines(description) == [
        "[b]01.[/b] Song 1 [i](3:20)[/i]",
        f"[b]02.[/b] {guest} [i](3:20)[/i]",
        "[b]03.[/b] Sullivan Murphy - Song 3 (Remi Xer Remix) [i](3:20)[/i]",
        "[b]04.[/b] Sullivan Murphy - Song 4 [i](3:20)[/i]",
        "[b]05.[/b] Other Artist - Song 5 [i](3:20)[/i]",
    ]


def test_descgen_names_no_artist_on_a_track_by_the_albums_orchestra_and_conductor(monkeypatch) -> None:
    track = [("Orchestra", "main"), ("A Conductor", "conductor"), ("A Composer", "composer")]
    guest = [("Orchestra", "main"), ("A Conductor", "conductor"), ("A Soloist", "guest")]
    album = _store_album(track, guest)

    lines = _descgen(monkeypatch, album)

    assert lines == [
        "[b]01.[/b] Song 1",
        f"[b]02.[/b] {create_artist_str(guest)} - Song 2",
    ]


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
