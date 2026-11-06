"""salmon web's read-only pages: the folder browser, the spectrals and checks jobs, the dashboard (#632, ADR 0004).

The roots are temporary folders; the albums are synthetic FLACs. sox is a fake that writes the spectrals, flac a
fake on PATH (CI has neither); the frequency analysis and the file checks run for real. No tracker client may be
built and nothing is sent anywhere: the network guard stands, and the tracker classes refuse to be made.
"""

import asyncio
import os
import shutil
import stat
import sys
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from aiohttp.test_utils import TestClient
from helpers.checks_all_command import FAKE_FLAC, _snapshot
from helpers.checks_mqa import _write_flac
from helpers.uploader_frequency import RATE, music_like, write_flac
from helpers.webui_jobs import (
    AUTH,
    _answer,
    _finished,
    _question,
    _until,
    _with_app,
)
from helpers.webui_pages import Roots, _album, _refused_paths

import salmon.commands as commands
import salmon.config
import salmon.trackers
from salmon import cfg, interaction
from salmon.config import _parse_config, compat
from salmon.config.validations import GazelleTrackerSettings, Seedbox
from salmon.tagger.audio_info import gather_audio_info
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import spectrals
from salmon.webui import jobs, kinds, paths
from salmon.webui.jobs import FINISHED, JobManager

SECRET = "planted-red-api-key-5e1d"


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture(autouse=True)
def no_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test, or the job, if a tracker client is made."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a read-only page made a tracker client")

    monkeypatch.setattr(salmon.trackers, "get_class", refuse)
    monkeypatch.setattr(BaseGazelleApi, "__init__", refuse)


@pytest.fixture(autouse=True)
def fake_tools(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """flac on PATH, and sox writing the images it is asked for."""
    bin_dir = tmp_path_factory.mktemp("bin")
    script = bin_dir / "flac"
    script.write_text(f"#!{sys.executable}\n{FAKE_FLAC}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    run_process = spectrals.anyio.run_process

    async def fake_sox(args: list[str], **kwargs: Any) -> Any:
        if args[0] != "sox":
            return await run_process(args, **kwargs)
        for i, arg in enumerate(args):
            if arg == "-o":
                Path(args[i + 1]).write_bytes(b"\x89PNG spectral")
        return None

    monkeypatch.setattr(spectrals.anyio, "run_process", fake_sox)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Roots:
    found = Roots(tmp_path / "downloads", tmp_path / "library", tmp_path / "outside", tmp_path / "scratch")
    for folder in (found.downloads, found.library, found.outside, found.scratch):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(found.downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(found.library)])
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(found.scratch))
    return found


Test = Callable[[TestClient, JobManager], Awaitable[None]]


async def _post_job(client: TestClient, kind: str, path: str, **body: Any) -> Any:
    params = {"path": path, **body.pop("params", {})}
    return await client.post("/api/jobs", json={"kind": kind, "params": params, **body}, headers=AUTH)


async def _start(client: TestClient, kind: str, path: str, **body: Any) -> str:
    response = await _post_job(client, kind, path, **body)
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


async def _browse(client: TestClient, path: str | None = None, **extra: str) -> Any:
    params = {"path": path, **extra} if path is not None else dict(extra)
    return await client.get("/api/browse", params=params, headers=AUTH)


# --- The album folder rule ------------------------------------------------------------------


def test_the_album_folder_rule_refuses_each_folder_it_must(roots: Roots) -> None:
    for case, (path, status) in _refused_paths(roots).items():
        with pytest.raises(paths.PathRefused) as refused:
            paths.album_folder(path)
        assert refused.value.status == status, case
    assert paths.album_folder(str(roots.downloads / "Album")) == str(roots.downloads / "Album")
    assert paths.album_folder(f"{roots.downloads}/empty/../Album") == str(roots.downloads / "Album")


def test_a_folder_holding_a_library_is_refused(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    inner = roots.library / "inner"
    _album(inner / "nested" / "Album")
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(roots.library), str(inner / "nested")])
    with pytest.raises(paths.PathRefused) as refused:
        paths.album_folder(str(inner))
    assert refused.value.status == 403
    assert "holds a library" in refused.value.detail


