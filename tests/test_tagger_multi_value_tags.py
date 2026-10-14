"""Retagging writes one value per genre and per artist (GENRE, ARTISTS) in FLAC and MP3; ARTIST stays the credit."""

import struct

import av
import msgspec
import numpy as np
import pytest
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, VCFLACDict
from mutagen.id3 import ID3, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from salmon import cfg
from salmon.converter import transcoding
from salmon.tagger.pre_data import construct_track_artists, split_genres
from salmon.tagger.retagger import (
    collect_album_data,
    create_track_changes,
    print_changes,
    retag_files,
)
from salmon.tagger.tagfile import TagFile

FRAME_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
FRAME_SIZE = 417


def _write_flac(path, **tags) -> None:
    """A FLAC with no audio: a STREAMINFO block and the given Vorbis comments, key case preserved.

    A list value is one field per item.
    """
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def _write_mp3(path, *frames) -> None:
    """A silent MP3 with an ID3v2.4 tag holding ``frames``."""
    path.write_bytes((FRAME_HEADER + bytes(FRAME_SIZE - len(FRAME_HEADER))) * 3)
    tags = ID3()
    for frame in frames:
        tags.add(frame)
    tags.save(path, v2_version=4)


def _write_m4a(path, **tags) -> None:
    with av.open(str(path), "w", format="ipod") as container:
        stream = container.add_stream("aac", rate=44100, layout="stereo")
        frame = av.AudioFrame.from_ndarray(np.zeros((2, 2048), dtype=np.float32), format="fltp", layout="stereo")
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    audio = MP4(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def _trackmeta(artists, title="Song", number="1"):
    return {
        "title": title,
        "isrc": None,
        "track#": number,
        "disc#": "1",
        "tracktotal": None,
        "disctotal": None,
        "artists": artists,
    }


def _metadata(*tracks, genres=("Rap", "Hip Hop")):
    return {
        "title": "Album",
        "edition_title": None,
        "genres": set(genres),
        "group_year": "2020",
        "label": None,
        "catno": None,
        "upc": None,
        "comment": None,
        "artists": [("Dree Low", "main")],
        "tracks": {"1": {str(i): track for i, track in enumerate(tracks, 1)}},
    }


def _gather(tmp_path):
    return {path.name: TagFile(str(path)) for path in sorted(tmp_path.iterdir())}


def _retag(tmp_path, metadata):
    """What ``tag_files`` does after the user agrees, returning what it proposed."""
    tags = _gather(tmp_path)
    album_changes = collect_album_data(metadata)
    track_changes = create_track_changes(tags, metadata)
    retag_files(str(tmp_path), album_changes, track_changes)
    return album_changes, track_changes


def _flac_tags(path):
    """Every comment in the file, in order, name case as stored."""
    tags = FLAC(path).tags
    assert isinstance(tags, VCFLACDict)
    return list(tags)


def _mp3_frames(path):
    """Every ID3 frame as (HashKey, text), in a stable order."""
    tags = MP3(path).tags
    assert tags is not None
    return sorted((key, list(frame.text)) for key, frame in tags.items() if hasattr(frame, "text"))


CREDIT = [("Dree Low", "main"), ("Owen", "guest")]


@pytest.fixture(autouse=True)
def _formatting(monkeypatch):
    monkeypatch.setattr(
        cfg.upload,
        "formatting",
        msgspec.structs.replace(cfg.upload.formatting, guests_in_track_title=False, various_artist_threshold=4),
    )


def _flac_album(tmp_path, **tags):
    _write_flac(tmp_path / "01.flac", TRACKNUMBER="1", DISCNUMBER="1", TITLE="Song", **tags)
    return tmp_path / "01.flac"


def _mp3_album(tmp_path, *frames):
    from mutagen.id3 import TRCK

    _write_mp3(tmp_path / "01.mp3", TRCK(text="1"), *frames)
    return tmp_path / "01.mp3"


def test_flac_gets_one_field_per_genre_and_per_artist(tmp_path):
    path = _flac_album(tmp_path, ARTIST="Old")

    _retag(tmp_path, _metadata(_trackmeta(CREDIT)))

    # Iterate the comment, not .keys(): that normalizes every name to lowercase.
    assert sorted((k, v) for k, v in _flac_tags(path) if k.upper() in {"GENRE", "ARTIST", "ARTISTS"}) == [
        ("ARTIST", "Dree Low (feat. Owen)"),
        ("ARTISTS", "Dree Low"),
        ("ARTISTS", "Owen"),
        ("GENRE", "Hip Hop"),
        ("GENRE", "Rap"),
    ]
    audio = FLAC(path)
    assert audio["genre"] == ["Hip Hop", "Rap"]
    assert audio["artist"] == ["Dree Low (feat. Owen)"]
    assert audio["artists"] == ["Dree Low", "Owen"]


def test_mp3_gets_one_value_per_genre_and_per_artist(tmp_path):
    path = _mp3_album(tmp_path)

    _retag(tmp_path, _metadata(_trackmeta(CREDIT)))

    tags = MP3(path).tags
    assert tags is not None
    assert tags.version == (2, 4, 0)
    assert tags["TCON"].text == ["Hip Hop", "Rap"]
    assert tags["TPE1"].text == ["Dree Low (feat. Owen)"]
    assert tags["TXXX:ARTISTS"].text == ["Dree Low", "Owen"]
    assert len(tags.getall("TCON")) == 1
    assert len(tags.getall("TXXX")) == 1


@pytest.mark.parametrize("kind", ["flac", "mp3"])
def test_retagging_twice_proposes_nothing_for_genre_or_artists_and_changes_nothing(tmp_path, kind, capsys):
    path = _flac_album(tmp_path) if kind == "flac" else _mp3_album(tmp_path)
    metadata = _metadata(_trackmeta(CREDIT))
    _retag(tmp_path, metadata)
    read = _flac_tags if kind == "flac" else _mp3_frames
    first = read(path)
    assert ("ARTISTS", "Owen") in first if kind == "flac" else ("TXXX:ARTISTS", ["Dree Low", "Owen"]) in first

    album_changes, track_changes = _retag(tmp_path, metadata)

    assert [c.tag for c in track_changes["01." + kind]] == []
    capsys.readouterr()
    print_changes(album_changes, track_changes, _gather(tmp_path)["01." + kind])
    out = capsys.readouterr().out
    assert "artists" not in out
    assert next(line for line in out.splitlines() if "genre" in line).count(">>>") == 0
    assert read(path) == first


def test_flac_leftovers_in_any_case_are_replaced(tmp_path):
    path = _flac_album(tmp_path, artists="X", ARTIST=["A", "B", "C"], genre="Old; Genre")

    _retag(tmp_path, _metadata(_trackmeta(CREDIT)))

    pairs = _flac_tags(path)
    assert sorted(k for k, _ in pairs if k.upper() in {"ARTIST", "ARTISTS", "GENRE"}) == [
        "ARTIST",
        "ARTISTS",
        "ARTISTS",
        "GENRE",
        "GENRE",
    ]
    audio = FLAC(path)
    assert audio["artist"] == ["Dree Low (feat. Owen)"]
    assert audio["artists"] == ["Dree Low", "Owen"]


def test_mp3_txxx_artists_in_another_case_is_replaced(tmp_path):
    path = _mp3_album(tmp_path, TXXX(encoding=3, desc="artists", text=["X"]))

    _retag(tmp_path, _metadata(_trackmeta(CREDIT)))

    tags = MP3(path).tags
    assert tags is not None
    assert [frame.desc for frame in tags.getall("TXXX")] == ["ARTISTS"]
    assert tags["TXXX:ARTISTS"].text == ["Dree Low", "Owen"]


@pytest.mark.parametrize("guests_in_title", [False, True])
def test_artists_lists_every_main_and_guest_by_name(tmp_path, monkeypatch, guests_in_title):
    monkeypatch.setattr(
        cfg.upload,
        "formatting",
        msgspec.structs.replace(
            cfg.upload.formatting, guests_in_track_title=guests_in_title, various_artist_threshold=3
        ),
    )
    path = _flac_album(tmp_path)
    artists = [("Main One", "main"), ("Main Two", "main"), ("G1", "guest"), ("G2", "guest"), ("G3", "guest")]

    _retag(tmp_path, _metadata(_trackmeta(artists)))

    audio = FLAC(path)
    assert audio["artists"] == ["Main One", "Main Two", "G1", "G2", "G3"]
    # The credit is where the threshold and the title setting apply
    if guests_in_title:
        assert audio["artist"] == ["Main One & Main Two"]
    else:
        assert audio["artist"] == [f"Main One & Main Two (feat. {cfg.upload.formatting.various_artist_word})"]


def test_artists_leaves_out_remixers_and_composers(tmp_path):
    path = _flac_album(tmp_path)
    artists = [("Dree Low", "main"), ("Owen", "guest"), ("Rem Ixer", "remixer"), ("Com Poser", "composer")]

    _retag(tmp_path, _metadata(_trackmeta(artists)))

    assert FLAC(path)["artists"] == ["Dree Low", "Owen"]
    assert FLAC(path)["composer"] == ["Com Poser"]


def test_a_track_with_one_artist_still_gets_artists(tmp_path):
    path = _flac_album(tmp_path)

    _retag(tmp_path, _metadata(_trackmeta([("Dree Low", "main")])))

    assert FLAC(path)["artists"] == ["Dree Low"]


def test_a_name_that_is_main_and_guest_is_listed_once(tmp_path):
    path = _flac_album(tmp_path)

    _retag(tmp_path, _metadata(_trackmeta([("Dree Low", "main"), ("Dree Low", "guest"), ("Owen", "guest")])))

    assert FLAC(path)["artists"] == ["Dree Low", "Owen"]


def test_transcoded_mp3_has_the_frames_of_a_retagged_mp3(tmp_path):
    flac_dir, mp3_dir, out_dir = tmp_path / "flac", tmp_path / "mp3", tmp_path / "out"
    for folder in (flac_dir, mp3_dir, out_dir):
        folder.mkdir()
    metadata = _metadata(_trackmeta(CREDIT))
    flac_path = _flac_album(flac_dir)
    _retag(flac_dir, metadata)
    mp3_path = _mp3_album(mp3_dir)
    _retag(mp3_dir, metadata)

    transcoded = out_dir / "01.mp3"
    transcoded.write_bytes(mp3_path.read_bytes())
    MP3(transcoded).delete()
    flac_obj, tag_dict = transcoding._parse_flac_tags(flac_path)
    transcoding._copy_tags(tag_dict, flac_obj, transcoded)

    def frames(path):
        tags = MP3(path).tags
        assert tags is not None
        return {key: tags[key].text for key in ("TCON", "TPE1", "TXXX:ARTISTS")}

    assert frames(transcoded) == frames(mp3_path)
    assert frames(transcoded)["TXXX:ARTISTS"] == ["Dree Low", "Owen"]
    tags = MP3(transcoded).tags
    assert tags is not None
    assert [f.desc for f in tags.getall("TXXX")] == ["ARTISTS"]


def test_proposed_changes_show_artists_only_when_it_differs(tmp_path, capsys):
    _flac_album(tmp_path, ARTIST="Dree Low (feat. Owen)", ARTISTS="Dree Low")
    tags = _gather(tmp_path)
    metadata = _metadata(_trackmeta(CREDIT))

    changes = create_track_changes(tags, metadata)
    print_changes(collect_album_data(metadata), changes, tags["01.flac"])

    out = capsys.readouterr().out
    assert [c.tag for c in changes["01.flac"]] == ["artists"]
    assert f"  {'artists'.ljust(20)} ••• Dree Low >>> Dree Low; Owen" in out
    assert "Hip Hop; Rap" in out


def test_proposed_artists_for_a_file_without_any_say_none(tmp_path, capsys):
    _flac_album(tmp_path, ARTIST="Dree Low (feat. Owen)")
    tags = _gather(tmp_path)
    metadata = _metadata(_trackmeta(CREDIT))

    changes = create_track_changes(tags, metadata)
    print_changes(collect_album_data(metadata), changes, tags["01.flac"])

    assert f"  {'artists'.ljust(20)} ••• None >>> Dree Low; Owen" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["flac", "mp3"])
