"""download_cover_if_nonexistent falls back to an embedded FLAC front cover when the folder has none."""

import asyncio
import io
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
from aiohttp import web
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

import salmon.uploader
from salmon import interaction
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


# --- Downloads, from a local fake store ------------------------------------------------------------


def _download(tmp_path: Path, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> str | None:
    """Run _download_cover against a local server whose /cover.jpg answers with `handler`."""

    async def main() -> str | None:
        app = web.Application()
        app.router.add_get("/cover.jpg", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            return await cover._download_cover(str(tmp_path), f"http://127.0.0.1:{runner.addresses[0][1]}/cover.jpg")
        finally:
            await runner.cleanup()

    return anyio.run(main)


def test_a_cover_download_cut_off_partway_leaves_no_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cover.cfg.upload.formatting, "lowercase_cover", False)

    async def cut_off(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Length": str(len(FRONT) * 10), "Content-Type": "image/jpeg"})
        await response.prepare(request)
        await response.write(FRONT[:40])
        assert request.transport is not None
        request.transport.close()
        return response

    result = _download(tmp_path, cut_off)

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_a_downloaded_cover_is_written_whole(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cover.cfg.upload.formatting, "lowercase_cover", False)

    async def whole(_request: web.Request) -> web.Response:
        return web.Response(body=FRONT, content_type="image/jpeg")

    result = _download(tmp_path, whole)

    assert result == str(tmp_path / "Cover.jpg")
    assert [entry.name for entry in tmp_path.iterdir()] == ["Cover.jpg"]
    assert (tmp_path / "Cover.jpg").read_bytes() == FRONT


def test_a_cover_download_that_times_out_is_reported_not_raised(tmp_path, monkeypatch, capsys) -> None:
    real_timeout = cover.aiohttp.ClientTimeout
    monkeypatch.setattr(cover.aiohttp, "ClientTimeout", lambda **_kwargs: real_timeout(total=0.2))

    async def stalled(_request: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(body=FRONT, content_type="image/jpeg")

    result = _download(tmp_path, stalled)

    assert result is None
    assert list(tmp_path.iterdir()) == []
    assert "Failed to download cover image (ERROR TimeoutError)" in capsys.readouterr().out


def test_a_cover_bigger_than_an_image_may_be_is_not_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cover, "MAX_IMAGE_BYTES", len(FRONT) - 1)

    async def whole(_request: web.Request) -> web.Response:
        return web.Response(body=FRONT, content_type="image/jpeg")

    result = _download(tmp_path, whole)

    assert result is None
    assert list(tmp_path.iterdir()) == []


def _noisy_jpeg() -> bytes:
    """A JPEG big enough that a cut-off copy still has a whole header."""
    buffer = io.BytesIO()
    Image.frombytes("RGB", (64, 64), bytes(range(256)) * 48).save(buffer, "jpeg", quality=95)
    return buffer.getvalue()


def test_a_cover_cut_off_with_no_content_length_is_not_kept(tmp_path, monkeypatch) -> None:
    body = _noisy_jpeg()

    async def ends_early(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        response.enable_chunked_encoding()
        await response.prepare(request)
        await response.write(body[: len(body) // 2])
        await response.write_eof()
        return response

    result = _download(tmp_path, ends_early)

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_a_failed_cover_download_never_prints_its_url(tmp_path, capsys) -> None:
    async def loops(request: web.Request) -> web.Response:
        raise web.HTTPFound("/cover.jpg?sig=test-sig-0001")

    result = _download(tmp_path, loops)

    assert result is None
    out = capsys.readouterr().out
    assert "test-sig-0001" not in out
    assert "Failed to download cover image (ERROR TooManyRedirects)" in out


def test_a_cover_whose_size_would_take_gigabytes_to_load_is_refused() -> None:
    """A small file can claim a huge image: 9000x9000 is under Pillow's own limit, far over any cover."""
    huge = io.BytesIO()
    Image.new("1", (9000, 9000)).save(huge, "png")

    assert len(huge.getvalue()) < 1_000_000
    assert cover._is_valid_cover(io.BytesIO(huge.getvalue())) is False
    assert cover._is_valid_cover(io.BytesIO(FRONT)) is True


def test_the_no_cover_message_for_a_new_group_names_a_failed_download(tmp_path, monkeypatch, capsys) -> None:
    async def no_cover(*_args, **_kwargs) -> tuple[None, bool]:
        return None, False

    monkeypatch.setattr(salmon.uploader, "get_cover_url", no_cover)

    with interaction.assuming_defaults():
        result = anyio.run(
            salmon.uploader.resolve_cover_url, RedApi(), None, {}, str(tmp_path), "https://store/cover.jpg", True
        )

    assert result == (False, None)
    out = capsys.readouterr().out
    assert "No cover image for this new group on RED: none was found, its download failed, or the upload to" in out