def test_a_folder_holding_a_link_is_refused_naming_the_link_not_what_it_leads_to(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")
    (roots.outside / "secret.txt").write_text("not the album's")
    (album / "Scans").mkdir()
    (album / "Scans" / "back.jpg").symlink_to(roots.outside / "secret.txt")

    with pytest.raises(paths.PathRefused) as refused:
        paths.album_folder(str(album))

    assert refused.value.status == 403
    assert os.path.join("Scans", "back.jpg") in refused.value.detail
    assert str(roots.outside) not in refused.value.detail


def test_an_album_with_a_hardlinked_file_is_accepted(roots: Roots) -> None:
    # A hardlink is the usual seeding layout: the same file, reached by a path inside the roots.
    album = _album(roots.downloads / "Album", tracks=1)
    os.link(album / "01 Track 1.flac", album / "02 Track 2.flac")

    assert paths.album_folder(str(album)) == str(album)


def test_a_folder_of_torrents_or_a_seedboxs_is_refused_and_so_is_one_holding_it(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _album(roots.downloads / "Holder")
    torrents = _album(holder / "torrents")
    seedbox = roots.downloads / "seedbox"
    _album(seedbox / "Album")
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(torrents))
    monkeypatch.setattr(cfg, "seedbox", [Seedbox(name="box", type="local", directory=str(seedbox))])

    for folder in (torrents, _album(torrents / "Album"), holder, seedbox / "Album", seedbox):
        with pytest.raises(paths.PathRefused) as refused:
            paths.album_folder(str(folder))
        assert refused.value.status == 403, folder
        assert "folder of .torrent files or a seedbox's" in refused.value.detail, folder


@pytest.mark.parametrize("where", ["download_directory", "above it"])
def test_albums_beside_the_torrents_in_a_root_are_accepted(
    roots: Roots, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    # dottorrents_dir set to download_directory, or a folder holding it: the .torrent files are beside the albums.
    album = _album(roots.downloads / "Album")
    torrents = roots.downloads if where == "download_directory" else roots.downloads.parent
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(torrents))

    assert paths.album_folder(str(album)) == str(album)


@pytest.mark.parametrize(
    ("entry", "link"),
    [
        (SimpleNamespace(is_symlink=lambda: True), True),
        (
            SimpleNamespace(
                is_symlink=lambda: False, stat=lambda follow_symlinks: SimpleNamespace(st_reparse_tag=0xA0000003)
            ),
            True,
        ),
        (
            SimpleNamespace(
                is_symlink=lambda: False, stat=lambda follow_symlinks: SimpleNamespace(st_reparse_tag=0xA000000C)
            ),
            False,
        ),
        (SimpleNamespace(is_symlink=lambda: False, stat=lambda follow_symlinks: SimpleNamespace()), False),
    ],
    ids=["a symlink", "a Windows junction", "another reparse point", "a plain entry"],
)
def test_a_link_is_a_symlink_or_a_junction(entry: Any, link: bool) -> None:
    assert paths._is_link(entry) is link


def test_an_entry_that_cant_be_read_counts_as_a_link() -> None:
    def unreadable(follow_symlinks: bool) -> None:
        raise PermissionError

    entry: Any = SimpleNamespace(is_symlink=lambda: False, stat=unreadable)
    assert paths._is_link(entry)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a folder its owner can't read")
def test_an_album_with_a_folder_salmon_cant_read_is_refused(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")
    locked = album / "Scans"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(paths.PathRefused) as refused:
            paths.album_folder(str(album))
    finally:
        locked.chmod(stat.S_IRWXU)

    assert refused.value.status == 403
    assert "Scans" in refused.value.detail


def test_a_folder_holding_more_folders_than_an_album_walks_is_refused(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "MAX_ALBUM_WALK", 3)
    album = _album(roots.downloads / "Album")
    for disc in range(1, 3):
        _album(album / f"CD{disc}")

    assert paths.album_folder(str(album)) == str(album)
    _album(album / "CD3")
    with pytest.raises(paths.PathRefused) as refused:
        paths.album_folder(str(album))
    assert refused.value.status == 422
    assert "more than 3 folders" in refused.value.detail


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_only_a_regular_file_counts_as_an_albums_audio(roots: Roots) -> None:
    folder = roots.downloads / "Pipe"
    folder.mkdir()
    os.mkfifo(folder / "01 Track 1.flac")
    (folder / "02 Track 2.flac").mkdir()

    with pytest.raises(paths.PathRefused) as refused:
        paths.album_folder(str(folder))
    assert refused.value.status == 422


@pytest.mark.parametrize("kind", ["spectrals", "checks"])
def test_a_job_refuses_a_folder_whose_audio_is_two_levels_down_and_says_why(roots: Roots, kind: str) -> None:
    _album(roots.downloads / "incoming" / "Store" / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post_job(client, kind, str(roots.downloads / "incoming"))
        assert response.status == 422
        assert (await response.json())["detail"] == (
            "No audio file in this folder or its disc folders, but there are some deeper down. Pick the album folder "
            "that holds them."
        )
        assert manager.jobs == {}

    _with_app(test)


@pytest.mark.parametrize("kind", ["spectrals", "checks"])
def test_a_job_accepts_a_box_set_whose_audio_is_in_its_disc_folders(roots: Roots, kind: str) -> None:
    box = roots.downloads / "Box"
    _album(box / "CD1", tracks=1)
    _album(box / "CD2", tracks=1)

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        job_id = await _start(client, kind, str(box))
        job = await (await client.get(f"/api/jobs/{job_id}", headers=AUTH)).json()
        assert job["params"]["path"] == str(box)
        await client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=AUTH)

    _with_app(test)


class _AudioLast:
    """os.scandir with the audio files last, whatever order the disk gives: an album read only in part misses them."""

    def __init__(self, path: Any = ".") -> None:
        with _real_scandir(path) as entries:
            self._entries = sorted(entries, key=lambda entry: entry.name.endswith(".flac"))

    def __iter__(self) -> "_AudioLast":
        return self

    def __next__(self) -> os.DirEntry[str]:
        if not self._entries:
            raise StopIteration
        return self._entries.pop(0)

    def __enter__(self) -> "_AudioLast":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._entries = []


_real_scandir = os.scandir


def test_a_job_reads_an_album_folder_whole(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    album = roots.downloads / "Album"
    album.mkdir()
    # More than the picker's MAX_MARK_SCAN, the track last: a job reads past both bounds.
    for scan in range(300):
        (album / f"scan{scan:03d}.jpg").write_bytes(b"jpeg")
    _write_flac(album / "zz track.flac", mqa_marker=False)
    monkeypatch.setattr(paths.os, "scandir", _AudioLast)
    # Far fewer than the folder's entries: the folders walked, not the entries of the folder itself.
    monkeypatch.setattr(paths, "MAX_ALBUM_WALK", 1)

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        job_id = await _start(client, "checks", str(album))
        await client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=AUTH)

    _with_app(test)


def test_the_picker_offers_exactly_the_folders_a_job_accepts(roots: Roots) -> None:
    _album(roots.downloads / "Album")
    _album(roots.downloads / "Box" / "CD1", tracks=1)
    _album(roots.downloads / "incoming" / "Store" / "Album")
    (roots.downloads / "Scans").mkdir()
    # A disc folder that is a symlink is not looked into, as salmon's own file gathering (os.walk) does not.
    (roots.downloads / "Linked").mkdir()
    (roots.downloads / "Linked" / "CD1").symlink_to(_album(roots.outside / "Album"))

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        answer = await (await _browse(client, str(roots.downloads), sort="name")).json()
        offered = {folder["name"]: folder["audio"] for folder in answer["folders"]}
        assert offered == {"Album": True, "Box": True, "incoming": False, "Linked": False, "Scans": False}
        for name, audio in offered.items():
            response = await _post_job(client, "checks", str(roots.downloads / name))
            assert (response.status == 201) is audio, (name, await response.text())
            if audio:
                await client.post(f"/api/jobs/{(await response.json())['id']}/cancel", json={}, headers=AUTH)

    _with_app(test)


def _accepted(path: Path) -> bool:
    try:
        paths.album_folder(str(path))
    except paths.PathRefused:
        return False
    return True


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a folder its owner can't read")
def test_select_is_offered_on_a_folder_only_when_a_job_accepts_it(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Album-shaped folders a job refuses all the same: holding a link, a library root in download_directory,
    # a local seedbox's folder and the albums in it, more folders than a job walks, a folder salmon can't read.
    _refused_paths(roots)
    _album(roots.downloads / "Box" / "CD1", tracks=1)
    _album(roots.downloads / "Box" / "CD2", tracks=1)
    _album(roots.downloads / "lib" / "Kept")
    seedbox = roots.downloads / "seedbox"
    _album(seedbox / "Album")
    for disc in range(1, 4):
        _album(roots.downloads / "Big" / f"CD{disc}", tracks=1)
    locked = _album(roots.downloads / "Locked") / "Scans"
    locked.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(roots.library), str(roots.downloads / "lib")])
    monkeypatch.setattr(cfg, "seedbox", [Seedbox(name="box", type="local", directory=str(seedbox))])
    monkeypatch.setattr(paths, "MAX_ALBUM_WALK", 3)
    folders = [
        Path(top) / name
        for top, names, _files in os.walk(roots.downloads)
        for name in names
        if not (Path(top) / name).is_symlink()
    ]
    accepted = {folder: _accepted(folder) for folder in folders if folder != locked}
    locked.chmod(0)
    accepted[roots.downloads / "Locked"] = _accepted(roots.downloads / "Locked")
    assert accepted[roots.downloads / "Box"] and accepted[roots.downloads / "Album"]

    async def test(client: TestClient, _manager: JobManager) -> None:
        differ = []
        for folder, accepts in accepted.items():
            parent = await (await _browse(client, str(folder.parent))).json()
            row = next(found for found in parent["folders"] if found["name"] == folder.name)
            own = await (await _browse(client, str(folder))).json()
            if (row["audio"], own["audio"]) != (accepts, accepts):
                differ.append((str(folder.relative_to(roots.downloads)), accepts, row["audio"], own["audio"]))
        assert differ == []

    try:
        _with_app(test)
    finally:
        locked.chmod(stat.S_IRWXU)


def test_a_listed_folder_holding_audio_that_a_job_refuses_says_why(roots: Roots) -> None:
    refused = _refused_paths(roots)

    async def test(client: TestClient, _manager: JobManager) -> None:
        linked = await (await _browse(client, refused["an audio file linked out of the roots"][0])).json()
        assert linked["audio"] is False
        assert linked["refused"] == (await (await _summary(client, linked["path"])).json())["detail"]
        assert "holds a link" in linked["refused"]
        # No audio in it or its disc folders: nothing to explain, as for the folders on the way to an album.
        for folder in (roots.downloads / "incoming", roots.downloads / "empty", roots.downloads):
            assert (await (await _browse(client, str(folder))).json())["refused"] is None, folder
        assert (await (await _browse(client, str(roots.downloads / "Album"))).json())["refused"] is None

    _with_app(test)


def test_a_listing_walks_its_albums_for_links_only_so_far(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("A", "B", "C"):
        album = _album(roots.downloads / name, tracks=1)
        (album / "Scans").symlink_to(roots.outside)
    monkeypatch.setattr(paths, "MAX_OFFER_WALK", 2)

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(roots.downloads), sort="name")).json()
        # A and B walked (one folder each) and refused; C past the bound, offered: Start's check refuses it.
        assert [(f["name"], f["audio"]) for f in answer["folders"]] == [("A", False), ("B", False), ("C", True)]

    _with_app(test)


