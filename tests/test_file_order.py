"""One order wherever an album's files get a number or a place (#940): natural, each folder and then the name.

Real functions on real FLAC files, sox faked where salmon calls it. Whole uploads run on the local fake tracker, with
retagging, renames and the tag check stubbed, as tests/test_uploader_dry_run.py does: the files keep their names.
"""

import os
import re
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from helpers.uploader_dry_run import FakeTracker, _run_up, _write_flac

import salmon.commands as commands
import salmon.images
import salmon.tagger.audio_info as audio_info_module
import salmon.tagger.pre_data as pre_data
import salmon.tagger.tags as tags_module
import salmon.uploader as uploader
from salmon import cfg, interaction
from salmon.common.files import get_audio_files
from salmon.converter import conversions, downconverting, transcoding
from salmon.images.base import BaseImageUploader
from salmon.prepared import Prepared
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.pre_data import create_track_list
from salmon.tagger.tags import gather_tags
from salmon.trackers import base
from salmon.uploader import preparation, spectrals
from salmon.uploader.upload import concat_track_data, generate_description, generate_t_description

FLAT = ["1.flac", "2.flac", "10.flac"]
DASHED = ["1-1.flac", "1-2.flac", "1-10.flac"]
DISC_ONE = ["Disc 1/1.flac", "Disc 1/2.flac", "Disc 1/10.flac", "Disc 2/1.flac"]
CDS = ["CD1/01.flac", "CD2/01.flac", "CD10/01.flac"]
BONUS = ["CD1/01.flac", "CD1 Bonus/01.flac", "CD2/01.flac"]

# What `salmon specs` numbers in another order than `up --spectrals` on the code before this.
MOVING = [FLAT, DASHED, CDS, BONUS]


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture(autouse=True)
def fake_sox(monkeypatch: pytest.MonkeyPatch) -> None:
    """sox, where salmon calls it: writes the images asked for."""
    run_process = spectrals.anyio.run_process

    async def run(args: list[str], **kwargs: Any) -> Any:
        if args[0] != "sox":
            return await run_process(args, **kwargs)
        for i, arg in enumerate(args):
            if arg == "-o":
                Path(args[i + 1]).write_bytes(b"\x89PNG spectral")
        return None

    monkeypatch.setattr(spectrals.anyio, "run_process", run)
    monkeypatch.setattr(cfg.upload.compression, "compress_spectrals", False)
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)


def _album(root: Path, names: Sequence[str], **tags: str) -> str:
    """A folder of real FLACs, each titled by its name, with these tags; its path as a string."""
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        _write_flac(file, title=name, artist="A", album="Album", date="2020", **tags)
    return str(root)


def _reverse_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = os.walk

    def reversed_walk(top: str, **kwargs: Any):
        for root, folders, files in sorted(walk(top, **kwargs), reverse=True):
            yield root, sorted(folders, reverse=True), sorted(files, reverse=True)

    monkeypatch.setattr(os, "walk", reversed_walk)


def _folder(tmp_path: Path, name: str) -> str:
    folder = tmp_path / name
    folder.mkdir()
    return str(folder)


async def _in_background(path: str, info: dict[str, Any], ids: tuple[int, ...]) -> dict[int, str]:
    async with spectrals.spectrals_in_background(path, info, ids, "FLAC") as generation:
        assert generation is not None
        return (await generation.result())[1]


async def _by_url(path: str, info: dict[str, Any], number: int) -> dict[int, str] | None:
    return (
        await uploader._spectrals_before_upload(
            SimpleNamespace(site_code="RED"),  # type: ignore[arg-type]
            path,
            info,
            {"format": "FLAC"},
            False,
            (),
            None,
            spectrals_after=False,
            given_spectral_urls={number: ["https://i.example/x.png"]},
            flac_group=None,
            reachable=None,
        )
    )[0]


def _prepared_files(path: str, folder: str, ids: tuple[int, ...]) -> list[str]:
    """The files a queued web upload's preparation made spectrals of, for these IDs."""
    kept = Prepared(path, folder)
    anyio.run(preparation._prepare_spectrals_of_album, kept, ids)
    return [name for name in get_audio_files(path) if kept.get("spectral", path, os.path.join(path, name)) is not None]