def test_reading_a_retagged_file_back_gives_the_same_genres_and_artists(tmp_path, kind):
    _flac_album(tmp_path) if kind == "flac" else _mp3_album(tmp_path)
    _retag(tmp_path, _metadata(_trackmeta(CREDIT)))

    track = TagFile(str(next(tmp_path.iterdir())))

    assert split_genres(track.genre) == ["Hip Hop", "Rap"]
    assert construct_track_artists(track) == [("Owen", "guest"), ("Dree Low", "main")]


def test_an_m4a_gets_the_genre_string_and_no_artists(tmp_path):
    path = tmp_path / "01.m4a"
    _write_m4a(path, trkn=[(1, 0)])
    metadata = _metadata(_trackmeta(CREDIT))

    _, track_changes = _retag(tmp_path, metadata)

    assert "artists" not in [c.tag for c in track_changes["01.m4a"]]
    audio = MutagenFile(path)
    assert audio is not None and audio.tags is not None
    assert audio.tags["\xa9gen"] == ["Hip Hop; Rap"]
    assert audio.tags["\xa9ART"] == ["Dree Low (feat. Owen)"]
    assert not [key for key in audio.tags if "artists" in str(key).lower()]


def _preview(tmp_path, metadata, name):
    tags = _gather(tmp_path)
    changes = create_track_changes(tags, metadata)
    print_changes(collect_album_data(metadata), changes, tags[name])
    return changes