def test_browsing_a_removed_folder_lists_its_parent_and_says_which_it_opened(roots: Roots) -> None:
    album = _album(roots.downloads / "incoming" / "Album")
    _album(roots.downloads / "incoming" / "Next")

    async def test(client: TestClient, _manager: JobManager) -> None:
        here = await (await _browse(client, str(album))).json()
        assert here["path"] == str(album)
        assert here["missing"] is None
        shutil.rmtree(album)
        response = await _browse(client, str(album))
        assert response.status == 200
        answer = await response.json()
        assert answer["path"] == str(roots.downloads / "incoming")
        assert answer["missing"] == str(album)
        assert [f["name"] for f in answer["folders"]] == ["Next"]

    _with_app(test)


def test_browsing_a_path_whose_parents_are_all_gone_opens_the_root(roots: Roots) -> None:
    gone = roots.library / "a" / "b" / "c"

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(gone))).json()
        assert answer["path"] == str(roots.library)
        assert answer["missing"] == str(gone)
        assert answer["parent"] is None
        assert answer["library"] is True

    _with_app(test)


async def _summary(client: TestClient, path: str) -> Any:
    return await client.get("/api/folder", params={"path": path}, headers=AUTH)


def test_the_folder_summary_counts_the_audio_files_and_the_folders_holding_them(roots: Roots) -> None:
    box = roots.downloads / "Box"
    _album(box / "CD1", tracks=2)
    _album(box / "CD2", tracks=3)
    (box / "Scans").mkdir()
    (box / "Scans" / "front.jpg").write_bytes(b"jpeg")
    album = _album(roots.library / "Album", tracks=1)
    before = _snapshot(roots.downloads)

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await _summary(client, str(box))
        assert response.status == 200
        assert await response.json() == {"path": str(box), "summary": "5 audio files in 2 folders"}
        single = await (await _summary(client, f"{roots.library}/Scans/../Album")).json()
        assert single == {"path": str(album), "summary": "1 audio file in 1 folder"}

    _with_app(test)
    assert _snapshot(roots.downloads) == before


def test_the_folder_summary_refuses_a_folder_of_more_folders_than_an_album_walks(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = roots.downloads / "Box"
    for disc in range(3):
        _album(box / f"CD{disc}", tracks=1)
    monkeypatch.setattr(paths, "MAX_ALBUM_WALK", 3)

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await _summary(client, str(box))
        assert response.status == 422
        assert "more than 3 folders" in (await response.json())["detail"]

    _with_app(test)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_the_picker_and_the_folder_summary_count_only_regular_files_as_audio(roots: Roots) -> None:
    album = _album(roots.downloads / "Album", tracks=1)
    os.mkfifo(album / "02 Track 2.flac")
    disc = roots.downloads / "Pipes" / "CD1"
    disc.mkdir(parents=True)
    os.mkfifo(disc / "01 Track 1.flac")

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _summary(client, str(album))).json()
        assert answer["summary"] == "1 audio file in 1 folder"
        listed = await (await _browse(client, str(roots.downloads), sort="name")).json()
        assert {folder["name"]: folder["audio"] for folder in listed["folders"]} == {"Album": True, "Pipes": False}

    _with_app(test)


