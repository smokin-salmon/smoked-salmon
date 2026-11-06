"""A source that fails at the metadata question asks the question again, a number or a URL alike (#968, part 1).

The runs go through salmon.run.main with the real get_metadata: the store search is stubbed, and Discogs, Deezer and
the Bandcamp catch-all are a fake store on 127.0.0.1 that answers 404, but for Discogs release 2. Uploads go to the
local fake tracker of helpers.uploader_dry_run. No real store or tracker is reached.
"""

import contextlib
import io
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from helpers.ssrf import FakeStore
from helpers.tagger_run_metadata import RELEASE, pointed_at
from helpers.uploader_dry_run import FakeTracker, _album, _returning_async, _write_flac, fake_upload_world
from helpers.webui_jobs import AUTH, _log, _with_app
from test_run_exit_status import (
    _exit_of_main,
    _upload_posts,
    no_waits,  # noqa: F401 (an autouse fixture)
)
from test_uploader_dry_run import (
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)
from test_webui_tag import _drive

import salmon.tagger
from salmon import cfg
from salmon.search.base import IdentData
from salmon.tagger import metadata as metadata_mod
from salmon.tagger.sources import deezer
from salmon.webui import kinds  # noqa: F401 (registers the job kinds)
from salmon.webui.jobs import JobManager

# Covers and spectrals go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

FAILING_URL = "https://www.discogs.com/release/1"
WORKING_URL = "https://www.discogs.com/release/2"
TAGGED_URL = "https://www.deezer.com/album/1"
QUESTION = "Which metadata results would you like to use?"


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, torrents


class Store(FakeStore):
    """Discogs's API, Deezer's and a Bandcamp page: release 2 of Discogs answers, everything else is a 404."""

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        if request.path == "/releases/2":
            return web.json_response(RELEASE)
        return web.json_response({"message": "Release not found."}, status=404)


async def _search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    # Two Discogs results that match nothing in the tags, so none is pre-typed.
    return {
        "Discogs": {
            "1": (IdentData("Someone", "Else", 1999, None, "WEB"), "Someone - Else"),
            "2": (IdentData("Someone", "Other", 1999, None, "WEB"), "Someone - Other"),
        }
    }


def _rls_data() -> dict[str, Any]:
    return {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
        "source": "WEB",
        "year": 2020,
        "group_year": 2020,
        "edition_title": None,
        "label": None,
        "upc": None,
        "genres": [],
        "rls_type": None,
        "comment": None,
        "urls": [],
        "tracks": {},
        "cover": None,
        "scene": False,
        "encoding_vbr": False,
        "date": None,
    }


