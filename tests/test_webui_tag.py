"""salmon web's tag job: the same tagging as salmon tag, through the HTTP API (#655, ADR 0004).

The job runs what ``salmon tag`` runs (``tagger.run_tag``), so the tests run the command on the same album with the
same answers and compare. The tagger's metadata sources, reviewer and file writers are replaced by functions that ask
the questions the real ones ask (through ``salmon.interaction``) and write a tag, as tests/test_library_dirs.py does
for the command; the staging, the folder rename and the job machinery are the real code. No tracker client may be
built and nothing is sent anywhere.
"""

import asyncio
import hashlib
import os
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import msgspec
import pytest
from aiohttp.test_utils import TestClient
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC
from test_webui_jobs import AUTH, _finished, _log, _until, _with_app  # pyright: ignore[reportMissingImports]
from test_webui_pages import _album, _refused_paths  # pyright: ignore[reportMissingImports]

import salmon.tagger
import salmon.tagger.foldername
import salmon.trackers
from salmon import cfg, interaction
from salmon.config.validations import GazelleTrackerSettings
from salmon.constants import TAG_ENCODINGS
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi
from salmon.uploader import staging
from salmon.webui import jobs, kinds  # noqa: F401 (registers the job kinds)
from salmon.webui.jobs import FINISHED, Job, JobKind, JobManager

SECRET = "planted-red-api-key-5e1d"
EDITED_TITLE = "Edited Title"
# What the questions are answered with, in the order the tag asks them: the metadata choice, the review (the title,
# then nothing more), whether to replace the folder name, whether the new name is acceptable.
ANSWERS = ["1", "t", "n", "y", "y"]
METADATA = {
    "title": "Album",
    "artists": [("Artist", "main")],
    "year": 2020,
    "source": "WEB",
    "format": "FLAC",
    "scene": False,
    "cover": None,
}


@pytest.fixture(autouse=True)
def no_learned_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the authkeys and passkeys other tests' fake trackers gave, short ones among them."""
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture(autouse=True)
def no_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test, or the job, if a tracker client is made."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a tag job made a tracker client")

    monkeypatch.setattr(salmon.trackers, "get_class", refuse)
    monkeypatch.setattr(BaseGazelleApi, "__init__", refuse)


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


class PathOnly(msgspec.Struct):
    path: str