def test_the_folder_summary_refuses_what_a_job_refuses_with_the_same_reason(roots: Roots) -> None:
    refused = _refused_paths(roots)

    async def test(client: TestClient, manager: JobManager) -> None:
        for case, (path, status) in refused.items():
            response = await _summary(client, path)
            job = await _post_job(client, "checks", path)
            assert response.status == status == job.status, case
            assert (await response.json())["detail"] == (await job.json())["detail"], case
        assert manager.jobs == {}

    _with_app(test)


@pytest.mark.parametrize("kind", ["spectrals", "checks"])
def test_each_job_kind_refuses_those_folders_before_it_starts(roots: Roots, kind: str) -> None:
    refused = _refused_paths(roots)

    async def test(client: TestClient, manager: JobManager) -> None:
        for case, (path, status) in refused.items():
            response = await _post_job(client, kind, path)
            assert response.status == status, case
            assert (await response.json())["detail"], case
        assert manager.jobs == {}

    _with_app(test)


@pytest.mark.parametrize("kind", ["spectrals", "checks"])
def test_a_folder_changed_while_its_job_waited_is_checked_again(roots: Roots, kind: str) -> None:
    album = _album(roots.downloads / "Album")
    _album(roots.outside / "Elsewhere")

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        job_id = await _start(client, kind, str(album))
        # Swapped for a link out of the roots while the job waits its turn.
        album.rename(roots.downloads / "moved")
        album.symlink_to(roots.outside)
        manager.max_jobs = 1
        manager._schedule()
        job = await _finished(manager, job_id)
        assert job.status == "failed"
        assert "outside download_directory and library_dirs" in (job.error or "")

    _with_app(test)
    assert os.listdir(roots.scratch) == []


# --- The folder browser ---------------------------------------------------------------------


def test_the_browser_starts_at_the_roots(roots: Roots) -> None:
    _album(roots.library / "Kept")

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await _browse(client)
        assert response.status == 200
        answer = await response.json()
        assert answer["path"] is None
        assert [(f["path"], f["library"]) for f in answer["folders"]] == [
            (str(roots.downloads), False),
            (str(roots.library), True),
        ]
        assert answer["roots"] == [
            {"path": str(roots.downloads), "name": "downloads", "library": False},
            {"path": str(roots.library), "name": "library", "library": True},
        ]

    _with_app(test)


def test_the_browser_lists_folders_and_marks_those_holding_audio(roots: Roots) -> None:
    _album(roots.downloads / "b Album")
    _album(roots.downloads / "A Box" / "CD1")
    (roots.downloads / "c Covers").mkdir()
    (roots.downloads / ".hidden").mkdir()
    (roots.downloads / "z link").symlink_to(roots.outside)
    (roots.downloads / "loose.flac").write_bytes(b"fLaC")
    before = _snapshot(roots.downloads)

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(roots.downloads), sort="name")).json()
        assert answer["path"] == str(roots.downloads)
        assert answer["sort"] == "name"
        assert answer["parent"] is None
        assert answer["audio"] is False  # A root is never an album.
        assert answer["truncated"] is False
        assert [(f["name"], f["audio"]) for f in answer["folders"]] == [
            ("A Box", True),
            ("b Album", True),
            ("c Covers", False),
        ]
        inside = await (await _browse(client, str(roots.downloads / "A Box"))).json()
        assert inside["parent"] == str(roots.downloads)
        assert inside["library"] is False

    _with_app(test)
    assert _snapshot(roots.downloads) == before


def test_the_roots_view_offers_no_root_as_an_album(roots: Roots) -> None:
    _album(roots.downloads / "Album")
    _album(roots.library / "Kept")

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client)).json()
        assert [(f["path"], f["audio"]) for f in answer["folders"]] == [
            (str(roots.downloads), False),
            (str(roots.library), False),
        ]

    _with_app(test)


@pytest.mark.parametrize("root", ["downloads", "library"])
def test_a_listed_root_is_no_album_but_the_folders_in_it_are(roots: Roots, root: str) -> None:
    base = getattr(roots, root)
    _album(base / "Album")
    _album(base / "Box" / "CD1", tracks=1)
    _album(base / "Box" / "CD2", tracks=1)

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(base), sort="name")).json()
        assert answer["audio"] is False
        assert [(f["name"], f["audio"]) for f in answer["folders"]] == [("Album", True), ("Box", True)]
        inside = await (await _browse(client, str(base / "Box"))).json()
        assert inside["audio"] is True

    _with_app(test)


def test_a_long_listing_is_cut_and_says_so(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(5):
        (roots.downloads / f"folder {number}").mkdir()
    monkeypatch.setattr(paths, "MAX_LISTED", 3)

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(roots.downloads), sort="name")).json()
        assert [f["name"] for f in answer["folders"]] == ["folder 0", "folder 1", "folder 2"]
        assert answer["truncated"] is True

    _with_app(test)


def _times(monkeypatch: pytest.MonkeyPatch, times: dict[str, tuple[float, float]]) -> None:
    """Give each named folder this (mtime, ctime) as the listing's stat sees them; any other entry is stat-ed for real.

    A real ``os.utime`` cannot do it: it sets the ctime to now. A negative mtime makes the stat fail.
    """
    real = paths._changed

    class Stamped:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.entry = entry

        def stat(self, follow_symlinks: bool = True) -> Any:
            if self.entry.name not in times:
                return self.entry.stat(follow_symlinks=follow_symlinks)
            mtime, ctime = times[self.entry.name]
            if mtime < 0:
                raise FileNotFoundError(self.entry.path)
            return SimpleNamespace(st_mtime=mtime, st_ctime=ctime)

    monkeypatch.setattr(paths, "_changed", lambda entry: real(Stamped(entry)))  # pyright: ignore[reportArgumentType]


def _make(roots: Roots, *names: str) -> None:
    for name in names:
        (roots.downloads / name).mkdir()


