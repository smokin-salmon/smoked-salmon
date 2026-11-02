"""An album's Spectrals folder salmon did not make is uploaded only when the user says so (#902).

The runs here go against a local fake tracker and a fake image host, never a real one.
"""

from pathlib import Path

import pytest
from helpers.uploader_dry_run import _album, _run_up, _snapshot
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from torf import Torrent

import salmon.trackers
from salmon import cfg
from salmon.uploader.spectrals import foreign_entries_text, foreign_spectrals_entries

pytestmark = pytest.mark.usefixtures("image_uploads")

QUESTION = "Upload it with the album anyway?"
# A new group, keep the folder name, upload, no lossy report comment, no downconversion: one tracker's run.
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


def _with_spectrals(album: Path, *names: str) -> Path:
    specs = album / "Spectrals"
    specs.mkdir()
    for name in names:
        (specs / name).write_bytes(b"png")
    return specs


def _uploaded_files(run) -> list[list[str]]:
    """The files of each torrent the run uploaded, as paths inside the torrent."""
    torrents = [
        Torrent.read_stream(dict(sent.fields)["file_input"][1])
        for sent in run.tracker.sent
        if sent.query.get("action") == "upload"
    ]
    return [["/".join(Path(str(file)).parts[1:]) for file in torrent.files] for torrent in torrents]


def test_the_run_asks_and_a_no_stops_it_before_any_tracker_request(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    before = _snapshot(specs)

    run = _run_up(monkeypatch, album, torrents, input="\n", yes_all=False)

    output = run.result.output
    assert QUESTION in output
    assert f"{specs} is not a spectrals folder salmon made" in output
    assert "01 Zoom (copy).png" in output
    # salmon's own image is not what made it judge the folder not its own.
    assert "01 Full.png" not in output
    assert f"Not uploading {specs}: move it out of the album and run salmon again." in output
    assert "Successfully uploaded" not in output
    assert run.tracker.sent == []
    assert _snapshot(specs) == before


def test_yyy_stops_without_asking(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    before = _snapshot(specs)

    run = _run_up(monkeypatch, album, torrents)

    output = run.result.output
    assert QUESTION not in output
    assert "01 Zoom (copy).png" in output
    assert f"Not uploading {specs}: move it out of the album and run salmon again." in output
    assert run.tracker.sent == []
    assert _snapshot(specs) == before


def test_a_yes_uploads_the_folder_as_before(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    before = _snapshot(specs)

    run = _run_up(monkeypatch, album, torrents, input="y\n" + ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert QUESTION in run.result.output
    assert "Successfully uploaded" in run.result.output
    files = _uploaded_files(run)
    assert files
    assert "Spectrals/01 Zoom (copy).png" in files[0]
    assert _snapshot(specs) == before


def test_salmons_own_spectrals_folder_asks_nothing(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    _with_spectrals(album, "01 Full.png", "01 Zoom.png")

    run = _run_up(monkeypatch, album, torrents, input=ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert QUESTION not in run.result.output
    assert "Successfully uploaded" in run.result.output


def test_the_entries_are_named_up_to_five(tmp_path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    specs = _with_spectrals(album, "01 Full.png", ".hidden", *(f"note {n}.txt" for n in range(6)))
    # A folder is never salmon's, whatever its name.
    (specs / "02 Zoom.png").mkdir()

    entries = foreign_spectrals_entries(str(album))

    assert entries is not None
    assert entries == ["02 Zoom.png", *(f"note {n}.txt" for n in range(6))]
    assert foreign_entries_text(entries) == "02 Zoom.png, note 0.txt, note 1.txt, note 2.txt, note 3.txt and 2 more"
    assert foreign_entries_text(entries[:5]) == "02 Zoom.png, note 0.txt, note 1.txt, note 2.txt, note 3.txt"


@pytest.mark.parametrize(
    "names",
    [(), ("01 Full.png", "01 Zoom.png", "01 Spectrum.png", ".hidden", "Thumbs.db")],
    ids=["none", "salmon's"],
)
def test_no_entry_to_name_is_no_foreign_folder(tmp_path, names: tuple[str, ...]) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    assert foreign_spectrals_entries(str(album)) is None
    _with_spectrals(album, *names)
    assert foreign_spectrals_entries(str(album)) is None
