"""`salmon specs` prints the frequency analysis next to its spectrals and asks nothing (#592).

Synthetic FLACs are measured for real; sox is replaced by a fake that writes the spectrals, the image host by a
recorder. No tracker or host is contacted.
"""

import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from test_uploader_frequency import (  # pyright: ignore[reportMissingImports]
    RATE,
    lowpass,
    music_like,
    write_flac,
)

import salmon.images
from salmon import cfg
from salmon.commands import specs
from salmon.images.base import BaseImageUploader
from salmon.uploader import frequency, spectrals
from salmon.uploader.spectrals import get_spectrals_path


@pytest.fixture
def album(tmp_path, monkeypatch) -> str:
    path = tmp_path / "album"
    path.mkdir()
    # A 128 kbps-style brick wall over quiet highs: the analysis has something to say.
    samples = lowpass(music_like(seconds=8), 16_000.0)
    for number in (1, 2):
        write_flac(path / f"{number:02d} track.flac", samples, RATE)

    async def fake_sox(args, **_kwargs) -> None:
        for i, arg in enumerate(args):
            if arg == "-o":
                Path(args[i + 1]).write_bytes(b"png")

    async def no_viewer(*_args, **_kwargs) -> None:
        pass

    async def no_question(*_args, **_kwargs):
        raise AssertionError("salmon specs must not ask the lossy-master question")

    async def pick_all(spectral_ids, *_args, **_kwargs):
        return spectral_ids

    monkeypatch.setattr(spectrals.anyio, "run_process", fake_sox)
    monkeypatch.setattr(spectrals, "view_spectrals", no_viewer)
    monkeypatch.setattr(spectrals, "prompt_lossy_master", no_question)
    monkeypatch.setattr(spectrals, "prompt_spectrals", pick_all)
    monkeypatch.setattr(spectrals.cfg.upload, "yes_all", False)
    monkeypatch.setattr(spectrals.cfg.upload.compression, "compress_spectrals", False)
    monkeypatch.setattr(spectrals.cfg.directory, "tmp_dir", None)
    return str(path)


@pytest.fixture
def image_uploads(monkeypatch) -> list[str]:
    """The file names the fake host took."""
    uploads: list[str] = []

    class ImageUploader(BaseImageUploader):
        async def upload_file(self, filename: str) -> tuple[str, None]:
            uploads.append(os.path.basename(filename))
            return f"https://images.test/{len(uploads)}.png", None

    monkeypatch.setitem(salmon.images.HOSTS, "testhost", SimpleNamespace(ImageUploader=ImageUploader))
    monkeypatch.setattr(cfg.image, "specs_uploader", "testhost")
    return uploads


def run_specs(album: str, no_delete_specs: bool = False) -> None:
    assert specs.callback is not None
    anyio.run(partial(specs.callback, album, no_delete_specs, False))


def test_specs_prints_the_analysis_and_writes_the_plots(album, image_uploads, capsys) -> None:
    run_specs(album, no_delete_specs=True)

    out = capsys.readouterr().out
    assert "Frequency analysis: " in out
    assert "A measurement, not a verdict" in out
    folder = get_spectrals_path(album)
    assert sorted(f for f in os.listdir(folder) if f.endswith("Spectrum.png")) == ["01 Spectrum.png", "02 Spectrum.png"]


def test_specs_uploads_the_spectrals_and_none_of_the_plots(album, image_uploads, monkeypatch) -> None:
    upload = spectrals.upload_spectrals
    in_folder: list[str] = []

    async def upload_and_look(spectrals_path, *args, **kwargs):
        in_folder.extend(os.listdir(spectrals_path))
        return await upload(spectrals_path, *args, **kwargs)

    monkeypatch.setattr(spectrals, "upload_spectrals", upload_and_look)

    run_specs(album)

    # The plots are there to be uploaded, and are not.
    assert "01 Spectrum.png" in in_folder
    assert sorted(image_uploads) == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]


def test_specs_leaves_nothing_behind_when_it_deletes_the_spectrals(album, image_uploads, monkeypatch) -> None:
    delete = spectrals._delete_spectrals
    in_folder: list[str] = []

    async def delete_and_look(spectrals_path):
        in_folder.extend(os.listdir(spectrals_path))
        await delete(spectrals_path)

    monkeypatch.setattr(spectrals, "_delete_spectrals", delete_and_look)

    run_specs(album)

    assert "01 Spectrum.png" in in_folder
    assert not os.path.exists(get_spectrals_path(album))


def test_a_failing_analysis_prints_one_line_and_specs_carries_on(album, image_uploads, monkeypatch, capsys) -> None:
    async def broken(*_args, **_kwargs):
        raise RuntimeError("no decoder")

    monkeypatch.setattr(frequency, "generate_frequency_plots", broken)

    run_specs(album)

    out = capsys.readouterr().out
    assert out.count("Frequency analysis failed") == 1
    assert "Frequency analysis:" not in out
    assert sorted(image_uploads) == ["01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png"]


def test_salmon_up_prints_the_analysis_once(album, monkeypatch, capsys) -> None:
    async def not_lossy(*_args, **_kwargs) -> bool:
        return False

    # As salmon up asks: no lossy answer yet, so the analysis comes before the question.
    monkeypatch.setattr(spectrals, "prompt_lossy_master", not_lossy)
    audio_info = {name: {"duration": 8} for name in ("01 track.flac", "02 track.flac")}

    anyio.run(spectrals.check_spectrals, album, audio_info)

    assert capsys.readouterr().out.count("Frequency analysis: ") == 1
