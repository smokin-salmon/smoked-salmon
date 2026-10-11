"""check_image: a file is an image by what is in it, never by its name (#657)."""

import io
from pathlib import Path

import pytest
from PIL import Image

from salmon.images import files
from salmon.images.files import NotAnImage, check_image


def _image(format: str) -> bytes:
    data = io.BytesIO()
    image = Image.new("RGB", (8, 8), "green")
    if format == "MPO":
        # Two pictures in one JPEG: the file Pillow reads back as an MPO.
        image.save(data, format, save_all=True, append_images=[Image.new("RGB", (8, 8), "red")])
    else:
        image.save(data, format)
    return data.getvalue()


@pytest.mark.parametrize(
    ("format", "found"),
    [("PNG", "PNG"), ("JPEG", "JPEG"), ("GIF", "GIF"), ("WEBP", "WEBP"), ("MPO", "MPO")],
)
def test_the_formats_the_hosts_take_are_images_whatever_the_file_is_called(
    tmp_path: Path, format: str, found: str
) -> None:
    # The name says nothing either way.
    path = tmp_path / "picture.torrent"
    path.write_bytes(_image(format))

    assert check_image(str(path)) == found


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (b"", "empty"),
        (b"d8:announce30:https://tracker.example/x/annoe", "not a JPEG, PNG, GIF or WebP"),
        (b"hello", "not a JPEG, PNG, GIF or WebP"),
        (_image("BMP"), "not a JPEG, PNG, GIF or WebP"),
        (_image("TIFF"), "not a JPEG, PNG, GIF or WebP"),
        (_image("PNG")[:-20], "corrupt|cut short"),
        (_image("PNG")[:40], "corrupt|cut short"),
    ],
)
def test_anything_else_is_refused_even_when_it_is_called_a_picture(tmp_path: Path, content: bytes, why: str) -> None:
    path = tmp_path / "cover.jpg"
    path.write_bytes(content)

    with pytest.raises(NotAnImage, match=why):
        check_image(str(path))


def test_an_image_with_too_many_pixels_is_not_decoded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(files, "MAX_IMAGE_PIXELS", 10)
    path = tmp_path / "huge.png"
    path.write_bytes(_image("PNG"))

    with pytest.raises(NotAnImage, match="too many pixels"):
        check_image(str(path))


def test_an_unreadable_file_is_the_callers_to_report_not_a_verdict_on_the_image(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        check_image(str(tmp_path / "missing.png"))
