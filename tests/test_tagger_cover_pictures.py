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
# A PICTURE block holds 32 bytes of fields (type, lengths, dimensions) and the MIME type besides the image.
BLOCK_FIELDS = 32 + len("image/jpeg")
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


def _image(image_format: str, size: int = 0) -> bytes:
    """A small real image, padded after its end to `size` bytes, which readers ignore."""
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, image_format)
    return buffer.getvalue().ljust(size, b"\0")


FRONT = _image("jpeg", 1500 * KIB)


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(file.relative_to(folder)): file.read_bytes() for file in sorted(folder.rglob("*")) if file.is_file()}


@pytest.fixture
def album(tmp_path) -> Path:
    """A release with a 1.5 MiB front cover in one FLAC, 2 MiB of padding in another, one within the limit."""
    folder = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    _write_flac(
        folder / "01. One.flac",
        pictures=((PictureType.COVER_FRONT, FRONT), (PictureType.COVER_BACK, b"back")),
    )
    _write_flac(folder / "CD2" / "02. Two.flac", padding=2 * MIB)
    _write_flac(folder / "03. Three.flac", pictures=((PictureType.COVER_FRONT, b"small" * (100 * KIB)),))
    return folder


def test_measures_pictures_and_padding(album) -> None:
    pictures = 1500 * KIB + 4 + 2 * BLOCK_FIELDS
    assert cover.pictures_and_padding_size(FLAC(album / "01. One.flac")) == pictures + 8 * KIB
    assert cover.pictures_and_padding_size(FLAC(album / "CD2" / "02. Two.flac")) == 2 * MIB
    assert cover.find_oversized_pictures(str(album)) == {
        "01. One.flac": pictures + 8 * KIB,
        "CD2/02. Two.flac": 2 * MIB,
    }


def test_exactly_one_mib_is_within_the_limit(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", padding=MIB)

    assert cover.find_oversized_pictures(str(tmp_path)) == {}


def test_the_picture_block_counts_not_only_the_image(tmp_path) -> None:
    # The image and the padding make 1 MiB less a byte; the PICTURE block's own fields put the file over.
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, b"x" * (MIB - 8 * KIB - 1)),))

    assert cover.find_oversized_pictures(str(tmp_path)) == {"01.flac": MIB - 1 + BLOCK_FIELDS}


def test_auto_compress_cover_strips_when_the_picture_block_is_over(tmp_path) -> None:
    folder_cover = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(folder_cover, "jpeg")
    (tmp_path / "cover.jpg").write_bytes(folder_cover.getvalue())
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, b"x" * (MIB - 8 * KIB - 1)),))

    cover.compress_pictures(str(tmp_path))

    # Stripped, then the folder's cover embedded in place of the oversized one.
    assert [picture.data for picture in FLAC(tmp_path / "01.flac").pictures] == [folder_cover.getvalue()]


def test_strips_by_default(album, capsys) -> None:
    before = _snapshot(album)
    assert cfg.image.strip_oversized_pictures is True

    cover.check_embedded_pictures(str(album))

    output = capsys.readouterr().out
    assert "exceed RED's 1 MiB limit" in output
    assert "01. One.flac: 1.47 MiB (484.09 KiB over)" in output
    assert "CD2/02. Two.flac: 2 MiB (1 MiB over)" in output
    assert "03. Three.flac" not in output
    for name in ("01. One.flac", "CD2/02. Two.flac"):
        audio = FLAC(album / name)
        assert audio.pictures == []
        assert cover.pictures_and_padding_size(audio) == 8 * KIB
        assert (album / name).read_bytes().endswith(AUDIO)
    # The front cover is kept as a file, and the file within the limit is left alone.
    assert (album / "cover.jpg").read_bytes() == FRONT
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


