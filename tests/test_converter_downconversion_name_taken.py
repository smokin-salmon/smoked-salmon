"""A 16-bit conversion whose name is the album's own (#1089).

A 24-bit album named "[WEB FLAC]", renamed "[WEB 24bit FLAC]", has "[WEB FLAC]" for its 16-bit name. The rename
copies the album, so that name still holds it: the source itself in download_directory, or a dry run's copy of it in
the run directory. The conversion takes "[WEB 16bit FLAC]" instead.
"""

import copy
import hashlib
import shutil
from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers.uploader_downconversion import _write_flac
from helpers.uploader_dry_run import METADATA, RLS_DATA, FakeTracker, _run_up, _track
from mutagen.flac import FLAC
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see the tests that use it)
)

from salmon import cfg
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.errors import UploadError
from salmon.tagger.audio_info import gather_audio_info
from salmon.uploader.staging import STAGING_DIR

ORIGINAL = "Artist - Album (2020) [WEB FLAC]"
RENAMED_24 = "Artist - Album (2020) [WEB 24bit FLAC]"
SIXTEEN = "Artist - Album (2020) [WEB 16bit FLAC]"
NAMES = ("01 - one.flac", "02 - two.flac")


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured; runs make the 16-bit conversion only."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    for folder in (downloads, torrents):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.upload, "conversions", ["16bit"])
    monkeypatch.setattr(dc, "_convert_audio_files", _sox)
    return downloads, torrents


def _album_24(folder: Path, audio: bytes = b"album", rate: int = 48000) -> Path:
    """A 24-bit album of two tagged tracks and a cover; `audio` sets what the tracks' audio is."""
    for name in NAMES:
        _write_flac(folder / name, rate, 24, md5=hashlib.md5(audio + name.encode()).digest())
        tagged = FLAC(folder / name)
        tagged["title"] = name
        tagged.save()
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


async def _sox(items: list[dc.ConvertItem], bit_depth: dc.BitDepth) -> None:
    """Stands in for sox: writes each converted file at the target depth and rate."""
    for item in items:
        _write_flac(Path(item.dst), item.target_rate, bit_depth)


def _up_24(monkeypatch, album: Path, torrents: Path, *args: str):
    """`salmon up` of a 24-bit album, to a new group on RED, which the metadata renames RENAMED_24."""
    rls_data = {**copy.deepcopy(RLS_DATA), "encoding": "24bit Lossless"}
    metadata = {**copy.deepcopy(METADATA), "encoding": "24bit Lossless"}
    # Named as the tracks are once the stubbed rename_files has run.
    tracks = {"01. one.flac": _track(1, "One"), "02. two.flac": _track(2, "Two")}
    track_data = {name: {**track, "precision": 24, "sample rate": 48000} for name, track in tracks.items()}

    async def construct_rls_data(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return rls_data

    async def get_metadata(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], None]:
        return metadata, None

    return _run_up(
        monkeypatch,
        album,
        torrents,
        args=args,
        input="\n",
        multi_tracker_upload=False,
        renamed=RENAMED_24,
        construct_rls_data=construct_rls_data,
        get_metadata=get_metadata,
        concat_track_data=lambda *_a, **_k: track_data,
        gather_audio_info=gather_audio_info,
        # A group with no torrent yet: none holds the 16-bit FLAC already.
        tracker=FakeTracker(group_torrents=[]),
    )


def _tracks(folder: Path) -> set[tuple[int, int]]:
    """The bit depths and sample rates of folder's FLACs."""
    return {(info["precision"], info["sample rate"]) for info in gather_audio_info(str(folder)).values()}


def _uploaded_names(run) -> list[str]:
    """The folder name each upload of the run was made from, in order."""
    return [
        line.split(" (", 1)[1].removesuffix(").") for line in run.result.output.splitlines() if "uploaded http" in line
    ]


