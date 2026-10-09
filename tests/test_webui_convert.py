"""salmon web's convert jobs: transcode, downconvert and compress, through the HTTP API (#654, ADR 0004).

The jobs run what ``salmon transcode``, ``salmon downconv`` and ``salmon compress`` run, so the tests run the command
on the same album and compare. The audio tools are replaced by functions that write a placeholder for each file
(the converters' own tests do the same); everything else, the output name, the extra files, the conversion record,
the mirror of a library album, is the real code. No tracker client may be built and nothing is sent anywhere.
"""

import asyncio
import hashlib
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import msgspec
import pytest
from aiohttp.test_utils import TestClient
from asyncclick.testing import CliRunner
from test_webui_jobs import AUTH, _finished, _log, _until, _with_app  # pyright: ignore[reportMissingImports]
from test_webui_pages import _album, _refused_paths  # pyright: ignore[reportMissingImports]

import salmon.commands
import salmon.converter
import salmon.trackers
from salmon import cfg
from salmon.config.validations import GazelleTrackerSettings
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc
from salmon.errors import UploadError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.webui import jobs, kinds  # noqa: F401 (registers the job kinds)
from salmon.webui.jobs import JobKind, JobManager

SECRET = "planted-red-api-key-5e1d"
KINDS = ["transcode", "downconvert", "compress"]
# What the form of each conversion gives, and the command that runs the same.
FORMS: dict[str, dict[str, Any]] = {
    "transcode": {"bitrate": "V0"},
    "downconvert": {},
}
COMMANDS: dict[str, tuple[Any, list[str]]] = {
    "transcode": (salmon.converter.transcode, ["-b", "V0"]),
    "downconvert": (salmon.converter.downconv, []),
}


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture(autouse=True)
def no_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test, or the job, if a tracker client is made."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a convert job made a tracker client")

    monkeypatch.setattr(salmon.trackers, "get_class", refuse)
    monkeypatch.setattr(BaseGazelleApi, "__init__", refuse)


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """24 bit files to downconvert, and a placeholder written in place of each converted file."""
    for module in (tc, dc):
        monkeypatch.setattr(module, "gather_audio_info", _info)
    monkeypatch.setattr(dc, "_validate_lossless", lambda _path: None)

    async def transcoded(items: list[tc.TranscodeItem], _bitrate: str) -> None:
        for item in items:
            Path(item.dst).parent.mkdir(parents=True, exist_ok=True)
            Path(item.dst).write_bytes(b"mp3 " + Path(item.src).read_bytes()[:16])

    async def converted(items: list[dc.ConvertItem], _bit_depth: int) -> None:
        for item in items:
            Path(item.dst).parent.mkdir(parents=True, exist_ok=True)
            Path(item.dst).write_bytes(b"16 bit " + Path(item.src).read_bytes()[:16])

    monkeypatch.setattr(tc, "_transcode_audio_files", transcoded)
    monkeypatch.setattr(dc, "_convert_audio_files", converted)


def _info(path: str) -> dict[str, dict[str, int]]:
    names = sorted(name for name in os.listdir(path) if name.lower().endswith(".flac"))
    return {name: {"precision": 24, "sample rate": 96000} for name in names}


class PathOnly(msgspec.Struct):
    path: str


class Roots(SimpleNamespace):
    downloads: Path
    library: Path
    outside: Path


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Roots:
    found = Roots(downloads=tmp_path / "downloads", library=tmp_path / "library", outside=tmp_path / "outside")
    for folder in (found.downloads, found.library, found.outside):
        folder.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(found.downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(found.library)])
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    return found


def _tree(*folders: Path) -> dict[str, str]:
    """Every file under the folders, and a hash of its content: what "byte for byte unchanged" compares."""
    found: dict[str, str] = {}
    for folder in folders:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as handle:
                    found[path] = hashlib.sha256(handle.read()).hexdigest()
    return found


def _post(client: TestClient, kind: str, path: str | Path, **body: Any) -> Any:
    params = {"path": str(path), **FORMS.get(kind, {}), **body.pop("params", {})}
    return client.post("/api/jobs", json={"kind": kind, "params": params, **body}, headers=AUTH)


async def _start(client: TestClient, kind: str, path: str | Path, **body: Any) -> str:
    response = await _post(client, kind, path, **body)
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