@pytest.mark.parametrize(
    ("front", "kept_as"),
    [
        (_image("gif", 2 * MIB), "cover.png"),
        (_image("png", 2 * MIB), "cover.png"),
    ],
    ids=["gif converted to png", "png labelled image/jpeg"],
)
def test_the_front_cover_is_kept_as_a_jpeg_or_png_file(tmp_path, front: bytes, kept_as: str) -> None:
    # _write_flac labels every picture image/jpeg: the file's format comes from the data, not the label.
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, front),))

    cover.check_embedded_pictures(str(tmp_path))

    assert FLAC(tmp_path / "01.flac").pictures == []
    assert sorted(file.name for file in tmp_path.iterdir()) == ["01.flac", kept_as]
    with Image.open(tmp_path / kept_as) as image:
        assert image.format == "PNG"
        assert image.size == (10, 10)


def test_a_front_cover_that_is_not_an_image_is_not_lost(tmp_path, capsys) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, b"not an image" * (100 * KIB)),))
    _write_flac(tmp_path / "02.flac", padding=2 * MIB)
    unreadable = (tmp_path / "01.flac").read_bytes()

    cover.check_embedded_pictures(str(tmp_path))

    # The file whose cover cannot be kept as a file keeps it embedded; the others are still stripped.
    output = capsys.readouterr().out
    assert "01.flac as it is: its front cover could not be read" in output
    assert "Stripped 1 file(s) to no embedded pictures and 8 KiB of padding." in output
    assert (tmp_path / "01.flac").read_bytes() == unreadable
    assert cover.pictures_and_padding_size(FLAC(tmp_path / "02.flac")) == 8 * KIB
    assert sorted(file.name for file in tmp_path.iterdir()) == ["01.flac", "02.flac"]


def test_a_failed_cover_write_leaves_no_partial_cover(tmp_path, monkeypatch) -> None:
    # A partial cover.jpg would pass for the folder's cover next time, and the embedded one would be stripped.
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))
    before = _snapshot(tmp_path)

    class DiskFull(io.FileIO):
        def write(self, data) -> int:
            super().write(bytes(data)[:1000])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(cover, "open", DiskFull, raising=False)

    with pytest.raises(OSError, match="No space left"):
        cover.check_embedded_pictures(str(tmp_path))

    assert _snapshot(tmp_path) == before


def test_the_cover_write_never_touches_an_existing_file(tmp_path, monkeypatch) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))
    (tmp_path / "cover.jpg.part").write_bytes(b"the user's own file")

    cover.check_embedded_pictures(str(tmp_path))

    assert (tmp_path / "cover.jpg").read_bytes() == FRONT
    assert (tmp_path / "cover.jpg.part").read_bytes() == b"the user's own file"
    assert sorted(file.name for file in tmp_path.iterdir()) == ["01.flac", "cover.jpg", "cover.jpg.part"]

    # Even a temporary name that is already taken raises rather than truncating or removing that file.
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / f".{'0' * 32}.part").write_bytes(b"someone else's")
    monkeypatch.setattr(cover.uuid, "uuid4", lambda: cover.uuid.UUID(int=0))

    with pytest.raises(FileExistsError):
        cover._write_whole_file(str(taken / "cover.jpg"), FRONT)

    assert sorted(file.name for file in taken.iterdir()) == [f".{'0' * 32}.part"]
    assert (taken / f".{'0' * 32}.part").read_bytes() == b"someone else's"


def test_auto_compress_cover_embeds_within_the_limit_counting_the_picture_block(tmp_path) -> None:
    # The image alone fits beside 8 KiB of padding; with the PICTURE block's own fields it would not.
    (tmp_path / "cover.jpg").write_bytes(_image("jpeg", MIB - 8 * KIB - 1))
    _write_flac(tmp_path / "01.flac")

    cover.compress_pictures(str(tmp_path))

    audio = FLAC(tmp_path / "01.flac")
    assert len(audio.pictures) == 1
    assert cover.pictures_and_padding_size(audio) <= MIB


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
    assert cover.pictures_and_padding_size(audio) == len(front.getvalue()) + BLOCK_FIELDS + 8 * KIB
    assert (tmp_path / "01.flac").read_bytes().endswith(AUDIO)
