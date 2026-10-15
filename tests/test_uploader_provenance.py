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

import salmon.checks.provenance
import salmon.trackers
import salmon.uploader
from salmon import cfg

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

HEADING = "Tag markers the audio contradicts:"
NOT_CHECKED = "Tag markers not checked: the tags could not be read."
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


def test_up_reads_the_markers_before_any_tag_is_rewritten(monkeypatch, tmp_path, torrents) -> None:
    order: list[str] = []
    gather, standardize = salmon.uploader.gather_provenance, salmon.uploader.standardize_tags

    def gathering(path: str):
        order.append("read markers")
        return gather(path)

    def standardizing(path: str) -> None:
        order.append("rewrite tags")
        standardize(path)

    monkeypatch.setattr(salmon.uploader, "gather_provenance", gathering)
    monkeypatch.setattr(salmon.uploader, "standardize_tags", standardizing)

    run = _run_up(monkeypatch, _album(tmp_path / "Album", comment="24bit"), torrents, input=ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert order[:2] == ["read markers", "rewrite tags"]
    assert HEADING in run.result.output


def test_up_says_when_the_markers_could_not_be_read_and_uploads(monkeypatch, tmp_path, torrents) -> None:
    def unreadable(_path: str) -> None:
        raise OSError("test: unreadable tags")

    # Only the marker check's read fails; the upload's own tag reads go on as before.
    monkeypatch.setattr(salmon.checks.provenance, "gather_tags", unreadable)

    run = _run_up(monkeypatch, _album(tmp_path / "Album", comment="24bit"), torrents, input=ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert NOT_CHECKED in run.result.output
    assert HEADING not in run.result.output
    assert "Successfully uploaded" in run.result.output