def test_the_browser_lists_the_newest_folders_first_unless_asked_for_names(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Names and ages disagree, so neither order is the other's by chance; the last two share a time: ties by name.
    _make(roots, "b middle", "a oldest", "c newest", "e tie", "d tie")
    _times(
        monkeypatch,
        {
            "b middle": (500, 500),
            "a oldest": (100, 100),
            "c newest": (900, 900),
            "e tie": (300, 300),
            "d tie": (300, 300),
        },
    )

    async def test(client: TestClient, _manager: JobManager) -> None:
        newest = await (await _browse(client, str(roots.downloads))).json()
        assert newest["sort"] == "newest"
        assert [f["name"] for f in newest["folders"]] == ["c newest", "b middle", "d tie", "e tie", "a oldest"]
        named = await (await _browse(client, str(roots.downloads), sort="name")).json()
        assert named["sort"] == "name"
        assert [f["name"] for f in named["folders"]] == ["a oldest", "b middle", "c newest", "d tie", "e tie"]

    _with_app(test)


def test_the_newest_folder_is_listed_in_a_folder_longer_than_the_cut(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "MAX_LISTED", 3)
    names = [f"folder {number}" for number in range(5)]
    # The last one alphabetically is the newest: by name, the cut would drop it.
    _make(roots, *names, "zz newest")
    stamps = {name: (100.0 + number, 100.0 + number) for number, name in enumerate(names)}
    _times(monkeypatch, stamps | {"zz newest": (999.0, 999.0)})

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(roots.downloads))).json()
        assert answer["truncated"] is True
        assert [f["name"] for f in answer["folders"]] == ["zz newest", "folder 4", "folder 3"]
        named = await (await _browse(client, str(roots.downloads), sort="name")).json()
        assert "zz newest" not in [f["name"] for f in named["folders"]]

    _with_app(test)


def test_a_folder_copied_in_with_an_old_mtime_counts_as_new(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    # cp -a and rsync -a keep the source's mtime; the ctime is the copy's.
    _make(roots, "a copied in", "b made earlier")
    _times(monkeypatch, {"a copied in": (10, 800), "b made earlier": (500, 500)})

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(roots.downloads))).json()
        assert [f["name"] for f in answer["folders"]] == ["a copied in", "b made earlier"]

    _with_app(test)


def test_a_folder_that_vanishes_mid_listing_sorts_last(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    _make(roots, "a gone", "b kept")
    _times(monkeypatch, {"a gone": (-1, -1), "b kept": (5, 5)})
    answer = paths.listing(str(roots.downloads))
    assert [f["name"] for f in answer["folders"]] == ["b kept", "a gone"]


def test_the_browser_refuses_a_sort_it_does_not_know(roots: Roots) -> None:
    async def test(client: TestClient, _manager: JobManager) -> None:
        for path in (str(roots.downloads), None):
            for sort in ("bogus", ""):
                response = await _browse(client, path, sort=sort)
                assert response.status == 400, (path, sort)

    _with_app(test)


def test_the_browser_refuses_every_path_out_of_the_roots(roots: Roots) -> None:
    (roots.downloads / "out").symlink_to(roots.outside)
    (roots.outside / "secret folder").mkdir()
    cases = {
        str(roots.outside): 403,
        f"{roots.downloads}/../outside": 403,
        str(roots.downloads / "out"): 403,
        "/": 403,
        "downloads": 400,
        # Missing, and outside: refused, never replaced by a folder above it.
        str(roots.outside / "gone" / "deeper"): 403,
    }

    async def test(client: TestClient, _manager: JobManager) -> None:
        for path, status in cases.items():
            response = await _browse(client, path)
            assert response.status == status, path
            assert "secret folder" not in await response.text()
        # A folder named %2e%2e is not there: its parent, the root, is listed, and nothing outside it.
        literal = await _browse(client, f"{roots.downloads}/%2e%2e/outside")
        assert (await literal.json())["path"] == str(roots.downloads)
        assert "secret folder" not in await literal.text()
        # Encoded once more in the URL, ".." is still resolved before the check.
        encoded = await client.get(f"/api/browse?path={roots.downloads}/%2e%2e/outside", headers=AUTH)
        assert encoded.status == 403

    _with_app(test)


# --- Spectrals ------------------------------------------------------------------------------


@pytest.fixture
def library_album(roots: Roots) -> Path:
    """A library album, its tracks long and loud enough for the frequency analysis to draw its plots."""
    folder = roots.library / "Artist - Album (2020) [WEB FLAC]"
    folder.mkdir()
    for number in (1, 2):
        write_flac(folder / f"{number:02d} Track.flac", music_like(seconds=4), RATE)
    return folder


def _own_folders(manager: JobManager, job_id: str) -> list[str]:
    return list(manager.jobs[job_id]._own_folders)


async def _spectrals_shown(client: TestClient, manager: JobManager, album: Path, **body: Any) -> tuple[str, Path]:
    """Start a spectrals job and wait for its images: the job's id and its own folder."""
    job_id = await _start(client, "spectrals", str(album), **body)
    question = await _question(manager, job_id)
    assert question["kind"] == "spectrals"
    (folder,) = _own_folders(manager, job_id)
    return job_id, Path(folder)


async def _tracks_as_the_page_reads_them(client: TestClient, manager: JobManager, album: Path) -> dict[str, Any]:
    """A spectrals job run to its end: its result as GET /api/jobs/{id} gives it, which the Spectrals page reads."""
    job_id = await _start(client, "spectrals", str(album))
    question = await _question(manager, job_id)
    await _answer(client, manager, job_id, True)
    job = await _finished(manager, job_id)
    assert job.status == "done", job.error
    response = await client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert response.status == 200
    return {"question": question, "result": (await response.json())["result"]}


def _spectrals_page_album(roots: Roots, monkeypatch: pytest.MonkeyPatch, names: list[str]) -> Path:
    folder = roots.library / "Artist - Album (2020) [WEB FLAC]"
    for name in names:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        write_flac(folder / name, music_like(seconds=1), RATE)
    monkeypatch.setattr(cfg.image, "default_spectral_ids", "2")
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.compression, "compress_spectrals", False)
    return folder


def _numbers_of_salmon_specs(monkeypatch: pytest.MonkeyPatch, album: Path) -> dict[str, str]:
    """The spectral numbers `salmon specs` gives this album, through the command, by its image host's arguments."""
    from_specs: dict[int, str] = {}

    async def capture(_path: str, ids: dict[int, str], **_: Any) -> dict[int, list[str]]:
        from_specs.update(ids)
        return {}

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    async def every_track(ids: dict[int, str], *_: Any, **__: Any) -> dict[int, str]:
        return ids

    monkeypatch.setattr(commands, "handle_spectrals_upload_and_deletion", capture)
    monkeypatch.setattr(interaction, "show_spectrals", nothing)
    monkeypatch.setattr(spectrals, "prompt_spectrals", every_track)
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    assert commands.specs.callback is not None
    anyio.run(partial(commands.specs.callback, str(album), False, False))
    return {f"{number:02d}": name for number, name in from_specs.items()}


