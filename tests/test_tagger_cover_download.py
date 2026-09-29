"""download_cover_if_nonexistent falls back to an embedded FLAC front cover when the folder has none."""

import io
import struct
from pathlib import Path

import anyio
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

import salmon.uploader
from salmon.tagger import cover
from salmon.trackers.red import RedApi

KIB = 1024
AUDIO = b"\xff\xf8 not really audio " * 1000


def _write_flac(path: Path, *, pictures: tuple[tuple[int, bytes], ...] = ()) -> None:
    """Write a FLAC with fake audio and the given (picture type, data) pictures."""
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
    audio.save(padding=lambda _info: 8 * KIB)


def _image(image_format: str = "jpeg") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, image_format)
    return buffer.getvalue()


FRONT = _image()


def test_no_cover_file_no_url_uses_the_embedded_front_cover(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))

    cover_path, was_downloaded = anyio.run(cover.download_cover_if_nonexistent, str(tmp_path), None)

    assert cover_path == str(tmp_path / "cover.jpg")
    assert was_downloaded is True
    assert cover_path is not None
    assert Path(cover_path).read_bytes() == FRONT


def test_a_failed_download_falls_back_to_the_embedded_front_cover(tmp_path, monkeypatch) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))

    async def failing_download(_path: str, _cover_url: str) -> None:
        return None

    monkeypatch.setattr(cover, "_download_cover", failing_download)

    cover_path, was_downloaded = anyio.run(
        cover.download_cover_if_nonexistent, str(tmp_path), "https://store/cover.jpg"
    )

    assert cover_path == str(tmp_path / "cover.jpg")
    assert was_downloaded is True
    assert cover_path is not None
    assert Path(cover_path).read_bytes() == FRONT


def test_a_successful_download_wins_over_the_embedded_cover(tmp_path, monkeypatch) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))

    async def fake_download(path: str, _cover_url: str) -> str:
        downloaded = Path(path) / "Cover.jpg"
        downloaded.write_bytes(b"downloaded cover")
        return str(downloaded)

    monkeypatch.setattr(cover, "_download_cover", fake_download)

    cover_path, was_downloaded = anyio.run(
        cover.download_cover_if_nonexistent, str(tmp_path), "https://store/cover.jpg"
    )

    assert cover_path == str(tmp_path / "Cover.jpg")
    assert was_downloaded is True
    assert cover_path is not None
    assert not (tmp_path / "cover.jpg").exists()


def test_an_existing_cover_file_wins_over_the_embedded_cover(tmp_path, monkeypatch) -> None:
    (tmp_path / "cover.png").write_bytes(b"the folder's own cover")
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))

    async def fake_download(path: str, _cover_url: str) -> str:
        raise AssertionError("should not be called when a cover file already exists")

    monkeypatch.setattr(cover, "_download_cover", fake_download)

    cover_path, was_downloaded = anyio.run(
        cover.download_cover_if_nonexistent, str(tmp_path), "https://store/cover.jpg"
    )

    assert cover_path == str(tmp_path / "cover.png")
    assert was_downloaded is False


def test_no_front_cover_anywhere_is_no_cover(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_BACK, FRONT),))

    result = anyio.run(cover.download_cover_if_nonexistent, str(tmp_path), None)

    assert result == (None, None)


def test_an_unreadable_embedded_picture_is_skipped_not_used(tmp_path) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, b"not an image" * 100),))

    result = anyio.run(cover.download_cover_if_nonexistent, str(tmp_path), None)

    assert result == (None, None)
    assert sorted(file.name for file in tmp_path.iterdir()) == ["01.flac"]


def test_embedded_cover_is_removed_after_upload_through_resolve_cover_url(tmp_path, monkeypatch) -> None:
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))

    async def fake_upload_cover(cover_path: str | None, host: str | None = None, red_api: object = None) -> str:
        assert cover_path == str(tmp_path / "cover.jpg")
        assert cover_path is not None
        assert Path(cover_path).exists()
        return f"https://{host}/uploaded.jpg"

    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload_cover)

    result = anyio.run(salmon.uploader.resolve_cover_url, RedApi(), None, {}, str(tmp_path), None, True)

    assert result == (True, "https://catbox/uploaded.jpg")
    assert not (tmp_path / "cover.jpg").exists()