def _no_viewer_and_every_track(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(*_: Any, **__: Any) -> None:
        return None

    async def every_track(ids: dict[int, str], *_: Any, **__: Any) -> dict[int, str]:
        return ids

    monkeypatch.setattr(interaction, "show_spectrals", nothing)
    monkeypatch.setattr(spectrals, "prompt_spectrals", every_track)


def _printed_by_specs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: str) -> dict[str, str]:
    """`salmon specs`, its image host faked: the URL it prints for each file, holding the spectral's number."""

    async def urls(_path: str, ids: dict[int, str], **_: Any) -> dict[int, list[str]]:
        assert ids
        return {number: [f"URL{number}"] for number in ids}

    _no_viewer_and_every_track(monkeypatch)
    monkeypatch.setattr(commands, "handle_spectrals_upload_and_deletion", urls)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    capsys.readouterr()
    assert commands.specs.callback is not None
    anyio.run(partial(commands.specs.callback, path, False, False))
    return dict(re.findall(r"^(.+\.flac): (URL\d+)$", capsys.readouterr().out, re.MULTILINE))


# --- A spectral's number ----------------------------------------------------------------------


@pytest.mark.parametrize("names", MOVING, ids=lambda names: names[-1])
def test_a_spectral_number_means_one_file_everywhere(tmp_path, monkeypatch, names) -> None:
    path = _album(tmp_path / "album", names)
    info = gather_audio_info(path)

    specs_numbers = anyio.run(spectrals.generate_spectrals_all, path, _folder(tmp_path, "all"), info)
    assert specs_numbers == dict(enumerate(names, 1))

    for number, name in specs_numbers.items():
        given = (number,)
        assert spectrals.get_wanted_filenames(list(info), given) == {name}
        made = anyio.run(spectrals.generate_spectrals_ids, path, given, _folder(tmp_path, f"ids{number}"), info)
        assert list(made.values()) == [name]
        assert list(anyio.run(_in_background, path, info, given).values()) == [name]
        assert anyio.run(_by_url, path, info, number) == {number: name}
        assert _prepared_files(path, _folder(tmp_path, f"prepared{number}"), given) == [name]
        printed = commands.format_spectral_urls(list(info), {number: ["URL"]}, False)
        assert printed == f"{name}: URL"
        monkeypatch.setattr(cfg.image, "default_spectral_ids", str(number))
        assert spectrals.default_spectral_ids(specs_numbers) == [number]


def test_two_spectral_ids_keep_track_order(tmp_path) -> None:
    path = _album(tmp_path / "album", FLAT)
    info = gather_audio_info(path)

    made = anyio.run(spectrals.generate_spectrals_ids, path, (2, 3), _folder(tmp_path, "s"), info)

    assert made == {1: "2.flac", 2: "10.flac"}


def test_two_spectral_ids_keep_track_order_in_the_background(tmp_path) -> None:
    path = _album(tmp_path / "album", FLAT)

    made = anyio.run(_in_background, path, gather_audio_info(path), (2, 3))

    assert made == {1: "2.flac", 2: "10.flac"}


def test_specs_prints_each_files_own_url(tmp_path, monkeypatch, capsys) -> None:
    path = _album(tmp_path / "album", CDS)

    printed = _printed_by_specs(monkeypatch, capsys, path)

    assert printed == {"CD1/01.flac": "URL1", "CD2/01.flac": "URL2", "CD10/01.flac": "URL3"}


@pytest.mark.parametrize(
    "names",
    [["01 a.flac", "01 b.flac"], ["1-01 One.flac", "1-02 Two.flac", "1-03 Three.flac"]],
    ids=lambda names: names[0],
)
def test_a_tie_keeps_specs_numbering_under_a_reversed_walk(tmp_path, monkeypatch, capsys, names) -> None:
    path = _album(tmp_path / "album", names)
    _reverse_walk(monkeypatch)

    printed = _printed_by_specs(monkeypatch, capsys, path)

    assert printed == {name: f"URL{number}" for number, name in enumerate(names, 1)}


def test_every_track_in_the_background_is_numbered_as_specs_does(tmp_path) -> None:
    path = _album(tmp_path / "album", CDS)

    assert anyio.run(_in_background, path, gather_audio_info(path), ()) == dict(enumerate(CDS, 1))


def test_a_preparation_of_every_track_numbers_as_specs_does(tmp_path) -> None:
    path = _album(tmp_path / "album", CDS)
    kept = Prepared(path, _folder(tmp_path, "prepared"))

    anyio.run(preparation._prepare_spectrals_of_album, kept, ())

    made = {}
    for name in CDS:
        images = kept.get("spectral", path, os.path.join(path, name))
        assert images is not None
        made[name] = os.path.basename(images[0])
    assert made == {
        "CD1/01.flac": "01 Full.png",
        "CD2/01.flac": "02 Full.png",
        "CD10/01.flac": "03 Full.png",
    }


def test_a_preparation_for_two_numbers_makes_their_files(tmp_path) -> None:
    path = _album(tmp_path / "album", FLAT)

    assert _prepared_files(path, _folder(tmp_path, "prepared"), (2, 3)) == ["2.flac", "10.flac"]


# --- A place in the album ---------------------------------------------------------------------