def _the_page_numbers_as_salmon_specs(
    roots: Roots, monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> dict[str, str]:
    album = _spectrals_page_album(roots, monkeypatch, names)
    seen: dict[str, Any] = {}

    async def test(client: TestClient, manager: JobManager) -> None:
        seen.update(await _tracks_as_the_page_reads_them(client, manager, album))

    _with_app(test)
    tracks = seen["result"]["tracks"]
    # What the page lists, each image's own number, and the default selection: all one numbering.
    assert seen["question"]["tracks"] == tracks
    assert {f"{number} Full.png" for number in tracks} <= set(seen["question"]["files"])
    assert seen["result"]["selection"] == [2]
    assert _numbers_of_salmon_specs(monkeypatch, album) == tracks
    return tracks


def test_the_spectrals_page_numbers_as_salmon_specs(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    tracks = _the_page_numbers_as_salmon_specs(roots, monkeypatch, ["CD1/01.flac", "CD2/01.flac", "CD10/01.flac"])

    assert tracks == {"01": "CD1/01.flac", "02": "CD2/01.flac", "03": "CD10/01.flac"}


def test_as_before_the_spectrals_page_numbers_as_salmon_specs_by_leading_number(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = _the_page_numbers_as_salmon_specs(roots, monkeypatch, ["1 a.flac", "2 b.flac", "10 c.flac"])

    assert tracks == {"01": "1 a.flac", "02": "2 b.flac", "03": "10 c.flac"}


def test_a_number_on_the_spectrals_page_is_the_file_the_upload_form_makes(
    roots: Roots, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    album = _spectrals_page_album(roots, monkeypatch, ["1 a.flac", "2 b.flac", "10 c.flac"])
    seen: dict[str, Any] = {}

    async def test(client: TestClient, manager: JobManager) -> None:
        seen.update(await _tracks_as_the_page_reads_them(client, manager, album))

    _with_app(test)
    form = kinds.UploadParams(path=str(album), source="WEB", spectrals=[2])
    out = tmp_path / "from the form"
    out.mkdir()

    made = anyio.run(
        spectrals.generate_spectrals_ids,
        str(album),
        kinds._up_options(form, ())["spectrals"],
        str(out),
        gather_audio_info(str(album)),
    )

    assert list(made.values()) == [seen["result"]["tracks"]["02"]] == ["2 b.flac"]


def test_spectrals_are_made_outside_a_library_album_and_served_until_discarded(
    roots: Roots, library_album: Path
) -> None:
    before = _snapshot(library_album)
    (roots.scratch / "keep").mkdir()

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id, folder = await _spectrals_shown(client, manager, library_album)
        assert folder.parent == roots.scratch
        files = (await _question(manager, job_id))["files"]
        assert {"01 Full.png", "01 Zoom.png", "02 Full.png", "02 Zoom.png", "01 Spectrum.png"} <= set(files)
        assert sorted(os.listdir(folder)) == sorted(files)
        image = await client.get(f"/api/jobs/{job_id}/spectrals/01 Full.png", headers=AUTH)
        assert image.status == 200
        assert await image.read() == b"\x89PNG spectral"
        await _answer(client, manager, job_id, True)
        job = await _finished(manager, job_id)
        assert job.status == "done", job.error
        assert job.result["tracks"] == {"01": "01 Track.flac", "02": "02 Track.flac"}
        assert any("Frequency analysis" in line["text"] for line in job.log)
        # Still there once the job is done: the user may look at them again.
        assert (await client.get(f"/api/jobs/{job_id}/spectrals/01 Zoom.png", headers=AUTH)).status == 200

        response = await client.post(f"/api/jobs/{job_id}/discard", json={}, headers=AUTH)
        assert response.status == 200
        assert not folder.exists()
        assert (await client.get(f"/api/jobs/{job_id}/spectrals/01 Full.png", headers=AUTH)).status == 404
        assert manager.jobs[job_id].summary()["spectrals"] is None

    _with_app(test)
    assert _snapshot(library_album) == before
    assert sorted(os.listdir(roots.scratch)) == ["keep"]


def test_without_tmp_dir_spectrals_go_to_a_temporary_folder(
    roots: Roots, library_album: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    before = _snapshot(library_album)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id, folder = await _spectrals_shown(client, manager, library_album)
        assert not cfg.directory.protects(str(folder))
        assert os.path.commonpath([folder, roots.downloads]) != str(roots.downloads)
        await _answer(client, manager, job_id, True)
        await _finished(manager, job_id)
        await client.post(f"/api/jobs/{job_id}/discard", json={}, headers=AUTH)
        assert not folder.exists()

    _with_app(test)
    assert _snapshot(library_album) == before


def test_a_running_job_cannot_be_discarded(roots: Roots, library_album: Path) -> None:
    async def test(client: TestClient, manager: JobManager) -> None:
        job_id, folder = await _spectrals_shown(client, manager, library_album)
        response = await client.post(f"/api/jobs/{job_id}/discard", json={}, headers=AUTH)
        assert response.status == 409
        assert folder.is_dir()
        await _answer(client, manager, job_id, True)
        await _finished(manager, job_id)

    _with_app(test)


def test_a_job_leaving_the_history_takes_its_spectrals_with_it(
    roots: Roots, library_album: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "MAX_FINISHED_JOBS", 0)

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id, folder = await _spectrals_shown(client, manager, library_album)
        question = await _question(manager, job_id)
        answer = {"question_id": question["id"], "value": True}
        assert (await client.post(f"/api/jobs/{job_id}/answer", json=answer, headers=AUTH)).status == 200
        await _until(lambda: job_id not in manager.jobs)
        await _until(lambda: not folder.exists())

    _with_app(test)
    assert os.listdir(roots.scratch) == []


@pytest.mark.parametrize("answered", [True, False])
def test_stopping_the_server_removes_every_jobs_spectrals(roots: Roots, library_album: Path, answered: bool) -> None:
    folders: list[Path] = []

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id, folder = await _spectrals_shown(client, manager, library_album)
        folders.append(folder)
        if answered:
            await _answer(client, manager, job_id, True)
            await _finished(manager, job_id)

    _with_app(test)
    assert folders
    assert not folders[0].exists()
    assert os.listdir(roots.scratch) == []


def test_a_folder_that_is_no_longer_the_jobs_own_is_left_alone(roots: Roots, tmp_path: Path) -> None:
    in_library = roots.library / f"{jobs.OWN_FOLDER_PREFIX}job-1-spectrals-x"
    in_library.mkdir()
    replaced = roots.scratch / f"{jobs.OWN_FOLDER_PREFIX}job-2-spectrals-y"
    replaced.symlink_to(roots.outside)
    (roots.outside / "kept").write_text("kept")
    other = roots.scratch / "someone else's"
    other.mkdir()
    jobs._remove_own_folders([str(in_library), str(replaced), str(other)])
    assert in_library.is_dir()
    assert (roots.outside / "kept").read_text() == "kept"
    assert other.is_dir()


def test_own_folder_is_for_a_job_only(roots: Roots) -> None:
    with pytest.raises(RuntimeError):
        jobs.own_folder("spectrals")
    assert os.listdir(roots.scratch) == []


# --- Checks ---------------------------------------------------------------------------------


def _rows(result: dict[str, Any]) -> dict[str, str]:
    return {row["check"]: row["verdict"] for row in result["rows"]}


def test_checks_give_the_rows_of_a_clean_album_and_change_nothing(roots: Roots) -> None:
    album = _album(roots.library / "Artist - Album (2020) [WEB FLAC]")
    before = _snapshot(album)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "checks", str(album)))
        assert job.status == "done", job.error
        rows = _rows(job.result)
        assert rows["MQA"] == "OK"
        assert rows["Integrity"] == "OK"
        assert job.result["blocking"] == 0
        assert job.result["report"] is None
        assert job.result["folder"] == album.name
        assert any("Nothing blocking" in line["text"] for line in job.log)

        reported = await _finished(manager, await _start(client, "checks", str(album), params={"report": True}))
        assert reported.result["report"].startswith(album.name)

    _with_app(test)
    assert _snapshot(album) == before


@pytest.mark.parametrize(
    ("damage", "check"),
    [
        (lambda track: _write_flac(track, mqa_marker=True), "MQA"),
        (lambda track: track.write_bytes(b"not a flac stream"), "Integrity"),
    ],
)
def test_checks_block_an_album_with_mqa_or_a_file_that_does_not_decode(
    roots: Roots, damage: Callable[[Path], None], check: str
) -> None:
    album = _album(roots.downloads / "Album")
    damage(album / "03 Track 3.flac")
    before = _snapshot(album)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "checks", str(album)))
        assert job.status == "done", job.error
        assert _rows(job.result)[check] == "BLOCK"
        assert job.result["blocking"] >= 1

    _with_app(test)
    assert _snapshot(album) == before


@pytest.mark.parametrize("kind", ["spectrals", "checks"])
def test_assume_defaults_and_dry_run_are_accepted(roots: Roots, library_album: Path, kind: str) -> None:
    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, kind, str(library_album), dry_run=True, assume_defaults=True)
        if kind == "spectrals":
            await _answer(client, manager, job_id, True)
        job = await _finished(manager, job_id)
        assert job.status == "done", job.error
        assert job.dry_run and job.assume_defaults

    _with_app(test)


