"""upload_spectrals refuses a spectral that is not a readable image before it asks the host for anything."""

from pathlib import Path

import anyio
import pytest
from PIL import Image

from salmon import dryrun
from salmon.uploader import spectrals as sp


@pytest.fixture(autouse=True)
def _spectral_image_check() -> None:
    """Override the suite's fixture: these tests run the real check."""


def _write(tmp_path: Path, broken: str | None) -> None:
    for name in ("01 Full.png", "01 Zoom.png"):
        path = tmp_path / name
        Image.new("RGB", (64, 64), "white").save(path, "PNG")
        if name == broken:
            path.write_bytes(path.read_bytes()[:40])


def _upload(monkeypatch, tmp_path: Path, *, dry: bool = False):
    received: list[str] = []

    async def host(spectrals_list, tracker=None):
        received.extend(path for _sid, _filename, paths in spectrals_list for path in paths)
        return {sid: [f"https://img.example/{Path(p).name}" for p in paths] for sid, _f, paths in spectrals_list}

    monkeypatch.setattr(sp, "upload_spectral_imgs", host)

    async def go():
        try:
            return await sp.upload_spectrals(str(tmp_path), {1: "01 a.flac"})
        except sp.SpectralsNotUploaded as failure:
            return failure

    if dry:
        with dryrun.mode():
            return anyio.run(go), received
    return anyio.run(go), received


@pytest.mark.parametrize("dry", [False, True], ids=["upload", "dry run"])
@pytest.mark.parametrize("oxipng", ["absent", "present"])
def test_a_truncated_spectral_refuses_the_whole_upload(monkeypatch, tmp_path, capsys, dry, oxipng) -> None:
    if oxipng == "absent":
        monkeypatch.setattr(sp, "oxipng", None)
    _write(tmp_path, broken="01 Zoom.png")

    result, received = _upload(monkeypatch, tmp_path, dry=dry)

    assert isinstance(result, sp.SpectralsNotUploaded)
    assert received == []
    out = capsys.readouterr().out
    assert "01 Zoom.png is not a usable image (" in out
    assert "no spectrals are uploaded." in out


def test_a_missing_spectral_refuses_the_upload(monkeypatch, tmp_path, capsys) -> None:
    _write(tmp_path, broken=None)
    (tmp_path / "01 Full.png").unlink()

    result, received = _upload(monkeypatch, tmp_path)

    assert isinstance(result, sp.SpectralsNotUploaded)
    assert received == []
    assert "01 Full.png is not a usable image" in capsys.readouterr().out


def test_readable_spectrals_are_uploaded(monkeypatch, tmp_path) -> None:
    _write(tmp_path, broken=None)

    result, received = _upload(monkeypatch, tmp_path)

    assert result == {1: ["https://img.example/01 Full.png", "https://img.example/01 Zoom.png"]}
    assert [Path(p).name for p in received] == ["01 Full.png", "01 Zoom.png"]