def _preview_line(out, start):
    """What follows the bullets on the first line of the output that starts with ``start``."""
    return next(line for line in out.splitlines() if line.lstrip().startswith(start)).split("•••")[1].strip()


def test_preview_marks_genre_changed_when_the_file_holds_it_as_one_joined_value(tmp_path, capsys):
    _flac_album(tmp_path, GENRE="Hip Hop; Rap")

    _preview(tmp_path, _metadata(_trackmeta(CREDIT)), "01.flac")

    assert _preview_line(capsys.readouterr().out, "> genre") == '"Hip Hop; Rap" >>> "Hip Hop", "Rap"'


def test_preview_marks_artists_changed_when_the_file_holds_them_as_one_joined_value(tmp_path, capsys):
    _flac_album(tmp_path, ARTIST="Dree Low (feat. Owen)", ARTISTS="Dree Low; Owen")

    changes = _preview(tmp_path, _metadata(_trackmeta(CREDIT)), "01.flac")

    assert [c.tag for c in changes["01.flac"]] == ["artists"]
    assert _preview_line(capsys.readouterr().out, "artists") == '"Dree Low; Owen" >>> "Dree Low", "Owen"'


def test_preview_does_not_quote_values_when_the_sides_already_read_differently(tmp_path, capsys):
    _flac_album(tmp_path, GENRE="Jazz")

    _preview(tmp_path, _metadata(_trackmeta(CREDIT)), "01.flac")

    assert _preview_line(capsys.readouterr().out, "> genre") == "Jazz >>> Hip Hop; Rap"


def test_preview_leaves_an_m4a_genre_string_unchanged(tmp_path, capsys):
    _write_m4a(tmp_path / "01.m4a", trkn=[(1, 0)], **{"\xa9gen": ["Hip Hop; Rap"]})

    _preview(tmp_path, _metadata(_trackmeta(CREDIT)), "01.m4a")

    assert ">>>" not in _preview_line(capsys.readouterr().out, "> genre")