@pytest.fixture(autouse=True)
def fake_tagger(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The seams that need audio tools, a network or a person: replaced by functions that ask the real questions.

    Returns the folder of every tag_files call, which is where the tags were written.
    """
    tagged: list[str] = []

    async def get_metadata(_path: str, _tags: dict, _rls_data: dict) -> tuple[dict, None]:
        reply = await interaction.prompt(click.style("\nWhich metadata results would you like to use?", fg="magenta"))
        if reply.lower().startswith("a"):
            raise click.Abort
        return dict(METADATA), None

    async def review(metadata: dict, *_args: Any, **_kwargs: Any) -> dict:
        while True:
            reply = await interaction.prompt(click.style("\nAre there any metadata fields to edit? [t]itle, [n]othing"))
            if reply.lower().startswith("n"):
                return metadata
            edited = await interaction.edit(metadata["title"])
            metadata["title"] = edited.strip() if edited else metadata["title"]

    async def tag_files(path: str, _tags: dict, metadata: dict, _auto_rename: bool) -> None:
        tagged.append(path)
        for name in os.listdir(path):
            if name.endswith(".flac"):
                file = FLAC(os.path.join(path, name))
                file["album"] = metadata["title"]
                file.save()

    async def rename_files(path: str, *_args: Any) -> None:
        for name in os.listdir(path):
            if name.endswith(".flac"):
                os.rename(os.path.join(path, name), os.path.join(path, name.replace(" Track", ". Track")))

    async def nothing(*_args: Any, **_kwargs: Any) -> Any:
        return {}

    def empty(*_args: Any, **_kwargs: Any) -> dict:
        return {}

    for name, fake in {
        "gather_tags": empty,
        "gather_audio_info": empty,
        "construct_rls_data": nothing,
        "get_metadata": get_metadata,
        "review_metadata_with_ai": review,
        "tag_files": tag_files,
        "download_cover_if_nonexistent": nothing,
        "check_tags": nothing,
        "rename_files": rename_files,
        "check_folder_structure": nothing,
    }.items():
        monkeypatch.setattr(salmon.tagger, name, fake)
    monkeypatch.setattr(
        salmon.tagger.foldername, "generate_folder_name", lambda metadata: f"Artist - {metadata['title']} (2020)"
    )
    # The terminal's editor: the command's runs take the edited title from here.
    monkeypatch.setattr(click, "edit", lambda *_args, **_kwargs: f"{EDITED_TITLE}\n")
    # A config with yes_all on would answer the questions the runs are meant to ask.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    return tagged


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


def _written(folder: Path) -> dict[str, dict[str, list[str]]]:
    """The tags of every FLAC under the folder, by its path relative to the folder: what a run wrote."""
    return {
        str(file.relative_to(folder)): {key: list(value) for key, value in FLAC(file).items()}
        for file in sorted(folder.rglob("*.flac"))
    }


def _names(folder: Path) -> list[str]:
    """The entries of the folder, but the run directories staging keeps there."""
    return sorted(name for name in os.listdir(folder) if name != staging.STAGING_DIR)


def _post(client: TestClient, path: str | Path, **body: Any) -> Any:
    params = {"path": str(path), "source": "WEB", **body.pop("params", {})}
    return client.post("/api/jobs", json={"kind": "tag", "params": params, **body}, headers=AUTH)


async def _start(client: TestClient, path: str | Path, **body: Any) -> str:
    response = await _post(client, path, **body)
    assert response.status == 201, await response.text()
    return (await response.json())["id"]


def _answers() -> Callable[[dict[str, Any]], Any]:
    """The answers of ANSWERS in order: an edit is answered with the edited title, any further yes or no with yes."""
    replies = iter(ANSWERS)

    def answer(question: dict[str, Any]) -> Any:
        if question["kind"] == "edit":
            return f"{EDITED_TITLE}\n"
        return "y" if question["kind"] == "confirm" else next(replies)

    return answer


async def _drive(
    client: TestClient, manager: JobManager, job_id: str, answer: Callable[[dict[str, Any]], Any] | None = None
) -> tuple[Job, list[dict[str, Any]]]:
    """Answer each question the job asks with `answer(question)` (ANSWERS by default), until it ends."""
    answer = answer or _answers()
    asked: list[dict[str, Any]] = []
    while True:
        await _until(
            lambda: (
                manager.jobs[job_id].status in FINISHED
                or (manager.jobs[job_id].question is not None and manager.jobs[job_id].question not in asked)
            )
        )
        job = manager.jobs[job_id]
        question = job.question
        if job.status in FINISHED or question is None:
            return await _finished(manager, job_id), asked
        asked.append(question)
        response = await client.post(
            f"/api/jobs/{job_id}/answer", json={"question_id": question["id"], "value": answer(question)}, headers=AUTH
        )
        assert response.status == 200, await response.text()


def _run_command(album: Path, *args: str, answers: list[str] = ANSWERS) -> Any:
    """Run `salmon tag ALBUM -s WEB` with the answers typed in, and return its result."""

    async def run() -> Any:
        return await CliRunner().invoke(
            salmon.tagger.tag, [str(album), "-s", "WEB", *args], input="".join(f"{each}\n" for each in answers)
        )

    return anyio.run(run)


# --- The same result as the command ---------------------------------------------------------------


def test_a_tag_job_tags_and_renames_as_salmon_tag_does(roots: Roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # salmon tag, in a download_directory of its own: the renamed album stays there.
    cli_downloads = tmp_path / "cli-downloads"
    cli_downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(cli_downloads))
    _album(cli_downloads / "Album")
    cli = _run_command(cli_downloads / "Album")
    assert cli.exit_code == 0, cli.output
    cli_folder = cli_downloads / f"Artist - {EDITED_TITLE} (2020)"
    # The renamed copy beside the album, which the command leaves (remove_source_dir is off).
    assert _names(cli_downloads) == ["Album", cli_folder.name]

    # The same album and answers, as a job.
    monkeypatch.setattr(cfg.directory, "download_directory", str(roots.downloads))
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        job, asked = await _drive(client, manager, await _start(client, album))
        assert job.status == "done", (job.error, _log(job))
        # The folder as it ends up, under its new name.
        assert job.result == {"folder": str(roots.downloads / cli_folder.name)}
        # Each question the command asked in the terminal, in the same order.
        shown = click.unstyle(cli.output)
        at = 0
        for question in asked:
            if question["kind"] == "edit":
                continue
            at = shown.index(click.unstyle(question["text"]).strip(), at)
        assert [question["kind"] for question in asked] == ["prompt", "prompt", "edit", "prompt", "confirm", "confirm"]
        assert not [question for question in asked if "\x1b" in question["text"]]

    _with_app(test)
    # The same folder and file names, and the same tags written.
    assert _names(roots.downloads) == _names(cli_downloads)
    for name in _names(cli_downloads):
        assert sorted(os.listdir(roots.downloads / name)) == sorted(os.listdir(cli_downloads / name))
        assert _written(roots.downloads / name) == _written(cli_downloads / name)
    assert FLAC(roots.downloads / cli_folder.name / "01. Track 1.flac")["album"] == [EDITED_TITLE]


def test_the_questions_reach_the_browser_in_order_and_an_edit_changes_the_tag(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        job, asked = await _drive(client, manager, await _start(client, album))
        assert job.status == "done", (job.error, _log(job))
        assert [(each["kind"], click.unstyle(each["text"]).strip()) for each in asked] == [
            ("prompt", "Which metadata results would you like to use?"),
            ("prompt", "Are there any metadata fields to edit? [t]itle, [n]othing"),
            ("edit", "Edit the text, then save it."),
            ("prompt", "Are there any metadata fields to edit? [t]itle, [n]othing"),
            ("confirm", "Would you like to replace the original folder name?"),
            ("confirm", "Is the new folder name acceptable? ([n] to edit)"),
        ]
        # The edit sends the text and gets it back.
        assert asked[2]["initial"] == "Album"

    _with_app(test)
    assert FLAC(roots.downloads / f"Artist - {EDITED_TITLE} (2020)" / "01. Track 1.flac")["album"] == [EDITED_TITLE]


def test_an_answer_the_edit_left_unchanged_keeps_the_tag(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")
    replies = iter(["1", "t", "n", "y", "y"])

    def answer(question: dict[str, Any]) -> Any:
        return None if question["kind"] == "edit" else next(replies)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album), answer)
        assert job.status == "done", (job.error, _log(job))
        assert job.result == {"folder": str(roots.downloads / "Artist - Album (2020)")}

    _with_app(test)
    assert FLAC(roots.downloads / "Artist - Album (2020)" / "01. Track 1.flac")["album"] == ["Album"]


def test_assuming_defaults_and_auto_rename_are_the_jobs_own(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        job_id = await _start(client, album, params={"auto_rename": True}, assume_defaults=True)
        replies = iter(["1", "n"])
        job, asked = await _drive(client, manager, job_id, lambda _question: next(replies))
        assert job.status == "done", (job.error, _log(job))
        # Only the stand-ins' own questions: the real rename asked none.
        assert [each["kind"] for each in asked] == ["prompt", "prompt"]
        assert job.assume_defaults is True

    _with_app(test)
    assert _names(roots.downloads) == ["Album", "Artist - Album (2020)"]
    assert cfg.upload.yes_all is False


# --- A library album --------------------------------------------------------------------------------


def test_a_library_album_is_tagged_on_a_copy_in_download_directory_and_left_byte_for_byte(roots: Roots) -> None:
    album = _album(roots.library / "Artist" / "Album")
    before = _tree(roots.library)
    mtimes = {file: file.stat().st_mtime_ns for file in roots.library.rglob("*")}

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album))
        assert job.status == "done", (job.error, _log(job))
        assert job.result == {"folder": str(roots.downloads / f"Artist - {EDITED_TITLE} (2020)")}
        assert "It is in library_dirs, so salmon works on a copy" in "\n".join(_log(job))

    _with_app(test)
    assert _tree(roots.library) == before
    assert {file: file.stat().st_mtime_ns for file in roots.library.rglob("*")} == mtimes
    copy = roots.downloads / f"Artist - {EDITED_TITLE} (2020)"
    assert FLAC(copy / "01. Track 1.flac")["album"] == [EDITED_TITLE]
    assert set(os.listdir(roots.downloads)) == {staging.STAGING_DIR, copy.name}
    assert os.listdir(roots.downloads / staging.STAGING_DIR) == []


# --- The form -------------------------------------------------------------------------------------


def test_the_job_passes_the_options_of_the_form_to_the_tagger(
    roots: Roots, monkeypatch: pytest.MonkeyPatch, fake_tagger: list[str]
) -> None:
    album = _album(roots.downloads / "Album")
    seen: list[tuple[Any, ...]] = []

    async def rls_data(_tags: dict, _info: dict, source: str, encoding: str | None, *, overwrite: bool = False) -> dict:
        seen.append((source, encoding, overwrite))
        return {}

    async def review(metadata: dict, *_args: Any, skip_initial_review: bool, apply_suggestions: bool) -> dict:
        seen.append((skip_initial_review, apply_suggestions))
        return metadata

    async def get_metadata(*_args: Any) -> tuple[dict, None]:
        return dict(METADATA), None

    monkeypatch.setattr(salmon.tagger, "construct_rls_data", rls_data)
    monkeypatch.setattr(salmon.tagger, "review_metadata_with_ai", review)
    monkeypatch.setattr(salmon.tagger, "get_metadata", get_metadata)
    params = {
        "source": "cd",
        "encoding": "v0",
        "overwrite": True,
        "auto_rename": True,
        "skip_initial_review": True,
        "apply_ai_suggestions": True,
    }

    async def test(client: TestClient, manager: JobManager) -> None:
        job, _asked = await _drive(client, manager, await _start(client, album, params=params), lambda _q: "y")
        assert job.status == "done", (job.error, _log(job))
        # The names the command shows for them, as it gives them to the tagger.
        assert job.summary()["params"]["source"] == "CD"
        assert job.summary()["params"]["encoding"] == "V0"

    _with_app(test)
    assert seen == [("CD", TAG_ENCODINGS["V0"], True), (True, True)]


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"source": "Blu-Ray"}, "Blu-Ray is not a valid source. Possible sources are: "),
        ({"encoding": "FLAC"}, "FLAC is not a valid encoding."),
    ],
)
def test_a_source_or_encoding_the_command_refuses_is_refused_before_the_job_starts(
    roots: Roots, params: dict[str, str], detail: str
) -> None:
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post(client, album, params=params)
        assert response.status == 400
        assert (await response.json())["detail"].startswith(detail)
        assert manager.jobs == {}

    _with_app(test)


def test_the_source_is_required_as_the_command_requires_it(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "tag", "params": {"path": str(album)}}, headers=AUTH)
        assert response.status == 400
        assert "source" in (await response.json())["detail"]
        assert manager.jobs == {}

    _with_app(test)
    result = anyio.run(CliRunner().invoke, salmon.tagger.tag, [str(album)])
    assert result.exit_code != 0
    assert "You must provide a source" in result.output


def test_the_command_has_no_dry_run_so_the_job_refuses_one(roots: Roots) -> None:
    album = _album(roots.downloads / "Album")
    before = _tree(roots.downloads)

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await _post(client, album, dry_run=True)
        assert response.status == 400
        assert "no dry run" in (await response.json())["detail"]
        assert manager.jobs == {}

    _with_app(test)
    assert _tree(roots.downloads) == before


# --- Folders ------------------------------------------------------------------------------------


def test_a_tag_job_refuses_the_folders_the_album_rule_refuses_before_it_starts(
    roots: Roots, fake_tagger: list[str]
) -> None:
    from test_webui_pages import Roots as PagesRoots  # pyright: ignore[reportMissingImports]

    refused = _refused_paths(PagesRoots(roots.downloads, roots.library, roots.outside, roots.outside))

    async def test(client: TestClient, manager: JobManager) -> None:
        for case, (path, status) in refused.items():
            response = await _post(client, path)
            assert response.status == status, case
            assert (await response.json())["detail"], case
        assert manager.jobs == {}

    _with_app(test)
    assert fake_tagger == []


def test_a_folder_changed_while_its_job_waited_never_starts(roots: Roots, fake_tagger: list[str]) -> None:
    album = _album(roots.downloads / "Album")
    _album(roots.outside / "Elsewhere")

    async def test(client: TestClient, manager: JobManager) -> None:
        manager.max_jobs = 0
        job_id = await _start(client, album)
        # Swapped for a link out of the roots while the job waits its turn.
        album.rename(roots.downloads / "moved")
        album.symlink_to(roots.outside)
        manager.max_jobs = 1
        manager._schedule()
        job = await _finished(manager, job_id)
        assert job.status == "failed"
        assert "outside download_directory and library_dirs" in (job.error or "")

    _with_app(test)
    assert fake_tagger == []
    assert os.listdir(roots.outside) == ["Elsewhere"]
    assert sorted(os.listdir(roots.downloads)) == ["Album", "moved"]


def test_a_tag_job_waits_while_an_upload_or_another_job_works_on_the_folder(
    roots: Roots, monkeypatch: pytest.MonkeyPatch
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
        JobKind(name="upload", params=PathOnly, run=holding, title=lambda _p: "Upload", folder=lambda p: p.path),
    )

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await client.post("/api/jobs", json={"kind": "upload", "params": {"path": str(album)}}, headers=AUTH)
        upload = (await response.json())["id"]
        waiting = await _start(client, album)
        again = await _start(client, album)
        await _until(lambda: manager.jobs[upload].status == "running")
        await asyncio.sleep(0.2)
        assert manager.jobs[waiting].status == "queued"
        assert manager.jobs[again].status == "queued"
        gate.set()
        await _finished(manager, upload)
        # The first tag job runs, and asks; the second waits for the first.
        await _until(
            lambda: manager.jobs[waiting].status in ("running", "waiting") or manager.jobs[waiting].status in FINISHED
        )
        assert manager.jobs[again].status == "queued"
        for each in (waiting, again):
            await _drive(client, manager, each)
            assert manager.jobs[each].status in FINISHED

    _with_app(test, max_jobs=3)


# --- Failures, secrets ----------------------------------------------------------------------------


def test_a_failed_tag_says_why_and_its_traceback_goes_to_the_servers_stderr(
    roots: Roots, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    album = _album(roots.downloads / "Album")

    async def failing(*_args: Any) -> None:
        raise salmon.tagger.ScrapeError("the store page changed")

    monkeypatch.setattr(salmon.tagger, "get_metadata", failing)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, album))
        assert job.status == "failed"
        assert "the store page changed" in (job.error or "")
        assert not any("Traceback" in line for line in _log(job))

    _with_app(test)
    err = capfd.readouterr().err
    assert "Traceback (most recent call last)" in err


def test_an_abort_at_a_question_ends_the_job_as_failed_and_leaves_the_album(
    roots: Roots, fake_tagger: list[str]
) -> None:
    album = _album(roots.downloads / "Album")
    before = _tree(roots.downloads)

    async def test(client: TestClient, manager: JobManager) -> None:
        job, asked = await _drive(client, manager, await _start(client, album), lambda _question: "a")
        assert (job.status, job.error) == ("failed", "Aborted.")
        assert len(asked) == 1

    _with_app(test)
    assert fake_tagger == []
    assert _tree(roots.downloads) == before


def test_no_planted_secret_is_in_any_event_of_a_tag_job(roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="planted-session-77aa", api_key=SECRET))
    album = _album(roots.downloads / f"Album {SECRET}")
    heard: list[str] = []

    async def test(client: TestClient, manager: JobManager) -> None:
        listener = manager.subscribe()
        assert listener is not None
        job_id = await _start(client, album)
        replies = iter(ANSWERS)
        job, _asked = await _drive(
            client, manager, job_id, lambda q: f"{SECRET}\n" if q["kind"] == "edit" else next(replies)
        )
        assert job.status == "done", (job.error, _log(job))
        while not listener.events.empty():
            heard.append(str(listener.events.get_nowait()))
        for path in ("/api/jobs", f"/api/jobs/{job_id}"):
            heard.append(await (await client.get(path, headers=AUTH)).text())

    _with_app(test)
    assert heard
    assert not any(SECRET in each for each in heard)
