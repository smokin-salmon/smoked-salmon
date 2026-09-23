from types import SimpleNamespace

from salmon.tagger.retagger import Change, _get_tag_number, create_track_changes


def _trackmeta(title, track_no, disc_no):
    return {
        "title": title,
        "isrc": None,
        "track#": track_no,
        "disc#": disc_no,
        "tracktotal": None,
        "disctotal": None,
        "artists": [("Some Artist", "main")],
    }


def _tagset(title, tracknumber, discnumber):
    return SimpleNamespace(
        artist=["Some Artist"],
        title=title,
        composer=None,
        conductor=None,
        comment=None,
        isrc=None,
        tracknumber=tracknumber,
        discnumber=discnumber,
        tracktotal=None,
        disctotal=None,
    )


def test_create_track_changes_matches_files_by_disc_and_track_number():
    # The tags dict is built out of disc/track order (as os.walk would give it),
    # so a positional zip against the metadata tracklist would pair the wrong
    # filename with the wrong track's changes.
    tags = {
        "1-02 Second.flac": _tagset("Old Second", tracknumber="2", discnumber="1"),
        "1-01 First.flac": _tagset("Old First", tracknumber="1", discnumber="1"),
        "2-01 Third.flac": _tagset("Old Third", tracknumber="1", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New Third", "1", "2"),
            },
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["1-01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["1-02 Second.flac"]
    assert Change("title", "Old Third", "New Third") in changes["2-01 Third.flac"]


def test_create_track_changes_keeps_file_order_when_discnumber_tags_are_missing():
    # A CD1/CD2 folder layout with no DISCNUMBER tag on any file: every file's disc
    # defaults to 1, so (disc, track) pairs collide across discs (track 1 of CD1 and
    # track 1 of CD2 both key as (1, 1)). Tags can't identify these files, so the
    # existing file order (already correct, as get_audio_files gives it) must be kept
    # instead of being reshuffled by an untrustworthy sort.
    tags = {
        "CD1/01.flac": _tagset("Old CD1 1", tracknumber="1", discnumber=None),
        "CD1/02.flac": _tagset("Old CD1 2", tracknumber="2", discnumber=None),
        "CD2/01.flac": _tagset("Old CD2 1", tracknumber="1", discnumber=None),
        "CD2/02.flac": _tagset("Old CD2 2", tracknumber="2", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New CD1 1", "1", "1"),
                "2": _trackmeta("New CD1 2", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New CD2 1", "1", "2"),
                "2": _trackmeta("New CD2 2", "2", "2"),
            },
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old CD1 1", "New CD1 1") in changes["CD1/01.flac"]
    assert Change("title", "Old CD1 2", "New CD1 2") in changes["CD1/02.flac"]
    assert Change("title", "Old CD2 1", "New CD2 1") in changes["CD2/01.flac"]
    assert Change("title", "Old CD2 2", "New CD2 2") in changes["CD2/02.flac"]


def test_get_tag_number_reads_the_number_part_of_a_slash_pair():
    assert _get_tag_number(SimpleNamespace(discnumber="3/12"), "discnumber") == 3


def test_get_tag_number_defaults_missing_tags_to_one():
    assert _get_tag_number(SimpleNamespace(discnumber=None), "discnumber") == 1
    assert _get_tag_number({}, "discnumber") == 1


def test_get_tag_number_unwraps_a_list_value():
    assert _get_tag_number({"tracknumber": ["7"]}, "tracknumber") == 7
