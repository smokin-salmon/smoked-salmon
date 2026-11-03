"""An album's Spectrals folder salmon did not make is uploaded only when the user says so (#902).

The runs here go against a local fake tracker and a fake image host, never a real one.
"""

from pathlib import Path

import anyio
import pytest
from helpers.uploader_dry_run import _album, _run_up, _snapshot
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from torf import Torrent

import salmon.trackers
import salmon.uploader.spectrals
from salmon import cfg, interaction
from salmon.uploader.spectrals import foreign_entries_text, foreign_spectrals_entries

pytestmark = pytest.mark.usefixtures("image_uploads")

QUESTION = "u: upload it with the album"
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


def test_the_run_asks_and_the_default_stops_it_before_any_tracker_request(monkeypatch, tmp_path, torrents) -> None:
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


def test_u_uploads_the_folder_as_before(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    before = _snapshot(specs)

    run = _run_up(monkeypatch, album, torrents, input="u\n" + ANSWERS, yes_all=False)

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


MOVED = "Spectrals (moved from Album)"


def _files_of(folder: Path) -> dict[str, bytes]:
    return {str(f.relative_to(folder)): f.read_bytes() for f in sorted(folder.rglob("*")) if f.is_file()}


def _download_dir() -> Path:
    return Path(cfg.directory.download_directory)


def test_m_moves_the_folder_to_download_directory_and_goes_on(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    contents = _files_of(specs)

    run = _run_up(monkeypatch, album, torrents, input="m\n" + ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    moved = _download_dir() / MOVED
    assert f"Moved {specs} to {moved}." in run.result.output
    assert _files_of(moved) == contents
    assert not specs.exists()
    assert "Successfully uploaded" in run.result.output
    files = _uploaded_files(run)
    assert files
    assert not [name for torrent in files for name in torrent if name.startswith("Spectrals/")]


def test_m_never_overwrites_a_folder_already_there(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    contents = _files_of(specs)
    taken = _download_dir() / MOVED
    taken.mkdir()
    (taken / "mine.txt").write_bytes(b"mine")

    run = _run_up(monkeypatch, album, torrents, input="m\n" + ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert _files_of(taken) == {"mine.txt": b"mine"}
    assert _files_of(_download_dir() / f"{MOVED} (2)") == contents
    assert not specs.exists()


def test_m_on_a_library_album_leaves_the_album_alone(monkeypatch, tmp_path, torrents) -> None:
    library = tmp_path / "library"
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    album = _album(library / "Artist" / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    before = _snapshot(album)

    run = _run_up(monkeypatch, album, torrents, input="m\n" + ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert f"Left {specs} out of this upload. The album still holds it, so a later upload asks again." in (
        run.result.output
    )
    assert "leave it out of this upload's copy" in run.result.output
    assert _snapshot(album) == before
    assert not (_download_dir() / MOVED).exists()
    files = _uploaded_files(run)
    assert files
    assert not [name for torrent in files for name in torrent if name.startswith("Spectrals/")]


def test_m_in_a_dry_run_changes_nothing_outside_the_scratch_copy(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    before = _snapshot(tmp_path / "Album")
    downloads_before = sorted(p.name for p in _download_dir().iterdir())

    run = _run_up(monkeypatch, album, torrents, args=("--dry-run",), input="m\n" + ANSWERS, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    assert "Left" in run.result.output and "a later upload asks again" in run.result.output
    assert _snapshot(tmp_path / "Album") == before
    # The scratch copy and its run directory are gone; the staging root stays, empty.
    assert sorted(p.name for p in _download_dir().iterdir() if p.name != ".salmon-staging") == downloads_before
    assert not list((_download_dir() / ".salmon-staging").iterdir())
    assert not [sent for sent in run.tracker.sent if sent.method != "GET"]


def test_the_question_has_three_options_and_stop_is_the_default(monkeypatch, tmp_path) -> None:
    album = _album(tmp_path / "Album")
    _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")
    asked: list[dict] = []

    async def prompt(text, **kwargs):
        asked.append(kwargs)
        return kwargs["default"]

    async def no_defaults() -> bool:
        return False

    monkeypatch.setattr(interaction, "prompt", prompt)
    monkeypatch.setattr(interaction, "assume_defaults", no_defaults)

    refusal = anyio.run(salmon.uploader.spectrals.foreign_spectrals_refusal, str(album), str(album))

    assert refusal is not None and refusal.startswith("Not uploading")
    (kwargs,) = asked
    assert [option.key for option in kwargs["options"].items] == ["m", "u", "s"]
    assert kwargs["default"] == "s"
    assert [option.key for option in kwargs["options"].items if option.empty] == ["s"]


def test_a_failed_move_stops_the_run_and_says_where_the_folder_is(monkeypatch, tmp_path, torrents) -> None:
    album = _album(tmp_path / "Album")
    specs = _with_spectrals(album, "01 Full.png", "01 Zoom (copy).png")

    def failing(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(salmon.uploader.spectrals.shutil, "move", failing)

    run = _run_up(monkeypatch, album, torrents, input="m\n" + ANSWERS, yes_all=False)

    assert f"Could not move {specs} to" in run.result.output
    assert f"The folder is still at {specs}." in run.result.output
    assert "Successfully uploaded" not in run.result.output
    assert run.tracker.sent == []
    assert specs.is_dir()
