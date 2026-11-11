"""A source that cannot be transcoded is refused before the transcode writes or deletes anything."""

import os
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from helpers.uploader_shortened_conversions import _write_flac
from mutagen import flac

from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc
from salmon.errors import UploadError


def _streaminfo_only_flac() -> bytes:
    """A FLAC of 16bit/44.1kHz with no audio: the output path reads the source's resolution."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    return b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo


def _source(folder: Path) -> Path:
    folder.mkdir()
    (folder / "01.flac").write_bytes(_streaminfo_only_flac())
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _six_channels(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    """The source's one track as a 5.1 file; only info.channels is read before the refusal."""
    fake: Any = SimpleNamespace(info=SimpleNamespace(channels=6))
    monkeypatch.setattr(
        tc,
        "_collect_transcode_items",
        lambda _path, new_path: [
            tc.TranscodeItem(src=str(source / "01.flac"), dst=f"{new_path}/01.mp3", tags={}, flac_obj=fake)
        ],
    )


def test_a_multichannel_source_is_refused_before_a_prior_output_is_deleted(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path / "Artist - Album (2024) [FLAC]")
    existing = Path(tc._build_output_path(str(source), "320"))
    existing.mkdir()
    (existing / "01 - already here.mp3").write_bytes(b"prior output")
    _six_channels(monkeypatch, source)

    with pytest.raises(ValueError, match="6 channels"):
        anyio.run(lambda: tc.transcode_folder(str(source), "320"))

    assert (existing / "01 - already here.mp3").read_bytes() == b"prior output"


def test_a_multichannel_source_writes_no_output_folder(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path / "Artist - Other (2024) [FLAC]")
    _six_channels(monkeypatch, source)

    with pytest.raises(ValueError, match="6 channels"):
        anyio.run(lambda: tc.transcode_folder(str(source), "320"))

    # The extra files are copied before any track is encoded.
    assert not os.path.exists(tc._build_output_path(str(source), "320"))


@pytest.mark.parametrize(
    ("tags", "expected_track"),
    [
        ({"tracknumber": ["1"], "tracktotal": ["12"]}, ["1/12"]),
        ({"tracknumber": ["1"], "tracktotal": [""]}, ["1"]),
        ({"tracknumber": ["1"], "tracktotal": ["   "]}, ["1"]),
        ({"tracknumber": ["12"], "tracktotal": ["12/12"]}, ["12/12"]),
        ({"tracknumber": ["1"], "tracktotal": ["1/12"]}, ["1/12"]),
        ({"tracknumber": ["1"], "tracktotal": ["12"], "totaltracks": [""]}, ["1/12"]),
        ({"tracknumber": ["1"], "tracktotal": ["", "12"]}, ["1/12"]),
        ({"tracknumber": ["1/12"], "tracktotal": ["12"]}, ["1/12"]),
        ({"tracknumber": ["1/12"], "tracktotal": ["12/12"]}, ["1/12"]),
        ({"tracknumber": ["1"], "tracktotal": ["１２"]}, ["1/12"]),
    ],
)
def test_prepare_tags_merges_totals(tags: dict[str, list[str]], expected_track: list[str]) -> None:
    result = tc._prepare_tags(tags)
    assert result["tracknumber"] == expected_track
    assert "tracktotal" not in result
    assert "totaltracks" not in result


def test_prepare_tags_both_totals_empty_merges_nothing() -> None:
    tags = {"tracknumber": ["1"], "tracktotal": [""], "totaltracks": ["  "]}
    result = tc._prepare_tags(tags)
    assert result["tracknumber"] == ["1"]
    assert "tracktotal" not in result
    assert "totaltracks" not in result


def test_prepare_tags_disctotal_slash() -> None:
    tags = {"discnumber": ["1"], "disctotal": ["2/2"]}
    result = tc._prepare_tags(tags)
    assert result["discnumber"] == ["1/2"]
    assert "disctotal" not in result


def test_prepare_tags_discnumber_with_total_keeps_one_total() -> None:
    tags = {"discnumber": ["1/2"], "disctotal": ["2"]}
    result = tc._prepare_tags(tags)
    assert result["discnumber"] == ["1/2"]
    assert "disctotal" not in result


@pytest.mark.parametrize("invalid_val", ["abc", "12/abc", "12/12/12", "12/"])
def test_prepare_tags_non_integer_total_raises_upload_error(invalid_val: str) -> None:
    tags = {"tracknumber": ["1"], "tracktotal": [invalid_val]}
    with pytest.raises(UploadError, match="Non-integer total values for tracknumber"):
        tc._prepare_tags(tags)


