"""Spectral PNGs are compressed losslessly, with oxipng where it installs and Pillow where it does not (Python 3.14)."""

import struct
import zlib
from functools import partial
from pathlib import Path

import anyio
from PIL import Image, PngImagePlugin

from salmon.uploader import spectrals


def _chunk_types(path: Path) -> list[bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    types, pos = [], 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        types.append(data[pos + 4 : pos + 8])
        pos += 12 + length
    return types


def _spectral_like_png(path: Path) -> None:
    """A palette PNG with metadata chunks, like sox writes, plus an ICC profile and EXIF."""
    image = Image.new("P", (200, 120))
    image.putpalette([v for i in range(256) for v in (i, (i * 3) % 256, 255 - i)])
    image.putdata([(x * y + x) % 256 for y in range(120) for x in range(200)])
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "SoX")
    info.add_text("Comment", "a" * 500, zip=True)
    image.save(path, pnginfo=info, icc_profile=b"\0" * 200, exif=b"Exif\0\0" + b"\0" * 20, compress_level=1)


def _pixels(path: Path) -> tuple[str, bytes, list[int] | None]:
    with Image.open(path) as image:
        return image.mode, image.tobytes(), image.getpalette()


def test_without_oxipng_pillow_keeps_the_pixels_and_drops_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(spectrals, "oxipng", None)
    png = tmp_path / "01 Full.png"
    _spectral_like_png(png)
    before = _pixels(png)
    size_before = png.stat().st_size
    assert {b"tEXt", b"zTXt", b"iCCP", b"eXIf"} <= set(_chunk_types(png))

    anyio.run(partial(spectrals._compress_single_spectral, str(png), 0))

    assert _pixels(png) == before
    assert set(_chunk_types(png)) <= {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}
    assert png.stat().st_size < size_before
    # Every chunk's CRC is valid: the result is a well-formed PNG, not only something Pillow tolerates.
    data, pos = png.read_bytes(), 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert zlib.crc32(data[pos + 4 : pos + 8 + length]) == crc
        pos += 12 + length


def test_with_oxipng_it_is_used(monkeypatch, tmp_path) -> None:
    calls = []

    class FakeStripChunks:
        @staticmethod
        def all() -> str:
            return "all"

    class FakeOxipng:
        StripChunks = FakeStripChunks

        @staticmethod
        def optimize(path, **kwargs) -> None:
            calls.append((path, kwargs))

    monkeypatch.setattr(spectrals, "oxipng", FakeOxipng)
    png = tmp_path / "01 Full.png"
    _spectral_like_png(png)
    contents = png.read_bytes()

    anyio.run(partial(spectrals._compress_single_spectral, str(png), 0))

    assert calls == [(str(png), {"level": 2, "strip": "all"})]
    assert png.read_bytes() == contents