# --- The dashboard, and secrets in GET answers -------------------------------------------------


def test_the_dashboard_shows_the_version_trackers_roots_and_jobs(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "checks", str(album)))
        assert job.status in FINISHED
        response = await client.get("/api/dashboard", headers=AUTH)
        assert response.status == 200
        answer = await response.json()
        assert answer["version"]
        assert answer["trackers"] == ["RED", "OPS"]
        assert [root["path"] for root in answer["roots"]] == [str(roots.downloads), str(roots.library)]
        assert answer["jobs"] == {"running": 0, "waiting": 0, "queued": 0, "finished": 1}

    _with_app(test)


def _config_file(tmp_path: Path, extra: str) -> Path:
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir()
    torrents.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        f'[directory]\ndownload_directory = "{music.as_posix()}"\ndottorrents_dir = "{torrents.as_posix()}"\n'
        f'[tracker.red]\nsession = "a-real-cookie"\n{extra}'
    )
    return path


def test_the_dashboard_returns_the_config_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat, "config_warnings", [])
    path = _config_file(tmp_path, "[upload]\nqwertyuiop = true\ndebug_tracker_connection = true\n")
    _parse_config(path)
    assert len(compat.config_warnings) == 2
    monkeypatch.setattr(salmon.config, "loaded_config_path", path)

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await client.get("/api/dashboard", headers=AUTH)).json()
        assert answer["config_warnings"] == compat.config_warnings
        assert any(line.startswith("Unknown config key upload.qwertyuiop") for line in answer["config_warnings"])
        assert any(line.startswith("upload.debug_tracker_connection is now") for line in answer["config_warnings"])
        assert answer["config_path"] == str(path)

    _with_app(test)


def test_the_dashboard_names_the_loaded_config_not_the_one_found_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _config_file(tmp_path, "")
    monkeypatch.setattr(salmon.config, "loaded_config_path", loaded)
    monkeypatch.setattr(salmon.config, "find_config_path", lambda: tmp_path / "another.toml")

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await client.get("/api/dashboard", headers=AUTH)).json()
        assert answer["config_path"] == str(loaded)

    _with_app(test)


def test_the_dashboard_has_no_config_warnings_on_a_clean_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compat, "config_warnings", [])
    _parse_config(_config_file(tmp_path, ""))

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await client.get("/api/dashboard", headers=AUTH)).json()
        assert answer["config_warnings"] == []

    _with_app(test)


def test_no_secret_leaves_in_the_dashboard_or_the_browser(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="planted-session-77aa", api_key=SECRET))
    planted = roots.downloads / f"Album {SECRET}"
    _album(planted)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(roots.library), str(roots.outside / SECRET)])
    (roots.outside / SECRET).mkdir()

    async def test(client: TestClient, _manager: JobManager) -> None:
        answers = [
            await client.get("/api/dashboard", headers=AUTH),
            await _browse(client),
            await _browse(client, str(roots.downloads)),
            await _browse(client, str(planted)),
            await _browse(client, str(planted / SECRET)),
        ]
        for response in answers:
            text = await response.text()
            assert SECRET not in text
            assert "planted-session-77aa" not in text
        assert "[REDACTED]" in await answers[0].text()

    _with_app(test)


