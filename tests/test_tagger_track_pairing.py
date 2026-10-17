import asyncio
from copy import deepcopy
from types import SimpleNamespace

import anyio
import pytest

from salmon import interaction
from salmon.errors import AmbiguousTrackOrderError, NumberedTrackMoveError
from salmon.tagger import retagger
from salmon.tagger.combine import combine_tracks
from salmon.tagger.pairing import pair_by_content
from salmon.tagger.pre_data import create_track_list
from salmon.tagger.retagger import create_track_changes, tag_files
from salmon.tagger.sources.beatport import Scraper as BeatportScraper
from salmon.tagger.sources.discogs import parse_duration


def _tagset(title, *, tracknumber=None, isrc=None, length=None):
    return SimpleNamespace(
        artist=["Some Artist"],
        title=title,
        composer=None,
        conductor=None,
        comment=None,
        isrc=isrc,
        tracknumber=tracknumber,
        discnumber=None,
        tracktotal=None,
        disctotal=None,
        mut=SimpleNamespace(info=SimpleNamespace(length=length)),
    )


def _trackmeta(title, track_no, *, isrc=None, duration=None):
    return {
        "title": title,
        "isrc": isrc,
        "duration": duration,
        "track#": track_no,
        "disc#": "1",
        "tracktotal": None,
        "disctotal": None,
        "artists": [("Some Artist", "main")],
    }


def _one_disc(*tracks):
    return {"tracks": {"1": {track["track#"]: track for track in tracks}}}


def _proposed(changes, tag):
    """The new value of `tag` proposed for each file, or None when it is left as it is."""
    return {
        filename: next((change.new for change in file_changes if change.tag == tag), None)
        for filename, file_changes in changes.items()
    }


def test_unnumbered_promo_files_get_their_own_tracks_whatever_their_file_names():
    # A promo folder: no TRACKNUMBER, and file names whose order is not the release's order. Paired by
    # position, Django.flac took Work's title and track number, and so on down the folder.
    tags = {
        "Django.flac": _tagset("Django"),
        "I Walk.flac": _tagset("I Walk"),
        "Work.flac": _tagset("Work"),
    }
    metadata = _one_disc(
        _trackmeta("Work (Original Mix)", "1"),
        _trackmeta("Django (Original Mix)", "2"),
        _trackmeta("I Walk (Original Mix)", "3"),
    )

    changes = create_track_changes(tags, metadata)

    assert _proposed(changes, "title") == {
        "Django.flac": "Django (Original Mix)",
        "I Walk.flac": "I Walk (Original Mix)",
        "Work.flac": "Work (Original Mix)",
    }
    assert _proposed(changes, "tracknumber") == {"Django.flac": "2", "I Walk.flac": "3", "Work.flac": "1"}


def _numbered_for_another_edition():
    # The ISRCs say 01.flac is the source's track 2, and 02.flac its track 1.
    tags = {
        "01.flac": _tagset(None, tracknumber="1", isrc="GB-AAA-26-00002"),
        "02.flac": _tagset(None, tracknumber="2", isrc="GBAAA2600001"),
    }
    metadata = _one_disc(
        _trackmeta("First", "1", isrc="GBAAA2600001"),
        _trackmeta("Second", "2", isrc="GBAAA2600002"),
    )
    return tags, metadata


def test_numbered_files_the_isrc_moves_are_not_retagged_as_other_tracks_without_asking():
    tags, metadata = _numbered_for_another_edition()

    with pytest.raises(NumberedTrackMoveError) as refused:
        create_track_changes(tags, metadata)

    assert [(filename, before["track#"], after["track#"]) for filename, before, after, _ in refused.value.moves] == [
        ("01.flac", "1", "2"),
        ("02.flac", "2", "1"),
    ]
    changes = create_track_changes(tags, metadata, move_numbered=True)
    assert _proposed(changes, "title") == {"01.flac": "Second", "02.flac": "First"}


def _run_tag_files(monkeypatch, tags, metadata, *, answer):
    asked, written = [], []

    async def confirm(text, **kwargs):
        asked.append(text)
        return answer

    monkeypatch.setattr(interaction, "confirm", confirm)
    monkeypatch.setattr(retagger, "print_changes", lambda *args: None)
    monkeypatch.setattr(retagger, "retag_files", lambda path, album, tracks: written.append(tracks))
    monkeypatch.setattr(retagger, "collect_album_data", lambda metadata: {})
    anyio.run(lambda: tag_files("/unused", tags, metadata, auto_rename=True))
    return asked, written


def test_tag_files_retags_numbered_files_as_other_tracks_when_told_to(monkeypatch):
    tags, metadata = _numbered_for_another_edition()

    asked, written = _run_tag_files(monkeypatch, tags, metadata, answer=True)

    assert len(asked) == 1
    assert _proposed(written[0], "title") == {"01.flac": "Second", "02.flac": "First"}