async def _run_command(kind: str, album: Path) -> Any:
    command, args = COMMANDS[kind]
    result = await CliRunner().invoke(command, [str(album), *args])
    assert result.exit_code == 0, result.output
    return result


def _clear(folder: Path, keep: Path) -> None:
    """Remove everything in the folder but `keep`: a conversion's output and its record."""
    for entry in folder.iterdir():
        if entry != keep:
            shutil.rmtree(entry)


# --- Each kind runs the command's code ---------------------------------------------------------


@pytest.mark.parametrize("kind", ["transcode", "downconvert"])
def test_a_conversion_makes_the_folder_and_the_record_the_command_makes(roots: Roots, kind: str) -> None:
    album = _album(roots.downloads / "Artist - Album (2020) [WEB 24bit FLAC]")
    (album / "cover.jpg").write_bytes(b"cover")
    (album / "notes.txt").write_bytes(b"notes")
    seen: dict[str, Any] = {}

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, kind, album))
        assert job.status == "done", job.error
        seen["job"] = (job.result, _tree(roots.downloads))
        # The same album through the command, from nothing: the output and its record go.
        _clear(roots.downloads, keep=album)
        assert _tree(roots.downloads) == _tree(album)
        await _run_command(kind, album)
        seen["command"] = _tree(roots.downloads)

    _with_app(test)
    result, from_job = seen["job"]
    assert from_job == seen["command"]
    output = next(path for path in from_job if path.endswith(".mp3" if kind == "transcode" else "01 Track 1.flac"))
    folder = os.path.dirname(output)
    assert folder != str(album)
    assert result == {"output": folder}
    assert conversions.conversion_of(folder) is not None
    assert os.path.join(os.path.dirname(folder), conversions.REGISTRY_DIR, os.path.basename(folder) + ".json") in (
        from_job
    )


def test_essential_only_leaves_the_extra_files_out_as_the_command_does(roots: Roots) -> None:
    album = _album(roots.downloads / "Album [WEB 24bit FLAC]")
    (album / "cover.jpg").write_bytes(b"cover")
    (album / "notes.txt").write_bytes(b"notes")

    async def test(client: TestClient, manager: JobManager) -> None:
        full = await _finished(manager, await _start(client, "transcode", album))
        kept = sorted(os.listdir(full.result["output"]))
        shutil.rmtree(full.result["output"])
        essential = await _finished(manager, await _start(client, "transcode", album, params={"essential_only": True}))
        assert essential.status == "done", essential.error
        assert "notes.txt" in kept
        assert "notes.txt" not in os.listdir(essential.result["output"])
        assert "cover.jpg" in os.listdir(essential.result["output"])

    _with_app(test)


@pytest.mark.parametrize("kind", ["transcode", "downconvert"])
def test_a_library_album_converts_into_download_directory_and_is_left_unchanged(roots: Roots, kind: str) -> None:
    album = _album(roots.library / "Artist" / "Album [WEB 24bit FLAC]")
    (album / "cover.jpg").write_bytes(b"cover")
    before = _tree(roots.library)
    seen: dict[str, Any] = {}

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, kind, album))
        assert job.status == "done", job.error
        seen["job"] = job.result
        seen["job_tree"] = _tree(roots.downloads)
        shutil.rmtree(roots.downloads)
        roots.downloads.mkdir()
        await _run_command(kind, album)
        seen["command_tree"] = _tree(roots.downloads)
        assert "is in library_dirs: writing the output into" in "\n".join(_log(job))

    _with_app(test)
    # Where the command puts it: the library album's parent, mirrored below download_directory.
    mirror = roots.downloads.joinpath(*Path(os.path.realpath(album)).parent.parts[1:])
    assert os.path.dirname(seen["job"]["output"]) == str(mirror)
    assert seen["job_tree"] == seen["command_tree"]
    assert seen["job_tree"]
    assert _tree(roots.library) == before


# --- compress ----------------------------------------------------------------------------------


@pytest.fixture
def recompressed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str] | None]]:
    """The calls of recompress_path, which it replaces: it would run flac."""
    calls: list[tuple[str, list[str] | None]] = []

    async def recompress(path: str, files: list[str] | None = None) -> None:
        calls.append((path, files))

    monkeypatch.setattr(salmon.commands, "recompress_path", recompress)
    return calls