def test_prepare_tags_conflicting_totals_raise_upload_error() -> None:
    tags = {"tracknumber": ["1"], "tracktotal": ["12"], "totaltracks": ["13"]}
    with pytest.raises(UploadError, match="conflicting values of"):
        tc._prepare_tags(tags)


def test_conflicting_values_within_one_total_tag_raise_upload_error() -> None:
    tags = {"tracknumber": ["1"], "tracktotal": ["12", "13"]}
    with pytest.raises(UploadError, match="conflicting values of tracktotal"):
        tc._prepare_tags(tags)


def test_an_unreadable_total_names_the_file(tmp_path) -> None:
    source = _source(tmp_path / "Artist - Album (2024) [FLAC]")
    track = source / "01.flac"
    fl = flac.FLAC(track)
    fl["tracknumber"] = "1"
    fl["tracktotal"] = "abc"
    fl.save()

    with pytest.raises(UploadError) as raised:
        tc._collect_transcode_items(str(source), str(tmp_path / "out"))

    assert str(raised.value) == f"{track}: Non-integer total values for tracknumber: tracktotal='abc'"


# --- Extra files: a lossy conversion copies the top level only (#1030) ---------------------------------------------


def _tree(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def _scans_album(tmp_path: Path) -> Path:
    album = tmp_path / "src"
    (album / "Scans").mkdir(parents=True)
    (album / "01.flac").write_bytes(b"fLaC")
    for name in ("cover.jpg", "info.txt", "Scans/01.jpg"):
        (album / name).write_bytes(b"x")
    return album


def test_a_transcode_copies_top_level_extras_and_no_subfolder(tmp_path: Path, capsys) -> None:
    album, out = _scans_album(tmp_path), tmp_path / "out"

    tc._copy_extra_files(str(album), str(out))

    assert _tree(out) == ["cover.jpg", "info.txt"]
    assert "Skip  Scans/01.jpg" in capsys.readouterr().out


def test_essential_only_still_drops_a_top_level_text_file(tmp_path: Path) -> None:
    album, out = _scans_album(tmp_path), tmp_path / "out"

    tc._copy_extra_files(str(album), str(out), essential_only=True)

    assert _tree(out) == ["cover.jpg"]


def test_a_downconversion_still_keeps_subfolder_extras(tmp_path: Path) -> None:
    album, out = _scans_album(tmp_path), tmp_path / "out"

    dc._copy_extra_files(str(album), str(out), frozenset({str(album / "01.flac")}))

    assert _tree(out) == ["Scans/01.jpg", "cover.jpg", "info.txt"]


def test_disc_folders_still_get_their_mp3s_and_no_loose_files(tmp_path: Path) -> None:
    album = tmp_path / "src"
    for cd in ("CD1", "CD2"):
        (album / cd).mkdir(parents=True)
        _write_flac(album / cd / "01.flac", 44100, 16, title="One", artist="A", album="B", tracknumber="1")
    (album / "CD1" / "folder.jpg").write_bytes(b"x")
    out = tmp_path / "out"

    items = tc._collect_transcode_items(str(album), str(out))
    tc._copy_extra_files(str(album), str(out))

    assert sorted(Path(i.dst).relative_to(out).as_posix() for i in items) == ["CD1/01.mp3", "CD2/01.mp3"]
    assert not out.exists() or _tree(out) == []


def _flac_item(tmp_path: Path, *, picture: bool) -> Any:
    fake: Any = SimpleNamespace(pictures=[object()] if picture else [])
    return tc.TranscodeItem(src=str(tmp_path / "01.flac"), dst=str(tmp_path / "01.mp3"), tags={}, flac_obj=fake)


def test_no_cover_is_said_when_nothing_holds_one(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    out.mkdir()

    tc._warn_if_no_cover(str(out), [_flac_item(tmp_path, picture=False)])

    assert "No cover" in capsys.readouterr().out


def test_no_cover_is_not_said_with_an_embedded_picture(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    out.mkdir()

    tc._warn_if_no_cover(str(out), [_flac_item(tmp_path, picture=True)])

    assert "No cover" not in capsys.readouterr().out


def test_no_cover_is_not_said_with_a_top_level_image(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "cover.jpg").write_bytes(b"x")

    tc._warn_if_no_cover(str(out), [_flac_item(tmp_path, picture=False)])

    assert "No cover" not in capsys.readouterr().out
