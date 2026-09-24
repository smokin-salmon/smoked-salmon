from types import SimpleNamespace

import msgspec
import pytest

from salmon import cfg
from salmon.tagger.retagger import Change, _get_tag_number, create_track_changes, rename_files


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