def test_compress_recompresses_the_flacs_in_place_as_the_command_does(
    roots: Roots, recompressed: list[tuple[str, list[str] | None]]
) -> None:
    album = _album(roots.downloads / "Album")
    (album / "03 Track.mp3").write_bytes(b"mp3")

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "compress", album))
        assert job.status == "done", job.error
        assert job.result == {"folder": str(album), "recompressed": 2}
        await CliRunner().invoke(salmon.commands.compress, [str(album)])

    _with_app(test)
    # The job and the command made the same call.
    assert recompressed == [(str(album), ["01 Track 1.flac", "02 Track 2.flac"])] * 2


def test_compress_with_no_flac_says_what_the_command_says_and_is_done(
    roots: Roots, recompressed: list[tuple[str, list[str] | None]]
) -> None:
    album = roots.downloads / "Album"
    album.mkdir()
    (album / "01.mp3").write_bytes(b"mp3")

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "compress", album))
        assert job.status == "done", job.error
        assert job.result == {"folder": str(album), "recompressed": 0}
        assert _log(job) == ["No flacs found to recompress. Skipping..."]

    _with_app(test)
    assert recompressed == []


def test_a_failed_recompression_fails_the_job(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    album = _album(roots.downloads / "Album")

    async def failing(_path: str, files: list[str] | None = None) -> None:
        raise UploadError("Failed to recompress 1 file(s).")

    monkeypatch.setattr(salmon.commands, "recompress_path", failing)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "compress", album))
        assert job.status == "failed"
        assert "Failed to recompress 1 file(s)." in (job.error or "")

    _with_app(test)


