"""`salmon up` warns about tag markers the audio contradicts, and goes on as before (#538).

The runs go against the local fake tracker of test_uploader_dry_run and a fake image host, never a real one.
"""

from pathlib import Path

import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _run_up,
    _write_flac,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.trackers
from salmon import cfg

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

HEADING = "Tag markers the audio contradicts:"
# A new group, keep the folder name, upload, no lossy report comment, no downconversion.
ANSWERS = "\ny\ny\n\nn\n"


@pytest.fixture
def torrents(monkeypatch, tmp_path) -> Path:
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    return torrents


def _album(folder: Path, **tags: str) -> Path:
    folder.mkdir(parents=True)
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", date="2020", **tags)
    _write_flac(folder / "02 - two.flac", title="Two", artist="Artist", date="2020")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def test_up_prints_the_contradiction_and_uploads_with_the_same_answers(monkeypatch, tmp_path, torrents) -> None:
    run = _run_up(monkeypatch, _album(tmp_path / "Album", comment="24bit"), torrents, input=ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert f"{HEADING}\n  - 01 - one.flac: comment claims 24bit, the audio is 16bit\n" in run.result.output
    assert "Successfully uploaded" in run.result.output


def test_up_prints_nothing_for_markers_the_audio_agrees_with(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album", comment="Qobuz", **{"encoded-by": "EAC"})

    run = _run_up(monkeypatch, album, torrents, input=ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert HEADING not in run.result.output
    assert "Successfully uploaded" in run.result.output