async def _rls(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _rls_data()


def _tagged_album(folder: Path) -> Path:
    """The album of helpers.uploader_dry_run, its files naming their Deezer album: the URL the question pre-types."""
    _album(folder)
    for name in ("01 - one.flac", "02 - two.flac"):
        _write_flac(folder / name, title=name, artist="Artist", year="2020", source=TAGGED_URL)
    return folder


Answers = Callable[["Store"], list[str]]


def _main(
    monkeypatch, capsys, argv: list[str], answers: Answers, store: "Store", tracker: FakeTracker | None = None
) -> tuple[Any, str]:
    """Run main with argv and answers(store) typed in, the metadata search stubbed, the stores on `store` and the
    tracker, if any, serving."""
    monkeypatch.setattr(metadata_mod, "run_metasearch", _search)
    monkeypatch.setattr(sys, "argv", argv)

    async def run() -> Any:
        async with store.serving(), tracker.serving() if tracker else contextlib.nullcontext():
            pointed_at(monkeypatch, store)
            monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{answer}\n" for answer in answers(store))))
            monkeypatch.setattr(deezer.Scraper, "url", store.url(""))
            return await anyio.to_thread.run_sync(_exit_of_main)

    code = anyio.run(run)
    captured = capsys.readouterr()
    return code, click.unstyle(captured.out + captured.err)


def _up(monkeypatch, capsys, dirs, answers: Answers, *, yyy: bool, tagged: bool = False):
    """salmon up through main, with the real get_metadata. Returns the exit, the output, the tracker and the store."""
    downloads, torrents = dirs
    folder = downloads.parent / "seeding" / "Album"
    album = _tagged_album(folder) if tagged else _album(folder)
    tracker = FakeTracker()
    fake_upload_world(
        monkeypatch,
        torrents,
        tracker=tracker,
        construct_rls_data=_rls,
        get_metadata=metadata_mod.get_metadata,
        # Not lossy: no lossy-approval comment to ask for.
        check_spectrals=_returning_async((False, None)),
    )
    store = Store()
    argv = ["salmon", "up", str(album), "-t", "RED", "-s", "WEB", "-n", "--skip-integrity-check", "--skip-up"]
    # The group question comes first: a new group.
    code, out = _main(
        monkeypatch, capsys, argv + (["-yyy"] if yyy else []), lambda s: ["", *answers(s)], store, tracker
    )
    return code, out, tracker, store


def _bandcamp_url(store: Store) -> str:
    # A URL no other source claims goes to the Bandcamp scraper, as https://open.spotify.com/album/x does.
    return store.url("/album/x")


# Each row: the first answer to the metadata question, which fails. The question is asked again; "a" ends the run.
FAILING = [
    pytest.param(lambda _store: "1", False, id="number"),
    pytest.param(lambda _store: "*1", False, id="number-starred"),
    pytest.param(lambda _store: FAILING_URL, False, id="url"),
    pytest.param(lambda _store: f"*{FAILING_URL}", False, id="url-starred"),
    pytest.param(lambda _store: f"2 {FAILING_URL}", False, id="working-number-and-failing-url"),
    pytest.param(lambda _store: f"*{FAILING_URL}", True, id="url-yyy"),
    pytest.param(lambda _store: "1", True, id="number-yyy"),
    pytest.param(_bandcamp_url, False, id="url-no-other-source-claims"),
]


@pytest.mark.parametrize(("first", "yyy"), FAILING)
def test_a_failed_source_asks_the_question_again(
    monkeypatch,
    capsys,
    dirs,
    first: Callable[[Store], str],
    yyy: bool,
) -> None:
    code, out, tracker, _store = _up(monkeypatch, capsys, dirs, lambda store: [first(store), "a"], yyy=yyy)

    assert (code, out.count(QUESTION), _upload_posts(tracker)) == (1, 2, 0), out
    assert "Scrape error: " in out
    assert "Aborting upload..." in out


def test_enter_on_the_url_pretyped_from_the_tags_asks_again_when_it_fails(monkeypatch, capsys, dirs) -> None:
    code, out, tracker, store = _up(monkeypatch, capsys, dirs, lambda _store: ["", "a"], yyy=False, tagged=True)

    assert (code, out.count(QUESTION), _upload_posts(tracker)) == (1, 2, 0), out
    assert f"Pre-typed *{TAGGED_URL}" in out
    assert "/album/1" in store.requests


@pytest.mark.parametrize("second", ["2", WORKING_URL], ids=["number", "url"])
def test_the_answer_after_a_failed_one_is_the_one_taken(monkeypatch, capsys, dirs, second: str) -> None:
    code, out, tracker, store = _up(
        monkeypatch, capsys, dirs, lambda _store: [f"*{FAILING_URL}", second, *[""] * 30], yyy=True
    )

    assert (code, out.count(QUESTION)) == (0, 2), out
    assert store.requests == ["/releases/1", "/releases/2"]
    # The failed URL is not the source, nor among the release's URLs: the second answer's are.
    assert f"New Source URL: {FAILING_URL}" not in out
    assert _upload_posts(tracker) > 0


def test_a_failed_answer_leaves_neither_its_url_nor_its_star(monkeypatch) -> None:
    store = Store()
    replies = iter([f"*{FAILING_URL}", "2"])

    async def prompt(*_args: Any, **_kwargs: Any) -> str:
        return next(replies)

    monkeypatch.setattr(metadata_mod.interaction, "prompt", prompt)
    choices = {1: ("Discogs", "1"), 2: ("Discogs", "2")}

    async def pick() -> tuple[dict[str, Any], str | None]:
        async with store.serving():
            pointed_at(monkeypatch, store)
            return await metadata_mod._select_choice(choices, _rls_data())

    metadata, source_url = anyio.run(pick)

    assert store.requests == ["/releases/1", "/releases/2"]
    assert source_url is None
    assert FAILING_URL not in metadata["urls"]
    assert metadata["title"] == RELEASE["title"]


# As before: a source that answers is taken at once.


@pytest.mark.parametrize("answer", ["2", WORKING_URL, f"*{WORKING_URL}"], ids=["number", "url", "url-starred"])
def test_a_source_that_answers_is_taken_at_once_as_before(monkeypatch, capsys, dirs, answer: str) -> None:
    code, out, tracker, store = _up(monkeypatch, capsys, dirs, lambda _store: [answer, *[""] * 30], yyy=True)

    assert (code, out.count(QUESTION)) == (0, 1), out
    assert store.requests == ["/releases/2"]
    assert "Scrape error" not in out
    assert _upload_posts(tracker) > 0


# salmon tag


@pytest.fixture
def tag_world(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path / "downloads"))
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(salmon.tagger, "construct_rls_data", _rls)
    return _album(tmp_path / "downloads" / "Album")


def test_salmon_tag_asks_again_when_its_url_fails(monkeypatch, capsys, tag_world: Path) -> None:
    store = Store()

    code, out = _main(
        monkeypatch, capsys, ["salmon", "tag", str(tag_world), "-s", "WEB"], lambda _store: [FAILING_URL, "a"], store
    )

    assert (code, out.count(QUESTION)) == (1, 2), out
    assert "Scrape error: " in out
    assert store.requests == ["/releases/1"]


# salmon web


def test_a_web_tag_job_asks_again_when_its_url_fails(monkeypatch, tag_world: Path) -> None:
    store = Store()
    monkeypatch.setattr(metadata_mod, "run_metasearch", _search)
    # As the page sends them: the URL typed, then Abort picked; a third question fails the test.
    answers = iter([{"texts": [FAILING_URL]}, {"picks": ["a"]}])

    async def test(client: TestClient, manager: JobManager) -> None:
        response = await client.post(
            "/api/jobs", json={"kind": "tag", "params": {"path": str(tag_world), "source": "WEB"}}, headers=AUTH
        )
        assert response.status == 201, await response.text()
        job, asked = await _drive(client, manager, (await response.json())["id"], lambda _question: next(answers))

        assert (job.status, job.error) == ("failed", "Aborted."), _log(job)
        # Asked again, not refused: the second time is a question of its own, with no error.
        assert [(QUESTION in question["text"], question["error"]) for question in asked] == [(True, None)] * 2

    async def serving(client: TestClient, manager: JobManager) -> None:
        async with store.serving():
            pointed_at(monkeypatch, store)
            await test(client, manager)

    _with_app(serving)
    assert store.requests == ["/releases/1"]