def test_compress_refuses_a_library_album_and_a_folder_holding_one(
    roots: Roots, recompressed: list[tuple[str, list[str] | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    album = _album(roots.library / "Artist" / "Album")
    holder = roots.library / "Artist"
    before = _tree(roots.library)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post(client, "compress", album)
        assert response.status == 403
        assert (await response.json())["detail"] == f"Not recompressing {album}: it is in library_dirs, or holds one."
        # A folder holding a library is refused before, by the album folder rule.
        monkeypatch.setattr(cfg.directory, "library_dirs", [str(roots.library), str(album)])
        for each in (holder, roots.library / "Artist"):
            response = await _post(client, "compress", each)
            assert response.status == 403
        assert manager.jobs == {}

    _with_app(test)
    assert recompressed == []
    assert _tree(roots.library) == before


def test_compress_checks_the_folder_again_when_the_job_starts(
    roots: Roots, recompressed: list[tuple[str, list[str] | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    album = _album(roots.downloads / "Album")
    before = _tree(roots.downloads)

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        job_id = await _start(client, "compress", album)
        # A library of its own while the job waits its turn.
        monkeypatch.setattr(cfg.directory, "library_dirs", [str(roots.library), str(roots.downloads)])
        manager.max_jobs = 1
        manager._schedule()
        job = await _finished(manager, job_id)
        assert job.status == "failed"
        assert job.error == f"Not recompressing {album}: it is in library_dirs, or holds one."

    _with_app(test)
    assert recompressed == []
    assert _tree(roots.downloads) == before


def test_the_command_refuses_a_library_album_with_the_same_message(
    roots: Roots, recompressed: list[tuple[str, list[str] | None]]
) -> None:
    album = _album(roots.library / "Album")

    result = anyio.run(CliRunner().invoke, salmon.commands.compress, [str(album)])

    assert result.exit_code != 0
    assert f"Not recompressing {album}: it is in library_dirs, or holds one." in result.output
    assert recompressed == []


# --- Folders ------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_refuses_the_folders_the_album_rule_refuses_before_it_starts(roots: Roots, kind: str) -> None:
    from test_webui_pages import Roots as PagesRoots  # pyright: ignore[reportMissingImports]

    refused = _refused_paths(PagesRoots(roots.downloads, roots.library, roots.outside, roots.outside))

    async def test(client: TestClient, manager: JobManager) -> None:
        for case, (path, status) in refused.items():
            response = await _post(client, kind, path)
            assert response.status == status, case
            assert (await response.json())["detail"], case
        assert manager.jobs == {}

    _with_app(test)


@pytest.mark.parametrize("kind", KINDS)
def test_a_folder_changed_while_its_job_waited_never_starts(
    roots: Roots, kind: str, recompressed: list[tuple[str, list[str] | None]]
) -> None:
    album = _album(roots.downloads / "Album")
    _album(roots.outside / "Elsewhere")

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        job_id = await _start(client, kind, album)
        # Swapped for a link out of the roots while the job waits its turn.
        album.rename(roots.downloads / "moved")
        album.symlink_to(roots.outside)
        manager.max_jobs = 1
        manager._schedule()
        job = await _finished(manager, job_id)
        assert job.status == "failed"
        assert "outside download_directory and library_dirs" in (job.error or "")

    _with_app(test)
    assert recompressed == []
    assert os.listdir(roots.outside) == ["Elsewhere"]
    assert sorted(os.listdir(roots.downloads)) == ["Album", "moved"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_conversion_waits_while_an_upload_or_another_job_works_on_the_folder(
    roots: Roots, kind: str, monkeypatch: pytest.MonkeyPatch, recompressed: list[tuple[str, list[str] | None]]
) -> None:
    album = _album(roots.downloads / "Album")
    gate = threading.Event()

    async def holding(_params: Any) -> str:
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return "through"

    # What an upload of the folder is to the queue: a job on that folder.
    monkeypatch.setitem(
        jobs.KINDS,
        "upload",
        JobKind(
            name="upload",
            params=PathOnly,
            run=holding,
            title=lambda _p: "Upload",
            folder=lambda p: p.path,
        ),
    )

    async def test(client: TestClient, manager: JobManager) -> None:
        upload = await _start(client, "upload", album)
        waiting = await _start(client, kind, album)
        again = await _start(client, kind, album)
        await _until(lambda: manager.jobs[upload].status == "running")
        await asyncio.sleep(0.2)
        assert manager.jobs[waiting].status == "queued"
        assert manager.jobs[again].status == "queued"
        gate.set()
        for each in (upload, waiting, again):
            assert (await _finished(manager, each)).status in ("done", "failed")

    _with_app(test, max_jobs=3)


# --- Failures, secrets ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["transcode", "downconvert"])
def test_a_failed_conversion_says_why_and_its_traceback_goes_to_the_servers_stderr(
    roots: Roots, kind: str, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    album = _album(roots.downloads / "Album [WEB 24bit FLAC]")

    async def failing(*_args: Any) -> None:
        raise UploadError("lame failed on 01 Track 1.flac")

    monkeypatch.setattr(tc, "_transcode_audio_files", failing)
    monkeypatch.setattr(dc, "_convert_audio_files", failing)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, kind, album))
        assert job.status == "failed"
        assert "lame failed on 01 Track 1.flac" in (job.error or "")
        assert not any("Traceback" in line for line in _log(job))

    _with_app(test)
    err = capfd.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "lame failed on 01 Track 1.flac" in err


@pytest.mark.parametrize("kind", KINDS)
def test_no_planted_secret_is_in_any_event_of_a_convert_job(
    roots: Roots,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    recompressed: list[tuple[str, list[str] | None]],
) -> None:
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="planted-session-77aa", api_key=SECRET))
    album = _album(roots.downloads / f"Album {SECRET} [WEB 24bit FLAC]")
    heard: list[str] = []

    async def test(client: TestClient, manager: JobManager) -> None:
        listener = manager.subscribe()
        assert listener is not None
        job_id = await _start(client, kind, album)
        job = await _finished(manager, job_id)
        assert job.status == "done", job.error
        while not listener.events.empty():
            heard.append(str(listener.events.get_nowait()))
        for path in ("/api/jobs", f"/api/jobs/{job_id}"):
            heard.append(await (await client.get(path, headers=AUTH)).text())

    _with_app(test)
    assert heard
    assert not any(SECRET in each for each in heard)


@pytest.mark.parametrize("kind", KINDS)
def test_the_commands_have_no_dry_run_so_the_jobs_refuse_one(roots: Roots, kind: str) -> None:
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post(client, kind, album, dry_run=True)
        assert response.status == 400
        assert "no dry run" in (await response.json())["detail"]
        assert manager.jobs == {}

    _with_app(test)
