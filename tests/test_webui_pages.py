"""salmon web's read-only pages: the folder browser, the spectrals and checks jobs, the dashboard (#632, ADR 0004).

The roots are temporary folders; the albums are synthetic FLACs. sox is a fake that writes the spectrals, flac a
fake on PATH (CI has neither); the frequency analysis and the file checks run for real. No tracker client may be
built and nothing is sent anywhere: the network guard stands, and the tracker classes refuse to be made.
"""

import asyncio
import os
import stat
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from mutagen.flac import FLAC
from test_checks_all_command import FAKE_FLAC, _snapshot  # pyright: ignore[reportMissingImports]
from test_checks_mqa import _write_flac  # pyright: ignore[reportMissingImports]
from test_uploader_frequency import RATE, music_like, write_flac  # pyright: ignore[reportMissingImports]
from test_webui_jobs import (  # pyright: ignore[reportMissingImports]
    AUTH,
    _answer,
    _finished,
    _question,
    _until,
    _with_app,
)

import salmon.trackers
from salmon import cfg
from salmon.config.validations import GazelleTrackerSettings
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import spectrals
from salmon.webui import jobs, paths
from salmon.webui.jobs import FINISHED, JobManager

SECRET = "planted-red-api-key-5e1d"


@dataclass
class Roots:
    downloads: Path
    library: Path
    outside: Path
    scratch: Path


def _album(folder: Path, tracks: int = 2) -> Path:
    folder.mkdir(parents=True)
    for number in range(1, tracks + 1):
        track = folder / f"{number:02d} Track {number}.flac"
        _write_flac(track, mqa_marker=False)
        tags = FLAC(track)
        tags.update({"artist": "Artist", "album": "Album", "date": "2020", "title": f"Track {number}"})
        tags["tracknumber"] = str(number)
        tags.save()
    return folder


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


async def _browse(client: TestClient, path: str | None = None) -> Any:
    return await client.get("/api/browse", params={"path": path} if path is not None else {}, headers=AUTH)


# --- The album folder rule ------------------------------------------------------------------


def _refused_paths(roots: Roots) -> dict[str, tuple[str, int]]:
    """Each folder the rule refuses, and the status it is refused with."""
    _album(roots.outside / "Album")
    album = _album(roots.downloads / "Album")
    (roots.downloads / "out").symlink_to(roots.outside / "Album")
    (roots.downloads / "linked").symlink_to(album)
    (roots.downloads / "empty").mkdir()
    (roots.library / "into").symlink_to(roots.downloads)
    return {
        "outside the roots": (str(roots.outside / "Album"), 403),
        "..": (f"{roots.downloads}/../outside/Album", 403),
        "encoded ..": (f"{roots.downloads}/%2e%2e/outside/Album", 404),
        "a symlink out of a root": (str(roots.downloads / "out"), 403),
        "a root": (str(roots.downloads), 403),
        "a library root": (str(roots.library), 403),
        "a symlinked album folder": (str(roots.downloads / "linked"), 403),
        "a folder linked into a library from outside": (str(roots.library / "into" / "Album"), 403),
        "a folder with no audio": (str(roots.downloads / "empty"), 422),
        "a relative path": ("downloads/Album", 400),
        "a missing folder": (str(roots.downloads / "missing"), 404),
    }


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
        answer = await (await _browse(client, str(roots.downloads))).json()
        assert answer["path"] == str(roots.downloads)
        assert answer["parent"] is None
        assert answer["audio"] is True
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


def test_a_long_listing_is_cut_and_says_so(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(5):
        (roots.downloads / f"folder {number}").mkdir()
    monkeypatch.setattr(paths, "MAX_LISTED", 3)

    async def test(client: TestClient, _manager: JobManager) -> None:
        answer = await (await _browse(client, str(roots.downloads))).json()
        assert [f["name"] for f in answer["folders"]] == ["folder 0", "folder 1", "folder 2"]
        assert answer["truncated"] is True

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
        f"{roots.downloads}/%2e%2e/outside": 404,
        str(roots.downloads / "missing"): 404,
    }

    async def test(client: TestClient, _manager: JobManager) -> None:
        for path, status in cases.items():
            response = await _browse(client, path)
            assert response.status == status, path
            assert "secret folder" not in await response.text()
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