def test_job_answers_hold_no_secret_either(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="planted-session-77aa", api_key=SECRET))
    album = _album(roots.downloads / f"Album {SECRET}")

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, "checks", str(album), params={"report": True})
        await _finished(manager, job_id)
        for path in ("/api/jobs", f"/api/jobs/{job_id}"):
            assert SECRET not in await (await client.get(path, headers=AUTH)).text()
        refused = await _post_job(client, "checks", str(roots.outside / SECRET))
        assert SECRET not in await refused.text()

    _with_app(test)


def test_a_bare_page_load_sends_nothing(roots: Roots) -> None:
    """The dashboard and the browser make no tracker client (see no_tracker) and start no job."""

    async def test(client: TestClient, manager: JobManager) -> None:
        for _ in range(3):
            assert (await client.get("/api/dashboard", headers=AUTH)).status == 200
            assert (await _browse(client)).status == 200
        assert manager.jobs == {}
        await asyncio.sleep(0)

    _with_app(test)


# --- The dashboard's tools and free space (#653) -----------------------------------------------


@pytest.fixture
def only_sox_and_flac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH holding fake sox and flac scripts and nothing else."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name in ("sox", "flac"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _dashboard() -> dict[str, Any]:
    answer: dict[str, Any] = {}

    async def test(client: TestClient, _manager: JobManager) -> None:
        response = await client.get("/api/dashboard", headers=AUTH)
        assert response.status == 200
        answer["_text"] = await response.text()
        answer.update(await response.json())

    _with_app(test)
    return answer


def test_the_dashboard_marks_the_tools_found_on_path(roots: Roots, only_sox_and_flac: Path) -> None:
    answer = _dashboard()
    assert answer["tools"]["required"] == {
        "curl": False,
        "flac": True,
        "git": False,
        "lame": False,
        "mp3val": False,
        "sox": True,
    }
    assert answer["tools"]["optional"] == {"puddletag": False, "feh": False, "rclone": False}
    assert str(only_sox_and_flac) not in answer["_text"]


def test_salmon_health_prints_the_tools_as_before(only_sox_and_flac: Path) -> None:
    from asyncclick.testing import CliRunner

    from salmon.commands import health

    result = anyio.run(partial(CliRunner().invoke, health, []))
    assert result.exit_code == 0, result.output
    expected = (
        "Required Dependencies:\ncurl ✘\nflac ✓\ngit ✘\nlame ✘\nmp3val ✘\nsox ✓\n"
        "\nOptional Dependencies:\npuddletag ✘\nfeh ✘\nrclone ✘\n"
    )
    assert result.output.endswith(expected)


def test_the_dashboard_gives_free_space_for_each_folder(roots: Roots) -> None:
    answer = _dashboard()
    assert [root["path"] for root in answer["roots"]] == [str(roots.downloads), str(roots.library)]
    for folder in (*answer["roots"], answer["tmp"]):
        assert folder["total_bytes"] > 0
        assert 0 <= folder["free_bytes"] <= folder["total_bytes"]
    assert answer["tmp"]["name"] == "scratch"
    assert str(roots.scratch) not in answer["_text"]


def test_a_removed_root_and_an_unset_tmp_dir_give_no_space(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    roots.library.rmdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    answer = _dashboard()
    assert answer["roots"][0]["free_bytes"] is not None
    assert answer["roots"][1]["free_bytes"] is None and answer["roots"][1]["total_bytes"] is None
    assert answer["tmp"] is None


def test_an_unreadable_folder_gives_no_space(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    real = shutil.disk_usage

    def usage(path: str) -> Any:
        if str(path) == str(roots.library):
            raise PermissionError("no")
        return real(path)

    monkeypatch.setattr(shutil, "disk_usage", usage)
    answer = _dashboard()
    assert answer["roots"][0]["free_bytes"] is not None
    assert answer["roots"][1]["free_bytes"] is None
    assert answer["tmp"]["free_bytes"] is not None


def test_the_tmp_dir_name_holds_no_secret(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_tmp = roots.outside / SECRET
    secret_tmp.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(secret_tmp))
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="planted-session-77aa", api_key=SECRET))
    assert SECRET not in _dashboard()["_text"]


def test_a_tmp_dir_with_a_trailing_slash_shows_only_its_folder_name(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp = roots.outside / SECRET
    tmp.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", f"{tmp}/")
    answer = _dashboard()
    assert answer["tmp"]["name"] == SECRET
    assert str(roots.outside) not in answer["_text"]


# --- The source preset ----------------------------------------------------------------------


def test_the_source_endpoint_gives_the_source_the_folder_name_says(roots: Roots) -> None:
    album = _album(roots.downloads / "Artist - Title (2020) [WEB FLAC]")
    plain = _album(roots.downloads / "Artist - Other")

    async def test(client: TestClient, _manager: JobManager) -> None:
        found = await client.get("/api/source", params={"path": str(album)}, headers=AUTH)
        assert await found.json() == {"source": "WEB", "reason": "folder name says WEB"}
        none = await client.get("/api/source", params={"path": str(plain)}, headers=AUTH)
        assert await none.json() == {"source": None, "reason": None}
        root = await client.get("/api/source", params={"path": str(roots.downloads)}, headers=AUTH)
        assert await root.json() == {"source": None, "reason": None}

    _with_app(test)


def test_the_source_endpoint_refuses_what_browse_refuses(roots: Roots) -> None:
    (roots.outside / "Album [WEB]").mkdir()
    cases = {
        str(roots.outside / "Album [WEB]"): 403,
        f"{roots.downloads}/../outside/Album [WEB]": 403,
        "relative/path": 400,
    }

    async def test(client: TestClient, _manager: JobManager) -> None:
        for path, status in cases.items():
            response = await client.get("/api/source", params={"path": path}, headers=AUTH)
            browse = await _browse(client, path)
            assert response.status == status == browse.status, path
            assert "WEB" not in await response.text()
        # The browser lists the folder above a missing one; the source of a missing folder is refused.
        missing = await client.get("/api/source", params={"path": str(roots.downloads / "missing")}, headers=AUTH)
        assert missing.status == 404

    _with_app(test)
