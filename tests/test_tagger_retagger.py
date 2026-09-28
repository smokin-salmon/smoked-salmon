from types import SimpleNamespace

import msgspec
import pytest

from salmon import cfg
from salmon.errors import AmbiguousTrackOrderError
from salmon.tagger.retagger import Change, _get_tag_number, create_track_changes, rename_files, tag_files


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


def test_create_track_changes_falls_back_when_only_some_files_carry_a_discnumber_tag():
    # CD1's file has a (nonstandard) DISCNUMBER/TRACKNUMBER pair that reads as a high track
    # number; CD2's file has no DISCNUMBER tag at all, so it defaults to disc 1 with its own low
    # track number. Neither pair collides, so a plain sort by (disc, track) would trust them: it
    # would put CD2's file first and CD1's file second, backwards from the actual CD1/CD2 layout.
    # A mix of files with and without a DISCNUMBER tag can't be trusted, so this must fall back to
    # pairing by folder instead of mispairing off that sort.
    tags = {
        "CD1/01.flac": _tagset("Old CD1", tracknumber="5", discnumber="1"),
        "CD2/01.flac": _tagset("Old CD2", tracknumber="1", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {"1": _trackmeta("New CD1", "1", "1")},
            "2": {"1": _trackmeta("New CD2", "1", "2")},
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old CD1", "New CD1") in changes["CD1/01.flac"]
    assert Change("title", "Old CD2", "New CD2") in changes["CD2/01.flac"]


def test_create_track_changes_trusts_continuous_track_numbers_with_no_discnumber_tag_anywhere():
    # A flat, single folder holding a two-disc release with no DISCNUMBER tag on any file, and
    # TRACKNUMBER counting straight through both discs (1..6, not restarting at each disc). Every
    # file defaults to disc 1, so the (disc, track) keys are unique on tracknumber alone and this
    # has always retagged correctly by a plain tag sort, positionally against the metadata's
    # flattened disc 1 then disc 2 track list. DISCNUMBER being absent on every file (not just
    # some) is exactly the case the disc-folder fallback is not needed for.
    tags = {f"{n:02d}.flac": _tagset(f"Old {n}", tracknumber=str(n), discnumber=None) for n in range(1, 7)}
    metadata = {
        "tracks": {
            "1": {str(t): _trackmeta(f"New 1-{t}", str(t), "1") for t in range(1, 4)},
            "2": {str(t): _trackmeta(f"New 2-{t}", str(t), "2") for t in range(1, 4)},
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old 4", "New 2-1") in changes["04.flac"]


def test_create_track_changes_falls_back_when_tag_pairs_do_not_match_the_metadata_discs():
    # Every file carries a distinct, parseable DISCNUMBER/TRACKNUMBER pair, so the tags look
    # trustworthy by uniqueness alone: (1, 1), (1, 2), (1, 3), (2, 1). But the metadata gives each
    # of two discs two tracks, so disc 1 has no track "3" and disc 2's second track has no file at
    # all. Trusting the unique-looking pairs would zip the file tagged (1, 3) onto disc 2's first
    # track. The tag pairs must match the metadata's real disc/track pairs, not just be unique
    # among themselves; the files aren't in a folder per disc either, so this must refuse.
    tags = {
        "a.flac": _tagset("Old a", tracknumber="1", discnumber="1"),
        "b.flac": _tagset("Old b", tracknumber="2", discnumber="1"),
        "c.flac": _tagset("Old c", tracknumber="3", discnumber="1"),
        "d.flac": _tagset("Old d", tracknumber="1", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {"1": _trackmeta("New 1-1", "1", "1"), "2": _trackmeta("New 1-2", "2", "1")},
            "2": {"1": _trackmeta("New 2-1", "1", "2"), "2": _trackmeta("New 2-2", "2", "2")},
        }
    }

    with pytest.raises(AmbiguousTrackOrderError, match="DISCNUMBER"):
        create_track_changes(tags, metadata)


def test_create_track_changes_orders_ten_plus_discs_naturally():
    # gather_tags lists CD10 before CD2 (a path with no leading number sorts as text). With no
    # DISCNUMBER tags every file collides on (1, 1), so the fallback must order the disc folders
    # naturally (CD2 before CD10), not lexically.
    tags = {f"CD{disc}/01.flac": _tagset(f"Old CD{disc}", tracknumber="1", discnumber=None) for disc in (1, 10, 2)}
    metadata = {"tracks": {str(disc): {"1": _trackmeta(f"New CD{disc}", "1", str(disc))} for disc in (1, 2, 10)}}

    changes = create_track_changes(tags, metadata)

    for disc in (1, 2, 10):
        assert Change("title", f"Old CD{disc}", f"New CD{disc}") in changes[f"CD{disc}/01.flac"]


def test_create_track_changes_refuses_a_flat_folder_without_disc_tags():
    # Track-first names in one flat folder (01-CD1, 01-CD2, 02-CD1, 02-CD2): no DISCNUMBER tags,
    # and the file order interleaves the two discs while the metadata goes disc by disc. Neither
    # the tags nor a folder per disc can sort this out, so retagging must refuse rather than
    # silently write CD2's titles onto CD1's files.
    tags = {
        name: _tagset(f"Old {name}", tracknumber=track, discnumber=None)
        for name, track in (
            ("01-CD1.flac", "1"),
            ("01-CD2.flac", "1"),
            ("02-CD1.flac", "2"),
            ("02-CD2.flac", "2"),
        )
    }
    metadata = {
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        }
    }

    with pytest.raises(AmbiguousTrackOrderError, match="DISCNUMBER"):
        create_track_changes(tags, metadata)


def test_tag_files_skips_retagging_a_flat_folder_without_disc_tags_instead_of_crashing(capsys):
    # Same flat, disc-encoded-in-the-name layout as above, but exercised through tag_files, the
    # entry point the uploader actually calls. It must print the refusal and return so the upload
    # continues with the files' current tags, not raise out and abort the whole run.
    tags = {
        name: _tagset(f"Old {name}", tracknumber=track, discnumber=None)
        for name, track in (
            ("01-CD1.flac", "1"),
            ("01-CD2.flac", "1"),
            ("02-CD1.flac", "2"),
            ("02-CD2.flac", "2"),
        )
    }
    metadata = {
        "title": "Some Album",
        "edition_title": None,
        "genres": [],
        "group_year": None,
        "label": None,
        "catno": None,
        "artists": [("Some Artist", "main")],
        "upc": None,
        "comment": None,
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        },
    }

    tag_files("/unused", tags, metadata, auto_rename=True)

    output = capsys.readouterr().out
    assert "DISCNUMBER" in output
    assert "Skipping retagging procedure" in output


def test_create_track_changes_refuses_disc_folders_whose_track_counts_differ_from_the_metadata():
    # Three files under CD1, one under CD2, against metadata that expects two tracks per disc.
    # The folder split cannot be trusted to line files up with the right disc's tracks.
    tags = {
        "CD1/01.flac": _tagset("a", tracknumber="1", discnumber=None),
        "CD1/02.flac": _tagset("b", tracknumber="2", discnumber=None),
        "CD1/03.flac": _tagset("c", tracknumber="3", discnumber=None),
        "CD2/01.flac": _tagset("d", tracknumber="1", discnumber=None),
    }
    metadata = {
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        }
    }

    with pytest.raises(AmbiguousTrackOrderError, match="DISCNUMBER"):
        create_track_changes(tags, metadata)


def test_create_track_changes_orders_a_single_disc_single_folder_with_duplicate_track_tags_by_file_name():
    # A single-disc release, one folder, every file tagged TRACKNUMBER=1 (the bad tags that are
    # exactly why someone would retag). On master this falls back to file order and retags; the
    # disc-folder fallback must keep doing that instead of refusing just because a single disc
    # can't resolve any other way.
    tags = {
        "01 First.flac": _tagset("Old First", tracknumber="1", discnumber=None),
        "02 Second.flac": _tagset("Old Second", tracknumber="1", discnumber=None),
        "03 Third.flac": _tagset("Old Third", tracknumber="1", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
                "3": _trackmeta("New Third", "3", "1"),
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["02 Second.flac"]
    assert Change("title", "Old Third", "New Third") in changes["03 Third.flac"]


def test_create_track_changes_orders_a_single_disc_single_folder_with_no_track_tags_by_file_name():
    # No file in the folder carries a TRACKNUMBER tag at all: still not ambiguous when the file
    # names are, so this must retag by file name order rather than refuse.
    tags = {
        "01 First.flac": _tagset("Old First", tracknumber=None, discnumber=None),
        "02 Second.flac": _tagset("Old Second", tracknumber=None, discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["02 Second.flac"]


def test_create_track_changes_orders_by_file_name_when_a_name_holds_a_digit_int_cannot_parse():
    # Same no-track-tag, single-disc, single-folder fallback as above, but one file's name has a
    # superscript "2" next to ordinary digits ("01²2.flac"): str.isdigit() accepts it as a
    # split segment on its own, and int() then rejects it. The natural-order file-name fallback
    # must not raise out of retagging over a file name like that.
    tags = {
        "01²2.flac": _tagset("Old First", tracknumber=None, discnumber=None),
        "02.flac": _tagset("Old Second", tracknumber=None, discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["01²2.flac"]
    assert Change("title", "Old Second", "New Second") in changes["02.flac"]


def test_tag_files_skips_retagging_when_one_disc_folder_is_genuinely_ambiguous(capsys):
    # A two-disc release with a proper folder per disc: CD1's files have no track tags and tie
    # under natural file-name order too (same name, different case), so CD1 alone can't be
    # resolved. tag_files must print the refusal and return, leaving the upload to continue with
    # the files' current tags, rather than raising out of tag_files and aborting the run.
    tags = {
        "CD1/Track.flac": _tagset("Old A", tracknumber=None, discnumber=None),
        "CD1/track.flac": _tagset("Old B", tracknumber=None, discnumber=None),
        "CD2/01.flac": _tagset("Old CD2 1", tracknumber="1", discnumber=None),
    }
    metadata = {
        "title": "Some Album",
        "edition_title": None,
        "genres": [],
        "group_year": None,
        "label": None,
        "catno": None,
        "artists": [("Some Artist", "main")],
        "upc": None,
        "comment": None,
        "tracks": {
            "1": {
                "1": _trackmeta("New CD1 1", "1", "1"),
                "2": _trackmeta("New CD1 2", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New CD2 1", "1", "2"),
            },
        },
    }

    tag_files("/unused", tags, metadata, auto_rename=True)

    output = capsys.readouterr().out
    assert "DISCNUMBER" in output
    assert "Skipping retagging procedure" in output


def test_create_track_changes_handles_the_one_folder_disc_dot_track_layout():
    # #479's one-folder layout keeps a multi-disc release flat, files named "<disc>.<track> ...".
    # The tags already carry real DISCNUMBER/TRACKNUMBER values by that point, so this stays on
    # the tag-sorted path rather than tripping the folder-collision fallback.
    tags = {
        "2.01 Third.flac": _tagset("Old Third", tracknumber="1", discnumber="2"),
        "1.01 First.flac": _tagset("Old First", tracknumber="1", discnumber="1"),
        "1.02 Second.flac": _tagset("Old Second", tracknumber="2", discnumber="1"),
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

    assert Change("title", "Old First", "New First") in changes["1.01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["1.02 Second.flac"]
    assert Change("title", "Old Third", "New Third") in changes["2.01 Third.flac"]


def test_get_tag_number_reads_the_number_part_of_a_slash_pair():
    assert _get_tag_number(SimpleNamespace(discnumber="3/12"), "discnumber") == 3


def test_get_tag_number_defaults_missing_tags_to_one():
    assert _get_tag_number(SimpleNamespace(discnumber=None), "discnumber") == 1
    assert _get_tag_number({}, "discnumber") == 1


def test_get_tag_number_unwraps_a_list_value():
    assert _get_tag_number({"tracknumber": ["7"]}, "tracknumber") == 7


def test_get_tag_number_defaults_a_digit_like_value_int_cannot_parse():
    # "²" (superscript two) passes str.isdigit() but int() rejects it; a malformed
    # TRACKNUMBER like this must read as unparseable rather than raise out of retagging.
    assert _get_tag_number({"tracknumber": ["²"]}, "tracknumber") == 1


def _formatting(**settings):
    """The formatting config with fixed file templates, plus ``settings``, built as a config file would be."""
    fields = msgspec.structs.asdict(cfg.upload.formatting)
    fields.pop("split_multi_disc_into_folders", None)
    fields.update(
        file_template="{tracknumber}. {artist} - {title}",
        one_album_artist_file_template="{tracknumber}. {title}",
        no_artist_in_filename_if_only_one_album_artist=True,
    )
    fields.update(settings)
    return msgspec.convert(fields, type(cfg.upload.formatting))


def _single_folder(monkeypatch, **settings):
    monkeypatch.setattr(cfg.upload, "formatting", _formatting(split_multi_disc_into_folders=False, **settings))


def _release(root, tracks, others=()):
    """Write a release's files, each holding its own path, and return the tags and metadata of its tracks.

    ``tracks`` maps a track's path to its (disc, track) numbers; a disc of None leaves the tag out.
    """
    tags = {}
    discs = {}
    for name, (disc, track) in tracks.items():
        tags[name] = SimpleNamespace(
            artist=["Some Artist"],
            title=f"Title {disc}-{track}",
            tracknumber=str(track),
            discnumber=None if disc is None else str(disc),
        )
        discs.setdefault(str(disc or 1), {})[str(track)] = {"artists": [("Some Artist", "main")]}
    for name in [*tracks, *others]:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(name)
    return tags, {"tracks": discs}


def _tree(root):
    """Every file and folder under ``root``, each file with the path it was written at."""
    return sorted(
        (path.relative_to(root).as_posix(), None if path.is_dir() else path.read_text()) for path in root.rglob("*")
    )


def _contents(root):
    return sorted(text for _, text in _tree(root) if text is not None)


def _two_discs(first=2, second=1, folder="Disc {}"):
    tracks = {f"{folder.format(1)}/a{n:03d}.flac": (1, n) for n in range(1, first + 1)}
    tracks.update({f"{folder.format(2)}/b{n:03d}.flac": (2, n) for n in range(1, second + 1)})
    return tracks


# Each case: the tracks, the other files, the source, and the names master gives them.
DEFAULT_LAYOUT_CASES = {
    "single-disc": (
        {"a.flac": (1, 1), "b.flac": (1, 2)},
        ["rip.log"],
        "CD",
        ["01. Title 1-1.flac", "02. Title 1-2.flac", "rip.log"],
    ),
    "multi-disc-cd": (
        _two_discs(),
        ["Disc 1/rip.log", "Disc 1/Scans/front.jpg", "Disc 2/rip.log", "cover.jpg"],
        "CD",
        [
            "CD01",
            "CD01/01. Title 1-1.flac",
            "CD01/02. Title 1-2.flac",
            "CD01/Scans",
            "CD01/Scans/front.jpg",
            "CD01/rip.log",
            "CD02",
            "CD02/01. Title 2-1.flac",
            "CD02/rip.log",
            "cover.jpg",
        ],
    ),
    "multi-disc-vinyl": (
        _two_discs(),
        [],
        "Vinyl",
        ["LP01", "LP01/01. Title 1-1.flac", "LP01/02. Title 1-2.flac", "LP02", "LP02/01. Title 2-1.flac"],
    ),
    "multi-disc-web": (
        _two_discs(),
        [],
        "WEB",
        ["Part01", "Part01/01. Title 1-1.flac", "Part01/02. Title 1-2.flac", "Part02", "Part02/01. Title 2-1.flac"],
    ),
    "single-disc-100-tracks": (
        {f"{n:03d}.flac": (1, n) for n in range(1, 102)},
        [],
        "CD",
        sorted(f"{n:02d}. Title 1-{n}.flac" for n in range(1, 102)),
    ),
    "multi-disc-100-tracks": (
        _two_discs(first=101, second=2),
        [],
        "CD",
        sorted(
            ["CD01", "CD02", "CD02/01. Title 2-1.flac", "CD02/02. Title 2-2.flac"]
            + [f"CD01/{n:02d}. Title 1-{n}.flac" for n in range(1, 102)]
        ),
    ),
}


@pytest.mark.parametrize("setting", [{}, {"split_multi_disc_into_folders": True}], ids=["absent", "true"])
@pytest.mark.parametrize("case", DEFAULT_LAYOUT_CASES)
def test_rename_files_names_are_unchanged_by_default(tmp_path, monkeypatch, case, setting) -> None:
    tracks, others, source, expected = DEFAULT_LAYOUT_CASES[case]
    monkeypatch.setattr(cfg.upload, "formatting", _formatting(**setting))
    tags, metadata = _release(tmp_path, tracks, others)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source=source)

    assert [name for name, _ in _tree(tmp_path)] == expected


@pytest.mark.parametrize("case", ["single-disc", "single-disc-100-tracks"])
def test_rename_files_single_folder_setting_leaves_single_disc_releases_alone(tmp_path, monkeypatch, case) -> None:
    tracks, others, source, expected = DEFAULT_LAYOUT_CASES[case]
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, tracks, others)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source=source)

    assert [name for name, _ in _tree(tmp_path)] == expected


def test_rename_files_can_keep_a_multi_disc_release_in_one_folder(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, _two_discs())

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert _tree(tmp_path) == [
        ("1.01. Title 1-1.flac", "Disc 1/a001.flac"),
        ("1.02. Title 1-2.flac", "Disc 1/a002.flac"),
        ("2.01. Title 2-1.flac", "Disc 2/b001.flac"),
    ]


def test_rename_files_single_folder_pads_numbers_to_the_largest(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, {"a.flac": (1, 1), "b.flac": (1, 100), "c.flac": (10, 1)})

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert [name for name, _ in _tree(tmp_path)] == [
        "01.001. Title 1-1.flac",
        "01.100. Title 1-100.flac",
        "10.001. Title 10-1.flac",
    ]


def test_rename_files_single_folder_names_each_disc_folders_files_for_its_disc(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    others = [
        f"Disc {disc}/{name}"
        for disc in (1, 2)
        for name in ("rip.log", "rip.cue", "cover.jpg", "folder.jpg", "Scans/front.jpg", "Scans/back.jpg")
    ]
    tags, metadata = _release(tmp_path, _two_discs(), [*others, "cover.jpg"])
    before = _contents(tmp_path)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert _contents(tmp_path) == before
    assert _tree(tmp_path) == [
        ("1.01. Title 1-1.flac", "Disc 1/a001.flac"),
        ("1.02. Title 1-2.flac", "Disc 1/a002.flac"),
        ("2.01. Title 2-1.flac", "Disc 2/b001.flac"),
        ("Scans.1", None),
        ("Scans.1/back.jpg", "Disc 1/Scans/back.jpg"),
        ("Scans.1/front.jpg", "Disc 1/Scans/front.jpg"),
        ("Scans.2", None),
        ("Scans.2/back.jpg", "Disc 2/Scans/back.jpg"),
        ("Scans.2/front.jpg", "Disc 2/Scans/front.jpg"),
        ("cover.1.jpg", "Disc 1/cover.jpg"),
        ("cover.2.jpg", "Disc 2/cover.jpg"),
        ("cover.jpg", "cover.jpg"),
        ("folder.1.jpg", "Disc 1/folder.jpg"),
        ("folder.2.jpg", "Disc 2/folder.jpg"),
        ("rip.1.cue", "Disc 1/rip.cue"),
        ("rip.1.log", "Disc 1/rip.log"),
        ("rip.2.cue", "Disc 2/rip.cue"),
        ("rip.2.log", "Disc 2/rip.log"),
    ]


def test_rename_files_single_folder_leaves_a_file_whose_new_name_is_taken(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, _two_discs(), ["Disc 1/cover.jpg", "Disc 2/cover.jpg", "cover.1.jpg"])
    before = _contents(tmp_path)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    tree = _tree(tmp_path)
    assert _contents(tmp_path) == before
    assert ("cover.1.jpg", "cover.1.jpg") in tree
    assert ("Disc 1/cover.jpg", "Disc 1/cover.jpg") in tree
    assert ("cover.2.jpg", "Disc 2/cover.jpg") in tree
    assert not (tmp_path / "Disc 2").exists()


def test_rename_files_single_folder_keeps_the_names_from_a_folder_of_several_discs(tmp_path, monkeypatch) -> None:
    # One folder holds both discs' tracks, so there is no one disc to name its other files for: they keep
    # their names, and one that would replace a file already in the release folder stays where it is.
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, _two_discs(folder="Audio"), ["Audio/rip.log", "Audio/cover.jpg", "cover.jpg"])
    before = _contents(tmp_path)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    tree = _tree(tmp_path)
    assert _contents(tmp_path) == before
    assert ("rip.log", "Audio/rip.log") in tree
    assert ("cover.jpg", "cover.jpg") in tree
    assert ("Audio/cover.jpg", "Audio/cover.jpg") in tree


@pytest.mark.parametrize(
    ("tracks", "template"),
    [
        pytest.param(_two_discs(first=1, second=1), "{title}", id="same-title-on-two-discs"),
        pytest.param({"CD1/01.flac": (None, 1), "CD2/01.flac": (None, 1)}, None, id="no-disc-numbers"),
    ],
)
def test_rename_files_single_folder_renames_nothing_when_two_tracks_get_one_name(
    tmp_path, monkeypatch, tracks, template
) -> None:
    templates = {"file_template": template, "one_album_artist_file_template": template} if template else {}
    _single_folder(monkeypatch, **templates)
    tags, metadata = _release(tmp_path, tracks, ["CD1/rip.log"])
    metadata["tracks"].setdefault("2", metadata["tracks"]["1"])
    for tagset in tags.values():
        tagset.title = "Intro"
    before = _tree(tmp_path)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert _tree(tmp_path) == before


def test_rename_files_single_folder_renames_nothing_over_an_existing_file(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, _two_discs(), ["2.01. Title 2-1.flac"])
    before = _tree(tmp_path)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert _tree(tmp_path) == before


def test_rename_files_single_folder_updates_the_spectral_file_names(tmp_path, monkeypatch) -> None:
    _single_folder(monkeypatch)
    tags, metadata = _release(tmp_path, _two_discs())
    spectral_ids = {1: "Disc 1/a001.flac", 2: "Disc 1/a002.flac", 3: "Disc 2/b001.flac"}

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=spectral_ids, source="CD")

    assert spectral_ids == {1: "1.01. Title 1-1.flac", 2: "1.02. Title 1-2.flac", 3: "2.01. Title 2-1.flac"}


def test_rename_files_never_replaces_a_file_when_two_folders_go_to_one_disc_folder(tmp_path, monkeypatch) -> None:
    # Two folders of disc 1 tracks both go to CD01: the second folder's cover.jpg used to replace the first's.
    monkeypatch.setattr(cfg.upload, "formatting", _formatting())
    tracks = {"Disc 1/a.flac": (1, 1), "Disc 1 bonus/b.flac": (1, 2), "Disc 2/c.flac": (2, 1)}
    tags, metadata = _release(tmp_path, tracks, ["Disc 1/cover.jpg", "Disc 1 bonus/cover.jpg"])
    before = _contents(tmp_path)

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    tree = _tree(tmp_path)
    assert _contents(tmp_path) == before
    assert ("CD01/cover.jpg", "Disc 1/cover.jpg") in tree
    assert ("Disc 1 bonus/cover.jpg", "Disc 1 bonus/cover.jpg") in tree
