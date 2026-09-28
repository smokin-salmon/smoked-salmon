"""FLACs whose embedded pictures plus padding exceed RED's 1 MiB limit: stripped, or only listed on opt-out."""

import io
import struct
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from salmon import cfg
from salmon.tagger import cover

MIB = 1024 * 1024
KIB = 1024
# Stands in for the audio frames, which follow the metadata blocks and must come out of a strip unchanged.
AUDIO = b"\xff\xf8 not really audio " * 1000


def _write_flac(path: Path, *, pictures: tuple[tuple[int, bytes], ...] = (), padding: int = 8 * KIB) -> None:
    """Write a FLAC with fake audio, the given (picture type, data) pictures and padding."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo + AUDIO)
    audio = FLAC(path)
    for picture_type, data in pictures:
        picture = Picture()
        picture.type = picture_type
        picture.mime = "image/jpeg"
        picture.data = data
        audio.add_picture(picture)
    audio.save(padding=lambda _info: padding)


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(file.relative_to(folder)): file.read_bytes() for file in sorted(folder.rglob("*")) if file.is_file()}


@pytest.fixture
def album(tmp_path) -> Path:
    """A release with a 1.5 MiB front cover in one FLAC, 2 MiB of padding in another, one within the limit."""
    folder = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    _write_flac(
        folder / "01. One.flac",
        pictures=((PictureType.COVER_FRONT, b"front" * (300 * KIB)), (PictureType.COVER_BACK, b"back")),
    )
    _write_flac(folder / "CD2" / "02. Two.flac", padding=2 * MIB)
    _write_flac(folder / "03. Three.flac", pictures=((PictureType.COVER_FRONT, b"small" * (100 * KIB)),))
    return folder


def test_measures_pictures_and_padding(album) -> None:
    assert cover.pictures_and_padding_size(FLAC(album / "01. One.flac")) == 1500 * KIB + 4 + 8 * KIB
    assert cover.pictures_and_padding_size(FLAC(album / "CD2" / "02. Two.flac")) == 2 * MIB
    assert cover.find_oversized_pictures(str(album)) == {
        "01. One.flac": 1500 * KIB + 4 + 8 * KIB,
        "CD2/02. Two.flac": 2 * MIB,
    }


def test_exactly_one_mib_is_within_the_limit(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", padding=MIB)

    assert cover.find_oversized_pictures(str(tmp_path)) == {}


def test_strips_by_default(album, capsys) -> None:
    before = _snapshot(album)
    assert cfg.image.strip_oversized_pictures is True

    cover.check_embedded_pictures(str(album))

    output = capsys.readouterr().out
    assert "exceed RED's 1 MiB limit" in output
    assert "01. One.flac: 1.47 MiB (484 KiB over)" in output
    assert "CD2/02. Two.flac: 2 MiB (1 MiB over)" in output
    assert "03. Three.flac" not in output
    for name in ("01. One.flac", "CD2/02. Two.flac"):
        audio = FLAC(album / name)
        assert audio.pictures == []
        assert cover.pictures_and_padding_size(audio) == 8 * KIB
        assert (album / name).read_bytes().endswith(AUDIO)
    # The front cover is kept as a file, and the file within the limit is left alone.
    assert (album / "cover.jpg").read_bytes() == b"front" * (300 * KIB)
    assert _snapshot(album)["03. Three.flac"] == before["03. Three.flac"]


def test_only_warns_with_the_setting_off(album, monkeypatch, capsys) -> None:
    before = _snapshot(album)
    monkeypatch.setattr(cfg.image, "strip_oversized_pictures", False)

    cover.check_embedded_pictures(str(album))

    output = capsys.readouterr().out
    assert "CD2/02. Two.flac: 2 MiB (1 MiB over)" in output
    assert "strip_oversized_pictures is off" in output
    assert _snapshot(album) == before


def test_an_existing_cover_file_is_not_overwritten(album) -> None:
    (album / "folder.png").write_bytes(b"the folder's own cover")

    cover.check_embedded_pictures(str(album))

    assert FLAC(album / "01. One.flac").pictures == []
    assert (album / "folder.png").read_bytes() == b"the folder's own cover"
    assert not (album / "cover.jpg").exists()


def test_nothing_is_said_or_changed_within_the_limit(tmp_path, capsys) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, b"x" * (900 * KIB)),))
    before = _snapshot(tmp_path)

    cover.check_embedded_pictures(str(tmp_path))

    assert capsys.readouterr().out == ""
    assert _snapshot(tmp_path) == before


def test_auto_compress_cover_still_strips_and_embeds_the_cover(tmp_path) -> None:
    # compress_pictures shares the strip with check_embedded_pictures; its own behaviour is unchanged.
    front = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(front, "jpeg")
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, front.getvalue()),), padding=2 * MIB)

    cover.compress_pictures(str(tmp_path))

    audio = FLAC(tmp_path / "01.flac")
    assert (tmp_path / "cover.jpg").read_bytes() == front.getvalue()
    assert [picture.data for picture in audio.pictures] == [front.getvalue()]
    assert cover.pictures_and_padding_size(audio) == len(front.getvalue()) + 8 * KIB
    assert (tmp_path / "01.flac").read_bytes().endswith(AUDIO)
