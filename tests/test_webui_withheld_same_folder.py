"""salmon web's withheld folders go by the folder itself, not by how its path is spelled.

Each case runs twice where it can. "second mount" runs everywhere: two real folders, with os.stat saying they are one,
as a second mount of a folder does (a real bind mount needs root). "another case" is the real thing on a volume that
ignores case, and skips on a volume that doesn't.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from helpers.webui_jobs import AUTH, _with_app
from helpers.webui_pages import Roots, _album

from salmon import cfg
from salmon.config.validations import GazelleTrackerSettings, Seedbox
from salmon.trackers import base
from salmon.webui import paths
from salmon.webui.jobs import JobManager

SecondName = Callable[[Path], Path]
WITHHELD = "folder of .torrent files or a seedbox's"
WITHHELD_ALBUM = "Refusing a folder of .torrent files or a seedbox's, or one that holds one."


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget what other tests' fake trackers gave, short values among them: they would mask words in a reply."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Roots:
    found = Roots(tmp_path / "downloads", tmp_path / "library", tmp_path / "outside", tmp_path / "scratch")
    for folder in (found.downloads, found.library, found.outside, found.scratch):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(found.downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(found.library)])
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(found.scratch))
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", "")
    monkeypatch.setattr(cfg, "seedbox", [])
    return found


def _second_mount(monkeypatch: pytest.MonkeyPatch) -> SecondName:
    """Each call makes a second real folder that os.stat says is the same folder as the one given."""
    real_stat = os.stat
    seconds: dict[str, str] = {}

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        return real_stat(seconds.get(str(path)) or path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)

    def second_name(folder: Path) -> Path:
        second = folder.with_name(f"{folder.name}-second-mount")
        second.mkdir()
        seconds[str(second)] = str(folder)
        return second

    return second_name


def _another_case(folder: Path) -> Path:
    """The same folder in another case, or a skip on a volume that tells the two apart."""
    second = folder.with_name(folder.name.upper())
    assert second != folder
    if not second.exists():
        pytest.skip("this volume does not ignore case")
    return second


@pytest.fixture(params=["second mount", "another case"])
def second_name(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> SecondName:
    return _second_mount(monkeypatch) if request.param == "second mount" else _another_case


def _withhold(how: str, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if how == "dottorrents_dir":
        monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(folder))
    elif how == "a tracker's dottorrents_dir":
        red = GazelleTrackerSettings(session="made-up-session", dottorrents_dir=str(folder))
        monkeypatch.setattr(cfg.tracker, "red", red)
    else:
        monkeypatch.setattr(cfg, "seedbox", [Seedbox(name="box", type="local", directory=str(folder))])


@pytest.fixture(params=["dottorrents_dir", "a tracker's dottorrents_dir", "a local seedbox"])
def how(request: pytest.FixtureRequest) -> str:
    return request.param


def _refused_as_withheld(call: Callable[[], Any]) -> None:
    with pytest.raises(paths.PathRefused) as refused:
        call()
    assert refused.value.status == 403
    assert WITHHELD in refused.value.detail


# --- Under another name of the folder --------------------------------------------------------


def test_a_path_under_another_name_of_a_withheld_folder_is_withheld(
    roots: Roots, second_name: SecondName, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    second = second_name(dots)
    _withhold(how, dots, monkeypatch)

    assert paths._is_withheld(str(second))
    assert paths._is_withheld(str(second / "CD1" / "not there yet.jpg"))


def test_an_image_under_another_name_of_a_withheld_folder_is_refused(
    roots: Roots, second_name: SecondName, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    second = second_name(dots)
    (second / "cover.jpg").write_bytes(b"jpeg")
    _withhold(how, dots, monkeypatch)

    _refused_as_withheld(lambda: paths.image_file(str(second / "cover.jpg"), content=False))


def test_an_album_under_another_name_of_a_withheld_folder_is_refused(
    roots: Roots, second_name: SecondName, monkeypatch: pytest.MonkeyPatch
) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    second = second_name(dots)
    album = _album(second / "Album")
    _withhold("dottorrents_dir", dots, monkeypatch)

    _refused_as_withheld(lambda: paths.album_folder(str(album)))


def test_another_name_of_a_withheld_folder_is_refused_as_an_album(
    roots: Roots, second_name: SecondName, monkeypatch: pytest.MonkeyPatch
) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    second = second_name(dots)
    _album(second / "CD1")
    _withhold("dottorrents_dir", dots, monkeypatch)

    _refused_as_withheld(lambda: paths.album_folder(str(second)))


def test_an_album_holding_a_withheld_folder_under_another_name_is_refused(
    roots: Roots, second_name: SecondName, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = roots.downloads / "Holder"
    dots = holder / "dots"
    dots.mkdir(parents=True)
    second = second_name(holder)
    _album(second / "CD1")
    _withhold("dottorrents_dir", dots, monkeypatch)

    _refused_as_withheld(lambda: paths.album_folder(str(second)))


def test_the_listing_offers_no_image_under_another_name_of_a_withheld_folder(
    roots: Roots, second_name: SecondName, monkeypatch: pytest.MonkeyPatch
) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    second = second_name(dots)
    (second / "cover.jpg").write_bytes(b"jpeg")
    (second / "Sub").mkdir()
    _withhold("dottorrents_dir", dots, monkeypatch)

    shown = paths.listing(str(second), images=True)

    assert [folder["name"] for folder in shown["folders"]] == ["Sub"]
    assert shown["images"] == []


def test_a_job_under_another_name_of_a_withheld_folder_is_refused_before_it_starts(
    roots: Roots, second_name: SecondName, monkeypatch: pytest.MonkeyPatch
) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    album = _album(second_name(dots) / "Album")
    _withhold("dottorrents_dir", dots, monkeypatch)

    async def test(client: TestClient, manager: JobManager) -> None:
        body = {"kind": "checks", "params": {"path": str(album)}}
        response = await client.post("/api/jobs", json=body, headers=AUTH)
        assert response.status == 403
        assert (await response.json())["detail"] == WITHHELD_ALBUM
        assert manager.jobs == {}

    _with_app(test)


# --- A withheld folder that is, or holds, a root under another name --------------------------


@pytest.mark.parametrize("where", ["download_directory", "above it"])
def test_as_before_albums_are_accepted_when_dottorrents_dir_is_a_root_by_another_name(
    roots: Roots, second_name: SecondName, where: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    album = _album(roots.downloads / "Album")
    folder = roots.downloads if where == "download_directory" else roots.downloads.parent
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(second_name(folder)))

    assert paths.album_folder(str(album)) == str(album)


@pytest.mark.parametrize("where", ["download_directory", "above it"])
def test_a_local_seedbox_that_is_a_root_by_another_name_is_not_withheld(
    roots: Roots, second_name: SecondName, where: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    album = _album(roots.downloads / "Album")
    folder = roots.downloads if where == "download_directory" else roots.downloads.parent
    (album / "cover.jpg").write_bytes(b"jpeg")
    second = second_name(folder)
    monkeypatch.setattr(cfg, "seedbox", [Seedbox(name="box", type="local", directory=str(second))])

    assert paths.withheld_folders() == []
    assert paths.album_folder(str(album)) == str(album)
    assert paths.image_file(str(album / "cover.jpg"), content=False) == str(album / "cover.jpg")


def test_dottorrents_dir_that_is_a_root_by_another_name_withholds_its_images(
    roots: Roots, second_name: SecondName, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As it does under the root's own name: only albums are let through, the .torrent files being beside them.
    (roots.downloads / "cover.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(second_name(roots.downloads)))

    _refused_as_withheld(lambda: paths.image_file(str(roots.downloads / "cover.jpg"), content=False))


def test_as_before_dottorrents_dir_that_is_a_root_withholds_its_images(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    (roots.downloads / "cover.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(roots.downloads))

    _refused_as_withheld(lambda: paths.image_file(str(roots.downloads / "cover.jpg"), content=False))


# --- As before: the same spelling, and other folders ----------------------------------------


def test_as_before_the_withheld_folder_itself_is_refused(roots: Roots, how: str, monkeypatch) -> None:
    dots = roots.downloads / "dots"
    album = _album(dots / "Album")
    (dots / "cover.jpg").write_bytes(b"jpeg")
    _withhold(how, dots, monkeypatch)

    assert paths._is_withheld(str(dots))
    _refused_as_withheld(lambda: paths.album_folder(str(album)))
    _refused_as_withheld(lambda: paths.image_file(str(dots / "cover.jpg"), content=False))
    assert paths.listing(str(dots), images=True)["images"] == []


def test_as_before_a_folder_beside_a_withheld_one_is_accepted(roots: Roots, how: str, monkeypatch) -> None:
    dots = roots.downloads / "dots"
    dots.mkdir()
    album = _album(roots.downloads / "dots-old")
    (album / "cover.jpg").write_bytes(b"jpeg")
    _withhold(how, dots, monkeypatch)

    assert paths.album_folder(str(album)) == str(album)
    assert paths.image_file(str(album / "cover.jpg"), content=False) == str(album / "cover.jpg")
    assert [image["name"] for image in paths.listing(str(album), images=True)["images"]] == ["cover.jpg"]


def test_as_before_a_withheld_folder_not_there_yet_goes_by_its_name(roots: Roots, monkeypatch) -> None:
    dots = roots.downloads / "not there yet"
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(dots))

    assert paths._is_withheld(str(dots / "cover.jpg"))
    assert not paths._is_withheld(str(roots.downloads / "cover.jpg"))
    assert not paths._is_withheld(str(roots.downloads))


# --- The roots stay what their names say ----------------------------------------------------


def test_as_before_a_root_by_another_name_is_outside_the_roots(roots: Roots, monkeypatch) -> None:
    second = _second_mount(monkeypatch)(roots.downloads)
    album = _album(second / "Album")
    (second / "cover.jpg").write_bytes(b"jpeg")

    for call in (
        lambda: paths.album_folder(str(album)),
        lambda: paths.album_folder(str(second)),
        lambda: paths.image_file(str(second / "cover.jpg"), content=False),
        lambda: paths.browsable_folder(str(second)),
        lambda: paths.listing(str(second)),
    ):
        with pytest.raises(paths.PathRefused) as refused:
            call()
        assert refused.value.status == 403
        assert "outside download_directory and library_dirs" in refused.value.detail


def test_as_before_a_root_or_an_album_in_another_case_is_refused_as_outside(roots: Roots) -> None:
    # On every volume: the path is refused by its text, whether or not it would find the folder.
    album = _album(roots.downloads / "Album")
    other_root = roots.downloads.with_name("DOWNLOADS")

    assert paths.album_folder(str(album)) == str(album)
    for path in (other_root, other_root / "Album"):
        with pytest.raises(paths.PathRefused) as refused:
            paths.album_folder(str(path))
        assert refused.value.status == 403
        assert "outside download_directory and library_dirs" in refused.value.detail


# --- _same_or_inside ------------------------------------------------------------------------


def test_same_or_inside_goes_up_past_what_it_may_not_look_at(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    second = _second_mount(monkeypatch)(folder)
    (second / "locked").mkdir()
    locked = second / "locked" / "a"
    resolved_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == str(locked):
            raise PermissionError(13, "made up", str(path))
        return resolved_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)

    assert paths._same_or_inside(str(locked), str(folder))


def test_same_or_inside_is_false_for_another_folder(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    other = tmp_path / "other"
    folder.mkdir()
    other.mkdir()
    (other / "file").write_bytes(b"made up")

    assert not paths._same_or_inside(str(other), str(folder))
    assert not paths._same_or_inside(str(other / "file"), str(folder))
    assert not paths._same_or_inside(str(tmp_path), str(folder))
    assert not paths._same_or_inside(str(tmp_path / "gone" / "a"), str(folder))


def test_same_or_inside_is_false_when_the_folder_cannot_be_looked_at(tmp_path: Path) -> None:
    assert not paths._same_or_inside(str(tmp_path), str(tmp_path / "gone"))