@pytest.mark.usefixtures("image_uploads")
def test_an_album_in_download_directory_gets_its_16bit_conversion_beside_it(monkeypatch, dirs) -> None:
    downloads, torrents = dirs
    album = _album_24(downloads / ORIGINAL)

    run = _up_24(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    assert "16bit 48.0 kHz conversion completed" in run.result.output, run.result.output
    assert _uploaded_names(run) == [RENAMED_24, SIXTEEN]
    assert _tracks(downloads / SIXTEEN) == {(16, 48000)}
    # The album it was named after is left as it was.
    assert _tracks(album) == {(24, 48000)}


@pytest.mark.usefixtures("image_uploads")
def test_an_album_elsewhere_keeps_its_16bit_conversion_name(monkeypatch, tmp_path, dirs) -> None:
    downloads, torrents = dirs
    album = _album_24(tmp_path / "seeding" / ORIGINAL)

    run = _up_24(monkeypatch, album, torrents)

    assert run.result.exit_code == 0, run.result.output
    # download_directory holds no folder of that name: nothing takes it.
    assert _uploaded_names(run) == [RENAMED_24, ORIGINAL]
    assert _tracks(downloads / ORIGINAL) == {(16, 48000)}


@pytest.mark.usefixtures("image_uploads")
@pytest.mark.parametrize("where", ["downloads", "seeding"])
def test_a_dry_run_converts_beside_its_copy_of_the_album(monkeypatch, tmp_path, dirs, where: str) -> None:
    downloads, torrents = dirs
    album = _album_24((downloads if where == "downloads" else tmp_path / "seeding") / ORIGINAL)

    run = _up_24(monkeypatch, album, torrents, "--dry-run")

    assert run.result.exit_code == 0, run.result.output
    assert "16bit 48.0 kHz conversion completed" in run.result.output, run.result.output
    assert "is incomplete or not salmon's conversion" not in run.result.output
    assert f"The torrent: {SIXTEEN}," in run.result.output
    # The run directory is gone, and nothing else was written to download_directory.
    assert not any((downloads / STAGING_DIR).iterdir())
    assert {path.name for path in downloads.iterdir()} == {STAGING_DIR, *([ORIGINAL] if where == "downloads" else [])}


def _source_beside(tmp_path: Path, *names: str, source_name: str = RENAMED_24, rate: int = 48000) -> Path:
    """A 24-bit album named source_name and a copy of it under each of names, all in tmp_path.

    Each copy's tracks have other names and tags, as the album's had before salmon renamed and retagged them.
    """
    source = _album_24(tmp_path / source_name, rate=rate)
    for name in names:
        copied = Path(shutil.copytree(source, tmp_path / name))
        for track in NAMES:
            tagged = FLAC(copied / track)
            tagged["title"] = "Before the retag"
            tagged.save()
            (copied / track).rename(copied / f"Track {track}")
    return source


@pytest.mark.usefixtures("dirs")
def test_a_conversion_beside_a_copy_of_the_album_takes_the_16bit_name(tmp_path) -> None:
    source = _source_beside(tmp_path, ORIGINAL)

    rate, path = anyio.run(dc.convert_folder, str(source), 16, None)

    assert (rate, path) == (48000, str(tmp_path / SIXTEEN))
    assert _tracks(tmp_path / SIXTEEN) == {(16, 48000)}
    assert _tracks(tmp_path / ORIGINAL) == {(24, 48000)}


@pytest.mark.usefixtures("dirs")
def test_a_second_run_finds_the_conversion_under_the_16bit_name(tmp_path, capsys) -> None:
    source = _source_beside(tmp_path, ORIGINAL)
    anyio.run(dc.convert_folder, str(source), 16, None)

    _rate, path = anyio.run(dc.convert_folder, str(source), 16, None)

    assert path == str(tmp_path / SIXTEEN)
    assert f"{tmp_path / SIXTEEN} already exists." in capsys.readouterr().out
    assert conversions.conversion_of(path) is not None


@pytest.mark.usefixtures("dirs")
def test_a_later_run_finds_the_conversion_once_the_album_has_gone_from_its_old_name(tmp_path, capsys) -> None:
    source = _source_beside(tmp_path, ORIGINAL)
    anyio.run(dc.convert_folder, str(source), 16, None)
    # remove_source_dir, or the user, removes the album the run started from.
    shutil.rmtree(tmp_path / ORIGINAL)

    _rate, path = anyio.run(dc.convert_folder, str(source), 16, None)

    assert path == str(tmp_path / SIXTEEN)
    assert f"{tmp_path / SIXTEEN} already exists." in capsys.readouterr().out
    assert not (tmp_path / ORIGINAL).exists()


@pytest.mark.usefixtures("dirs")
def test_a_resolution_name_says_the_16bit_token_it_would_leave_out(tmp_path) -> None:
    # "[{source} FLAC {resolution}]": 16/44.1 has no token, so its name is the album's, "[WEB FLAC]", before the rename.
    source = _source_beside(tmp_path, ORIGINAL, source_name="Artist - Album (2020) [WEB FLAC 24-44.1]", rate=44100)

    _rate, path = anyio.run(dc.convert_folder, str(source), 16, None)

    assert path == str(tmp_path / "Artist - Album (2020) [WEB FLAC 16-44.1]")
    assert _tracks(Path(path)) == {(16, 44100)}


@pytest.mark.usefixtures("dirs")
def test_another_album_under_the_name_is_still_refused(tmp_path) -> None:
    source = _album_24(tmp_path / RENAMED_24)
    _album_24(tmp_path / ORIGINAL, audio=b"another album")

    with pytest.raises(UploadError) as error:
        anyio.run(dc.convert_folder, str(source), 16, None)

    assert str(error.value) == (
        f"{tmp_path / ORIGINAL} is incomplete or not salmon's conversion of this album at these settings: "
        "remove it and run again."
    )
    assert not (tmp_path / SIXTEEN).exists()


@pytest.mark.usefixtures("dirs")
def test_a_name_the_album_holds_either_way_is_refused_as_the_album(tmp_path) -> None:
    source = _source_beside(tmp_path, ORIGINAL, SIXTEEN)

    with pytest.raises(UploadError) as error:
        anyio.run(dc.convert_folder, str(source), 16, None)

    assert str(error.value) == (
        f"{tmp_path / SIXTEEN} holds this album, or a copy of it, under the name its 16-bit conversion would take: "
        "move or rename that folder, or give the album another name, and run again."
    )
    assert _tracks(tmp_path / SIXTEEN) == {(24, 48000)}