@pytest.mark.parametrize("names", [FLAT, DASHED, DISC_ONE, CDS, BONUS], ids=lambda names: names[-1])
def test_tags_and_audio_info_come_in_track_order(tmp_path, monkeypatch, names) -> None:
    path = _album(tmp_path / "album", names)
    _reverse_walk(monkeypatch)

    assert list(gather_tags(path)) == names
    assert list(gather_audio_info(path)) == names


def _lines(description: str) -> list[str]:
    """A tracklist's titles, in the order listed."""
    return re.findall(r"\[/b\] (.+?\.flac) \[i\]", description)


METADATA: dict[str, Any] = {
    "artists": [("A", "main")],
    "urls": [],
    "date": None,
    "comment": None,
    "title": "Album",
    "rls_type": "Album",
}


@pytest.mark.parametrize("names", [FLAT, CDS, BONUS], ids=lambda names: names[-1])
def test_the_group_description_lists_tracks_in_order(tmp_path, names) -> None:
    path = _album(tmp_path / "album", names, tracknumber="1")
    track_data = concat_track_data(gather_tags(path), gather_audio_info(path))

    assert _lines(generate_description(track_data, METADATA)) == names


def test_the_torrent_description_lists_files_in_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.description, "include_tracklist_in_t_desc", True)
    path = _album(tmp_path / "album", FLAT, tracknumber="1")
    track_data = concat_track_data(gather_tags(path), gather_audio_info(path))

    text = generate_t_description(METADATA, track_data, False, [], None, None, None, None)

    assert re.findall(r"^(\d+) \[i\]", text, re.MULTILINE) == ["1", "2", "10"]


@pytest.mark.parametrize(
    "names",
    [
        DISC_ONE,
        DASHED,
        ["CD1.flac", "CD2.flac", "CD10.flac"],
        ["Track 1.flac", "Track 2.flac", "Track 10.flac"],
    ],
    ids=lambda names: names[-1],
)
def test_position_gives_track_numbers_in_order(tmp_path, monkeypatch, names) -> None:
    path = _album(tmp_path / "album", names)
    _reverse_walk(monkeypatch)

    tracks = create_track_list(gather_tags(path), False)

    assert [tracks["1"][str(place)]["title"] for place in range(1, len(names) + 1)] == names


def test_create_track_list_sorts_whatever_it_is_given() -> None:
    tags = {
        name: SimpleNamespace(
            artist=["A"],
            title=name,
            tracknumber=None,
            discnumber=None,
            tracktotal=None,
            disctotal=None,
            replay_gain=None,
            peak=None,
            isrc=None,
        )
        for name in ("CD10.flac", "CD2.flac", "CD1.flac")
    }

    tracks = create_track_list(tags, False)

    assert [tracks["1"][place]["title"] for place in "123"] == ["CD1.flac", "CD2.flac", "CD10.flac"]


def test_transcode_items_in_track_order(tmp_path, monkeypatch) -> None:
    names = ["CD1/02.flac", "CD1 Bonus/01.flac", "CD2/01.flac", "CD10/01.flac"]
    path = _album(tmp_path / "album", names)
    # What the disk gives, last first, so the order can't come from it.
    rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: sorted(rglob(self, pattern), reverse=True))

    items = transcoding._collect_transcode_items(path, str(tmp_path / "out"))

    assert [os.path.relpath(item.src, path) for item in items] == names