@pytest.mark.parametrize("assume_defaults", [False, True], ids=["declined", "-yyy"])
def test_tag_files_skips_retagging_numbered_files_it_may_not_move(monkeypatch, capsys, assume_defaults):
    tags, metadata = _numbered_for_another_edition()

    with interaction.assuming_defaults(assume_defaults):
        asked, written = _run_tag_files(monkeypatch, tags, metadata, answer=False)

    assert len(asked) == (0 if assume_defaults else 1)
    assert written == []
    assert "Skipping retagging procedure" in capsys.readouterr().out


def test_a_numbered_folder_keeps_its_numbers_when_only_durations_disagree():
    # Titles that fit nothing, and the source's durations (from another edition, say) closest to another file's.
    tags = {
        "01.flac": _tagset("Foo", tracknumber="1", length=200.3),
        "02.flac": _tagset("Bar", tracknumber="2", length=250.1),
        "03.flac": _tagset("Baz", tracknumber="3", length=300.4),
    }
    metadata = _one_disc(
        _trackmeta("One", "1", duration=249),
        _trackmeta("Two", "2"),
        _trackmeta("Three", "3", duration=300),
    )

    changes = create_track_changes(tags, metadata)

    assert _proposed(changes, "title") == {"01.flac": "One", "02.flac": "Two", "03.flac": "Three"}


def test_a_title_that_only_fits_does_not_move_a_numbered_file():
    tags = {"01.flac": _tagset("Django", tracknumber="1"), "02.flac": _tagset("Work", tracknumber="2")}
    metadata = _one_disc(_trackmeta("Work (Original Mix)", "1"), _trackmeta("Django (Original Mix)", "2"))

    changes = create_track_changes(tags, metadata)

    assert _proposed(changes, "title") == {"01.flac": "Work (Original Mix)", "02.flac": "Django (Original Mix)"}


def test_a_duration_alone_pairs_nothing():
    locals_ = [{"title": "Foo", "duration": 250}, {"title": "Bar", "duration": 200}]
    metas = [{"title": "One", "duration": 200}, {"title": "Two", "duration": 250}]

    assert pair_by_content(locals_, metas) == ([0, 1], ["position", "position"])


def test_duration_tells_apart_two_files_with_the_same_title():
    tags = {"a.flac": _tagset("Django", length=420.4), "b.flac": _tagset("Django", length=300.2)}
    metadata = _one_disc(
        _trackmeta("Django (Original Mix)", "1", duration=300),
        _trackmeta("Django (Extended Mix)", "2", duration=420),
    )

    changes = create_track_changes(tags, metadata)

    assert _proposed(changes, "title") == {"a.flac": "Django (Extended Mix)", "b.flac": "Django (Original Mix)"}


def test_files_whose_order_is_ambiguous_are_tagged_when_their_titles_place_every_one():
    # Two folders against a one-disc tracklist, with clashing track numbers: no order can be trusted.
    tags = {
        "a/x.flac": _tagset("Work", tracknumber="1"),
        "a/y.flac": _tagset("I Walk", tracknumber="1"),
        "b/z.flac": _tagset("Django", tracknumber="1"),
    }
    metadata = _one_disc(
        _trackmeta("Work (Original Mix)", "1"),
        _trackmeta("Django (Original Mix)", "2"),
        _trackmeta("I Walk (Original Mix)", "3"),
    )

    changes = create_track_changes(tags, metadata)

    assert _proposed(changes, "title") == {
        "a/x.flac": "Work (Original Mix)",
        "a/y.flac": "I Walk (Original Mix)",
        "b/z.flac": "Django (Original Mix)",
    }


def test_files_whose_order_is_ambiguous_are_still_refused_when_titles_cannot_place_them():
    tags = {
        "a/x.flac": _tagset("Foo", tracknumber="1"),
        "a/y.flac": _tagset("Bar", tracknumber="1"),
        "b/z.flac": _tagset("Baz", tracknumber="1"),
    }
    metadata = _one_disc(
        _trackmeta("Work (Original Mix)", "1"),
        _trackmeta("Django (Original Mix)", "2"),
        _trackmeta("I Walk (Original Mix)", "3"),
    )

    with pytest.raises(AmbiguousTrackOrderError):
        create_track_changes(tags, metadata)


def test_files_whose_order_is_ambiguous_are_refused_when_a_duration_had_to_place_one():
    # The two Django files' titles fit both Django tracks: only their durations tell them apart.
    tags = {
        "a/x.flac": _tagset("Django", tracknumber="1", length=300),
        "a/y.flac": _tagset("Django", tracknumber="1", length=420),
        "b/z.flac": _tagset("Work", tracknumber="1"),
    }
    metadata = _one_disc(
        _trackmeta("Django (Original Mix)", "1", duration=300),
        _trackmeta("Django (Extended Mix)", "2", duration=420),
        _trackmeta("Work (Original Mix)", "3"),
    )

    with pytest.raises(AmbiguousTrackOrderError):
        create_track_changes(tags, metadata)


def test_a_title_that_fits_two_tracks_is_not_matched_to_either():
    # "Django" is in both scraped titles, so it says nothing about which one the file is.
    locals_ = [{"title": "Django"}, {"title": "Untitled"}]
    metas = [{"title": "Django (Dub)"}, {"title": "Django (Original Mix)"}]

    assert pair_by_content(locals_, metas) == ([0, 1], ["position", "position"])


def test_a_title_is_found_in_a_longer_one_only_as_whole_words():
    # Matched as text, "Work" and "Home" are both in "Homework", which then wants neither file.
    locals_ = [{"title": "Work"}, {"title": "Home"}]
    metas = [{"title": "Homework"}, {"title": "Work (Original Mix)"}]

    assert pair_by_content(locals_, metas) == ([1, 0], ["position", "partial title"])


def test_tracks_nothing_identifies_keep_their_order():
    # "A" is a letter of "Original", not a word of it.
    locals_ = [{"title": "B"}, {"title": "Work"}, {"title": "A"}]
    metas = [{"title": "One"}, {"title": "Two"}, {"title": "Work (Original Mix)"}]

    assert pair_by_content(locals_, metas) == ([0, 2, 1], ["position", "position", "partial title"])


def _track(title, track_no, *, isrc=None, peak=None, duration=None, numbered=False):
    return {
        "track#": track_no,
        "disc#": "1",
        "tracktotal": "3",
        "disctotal": "1",
        "artists": [("Some Artist", "main")],
        "title": title,
        "replay_gain": None,
        "peak": peak,
        "isrc": isrc,
        "explicit": None,
        "format": None,
        "streamable": None,
        "duration": duration,
        "numbered": numbered,
    }


def test_combine_tracks_keeps_numbered_local_tracks_when_only_durations_disagree():
    base = {
        "1": {
            "1": _track("Foo", "1", peak="0.5", duration=200, numbered=True),
            "2": _track("Bar", "2", peak="0.6", duration=250, numbered=True),
            "3": _track("Baz", "3", peak="0.7", duration=300, numbered=True),
        }
    }
    meta = {
        "1": {
            "1": _track("One", "1", duration=249),
            "2": _track("Two", "2"),
            "3": _track("Three", "3", duration=300),
        }
    }

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert {num: track["peak"] for num, track in combined["1"].items()} == {"1": "0.5", "2": "0.6", "3": "0.7"}


def test_combine_tracks_merges_each_scraped_track_onto_its_own_local_track():
    # Unnumbered local tracks, numbered in file-name order; the source lists them in release order.
    base = {
        "1": {
            "1": _track("Django", "1", peak="0.5"),
            "2": _track("I Walk", "2", peak="0.6"),
            "3": _track("Work", "3", peak="0.7"),
        }
    }
    meta = {
        "1": {
            "1": _track("Work (Original Mix)", "1", isrc="GBAAA2600001"),
            "2": _track("Django (Original Mix)", "2", isrc="GBAAA2600002"),
            "3": _track("I Walk (Original Mix)", "3", isrc="GBAAA2600003"),
        }
    }

    combined = combine_tracks(deepcopy(base), deepcopy(meta), True)

    assert {num: (track["peak"], track["title"], track["isrc"]) for num, track in combined["1"].items()} == {
        "1": ("0.7", "Work (Original Mix)", "GBAAA2600001"),
        "2": ("0.5", "Django (Original Mix)", "GBAAA2600002"),
        "3": ("0.6", "I Walk (Original Mix)", "GBAAA2600003"),
    }


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [("4:05", 245), ("12:00", 720), ("1:02:03", 3723), (" 4:05 ", 245), ("", None), (None, None), ("4:0x", None)],
)
def test_discogs_track_durations_are_read_in_seconds(duration, seconds):
    assert parse_duration(duration) == seconds


def test_beatport_track_length_is_kept_in_seconds():
    soup = {
        "tracks": [
            {
                "id": 1,
                "name": "Work",
                "mix_name": "Original Mix",
                "artists": [{"name": "Some Artist"}],
                "remixers": [],
                "is_available_for_streaming": True,
                "isrc": None,
                "length_ms": 245_900,
            }
        ]
    }

    tracks = asyncio.run(BeatportScraper().parse_tracks(soup))

    assert tracks["1"]["1"]["duration"] == 245


def test_local_tracks_carry_the_duration_of_their_file():
    tags = {
        "01 Work.flac": SimpleNamespace(
            artist=["Some Artist"],
            title="Work",
            tracknumber="1",
            discnumber=None,
            tracktotal=None,
            disctotal=None,
            replay_gain=None,
            peak=None,
            isrc=None,
        )
    }

    tracks = create_track_list(tags, False, {"01 Work.flac": {"duration": 245}})

    assert tracks["1"]["1"]["duration"] == 245
    assert tracks["1"]["1"]["numbered"] is True


def test_a_local_track_without_a_track_number_is_not_numbered():
    tags = {
        "Work.flac": SimpleNamespace(
            artist=["Some Artist"],
            title="Work",
            tracknumber=None,
            discnumber=None,
            tracktotal=None,
            disctotal=None,
            replay_gain=None,
            peak=None,
            isrc=None,
        )
    }

    tracks = create_track_list(tags, False)

    assert tracks["1"]["1"]["numbered"] is False