def test_as_before_a_conversion_record_stays_in_text_order(tmp_path, monkeypatch) -> None:
    path = _album(tmp_path / "Album [WEB 24bit FLAC]", FLAT)
    out = tmp_path / "Album [WEB FLAC]"
    items = [SimpleNamespace(src=os.path.join(path, name), dst=str(out / name), target_rate=44100) for name in FLAT]

    async def no_conversion(*_: Any) -> None:
        return None

    monkeypatch.setattr(downconverting, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(downconverting, "_build_output_path", lambda *_: str(out))
    monkeypatch.setattr(downconverting, "_collect_convert_items", lambda *_: items)
    monkeypatch.setattr(downconverting, "_copy_extra_files", lambda *_, **__: None)
    monkeypatch.setattr(downconverting, "_convert_audio_files", no_conversion)

    anyio.run(downconverting.convert_folder, path, 16, 44100)

    record = conversions.conversion_of(str(out))
    assert record is not None
    in_text_order = ["1.flac", "10.flac", "2.flac"]
    assert record["album_files"] == in_text_order
    assert list(record["files"]) == in_text_order
    assert list(record["source_fingerprints"]) == in_text_order
    assert all(record["source_fingerprints"].values())


# --- A whole `salmon up` on the fake tracker ----------------------------------------------------


def _up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[dict[str, str]], *args: str) -> FakeTracker:
    """`salmon up -n -yyy`: real file lists, tags and spectrals; the fake tracker over real HTTP."""
    album = tmp_path / "in" / "Album"
    for file in files:
        tags = {key: value for key, value in file.items() if key != "name"}
        (album / file["name"]).parent.mkdir(parents=True, exist_ok=True)
        _write_flac(album / file["name"], date="2020", **tags)
    (album / "cover.jpg").write_bytes(b"jpeg")
    for name in ("torrents", "downloads", "library"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path / "downloads"))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(tmp_path / "library")])

    class ImageUploader(BaseImageUploader):
        async def upload_file(self, filename: str) -> tuple[str, None]:
            return f"https://images.test/{os.path.basename(filename).replace(' ', '_')}", None

    monkeypatch.setitem(salmon.images.HOSTS, "testhost", SimpleNamespace(ImageUploader=ImageUploader))
    monkeypatch.setattr(cfg.image, "cover_uploader", "testhost")
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(interaction, "show_spectrals", nothing)

    async def get_metadata(*given: Any, **_: Any) -> Any:
        found = next(a for a in given if isinstance(a, dict) and "artists" in a)
        return {**found, **METADATA_OF_A_SOURCE, "source": "WEB"}, None

    async def review(*given: Any, **_: Any) -> dict[str, Any]:
        return next(a for a in given if isinstance(a, dict) and "label" in a)

    run = _run_up(
        monkeypatch,
        album,
        tmp_path / "torrents",
        ("--not-lossy", *args),
        "\n" * 30,
        multi_tracker_upload=False,
        trackers=("RED",),
        tracker=FakeTracker(group_torrents=[]),
        gather_audio_info=audio_info_module.gather_audio_info,
        gather_tags=tags_module.gather_tags,
        concat_track_data=concat_track_data,
        check_spectrals=spectrals.check_spectrals,
        construct_rls_data=pre_data.construct_rls_data,
        get_metadata=get_metadata,
        review_metadata_with_ai=review,
    )
    assert run.result.exit_code == 0, run.result.output
    return run.tracker


METADATA_OF_A_SOURCE = {
    "year": 2020,
    "group_year": 2020,
    "date": "2020-01-01",
    "label": "Label",
    "catno": "CAT1",
    "rls_type": "Album",
    "edition_title": None,
    "encoding_vbr": False,
    "scene": False,
    "cover": None,
    "comment": None,
    "urls": [],
    "genres": ["Electronic"],
}


def _form(tracker: FakeTracker, field: str) -> list[str]:
    """Each value of a field of the upload's form."""
    return [
        value
        for sent in tracker.sent
        if sent.method == "POST"
        for name, value in sent.fields
        if name == field and isinstance(value, str)
    ]


def _files(names: list[str], **tags: str) -> list[dict[str, str]]:
    return [{"name": name, "title": name, "artist": "A", "album": "Album", **tags} for name in names]


def _numbered(names: list[str]) -> list[dict[str, str]]:
    return [{**file, "tracknumber": str(place)} for place, file in enumerate(_files(names), 1)]


SPECTRAL_URL = "2=https://files.catbox.moe/full.png,https://files.catbox.moe/zoom.png"


@pytest.mark.parametrize(
    ("names", "heading"),
    [(FLAT, "2.flac"), (BONUS, "CD1 Bonus/01.flac")],
    ids=lambda given: given if isinstance(given, str) else "",
)
@pytest.mark.parametrize("args", [("-sp", "2"), ("--spectral-url", SPECTRAL_URL)], ids=["sp", "url"])
def test_the_description_names_the_track_asked_for(tmp_path, monkeypatch, names, heading, args) -> None:
    tracker = _up(tmp_path, monkeypatch, _numbered(names), *args)

    assert re.findall(r"\[b\](.+?\.flac) Full\[/b\]", _form(tracker, "release_desc")[0]) == [heading]


def test_a_whole_upload_lists_the_tracks_in_order_without_a_rename(tmp_path, monkeypatch) -> None:
    tracker = _up(tmp_path, monkeypatch, _numbered(FLAT), "-sp", "2")

    titles = re.findall(r"\[/b\] (.+?\.flac) \[i\]", _form(tracker, "album_desc")[0])
    assert titles == FLAT


def test_a_whole_upload_takes_album_tags_and_artists_from_the_first_track(tmp_path, monkeypatch) -> None:
    files = [
        {"name": "CD2/01.flac", "title": "One", "artist": "First", "album": "Album 2", "tracknumber": "1"},
        {"name": "CD10/01.flac", "title": "Two", "artist": "Second", "album": "Album 10", "tracknumber": "1"},
    ]

    tracker = _up(tmp_path, monkeypatch, files, "-sp", "1")

    assert _form(tracker, "title") == ["Album 2"]
    assert _form(tracker, "artists[]") == ["First", "Second"]
