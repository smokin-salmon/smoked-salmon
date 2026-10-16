"""The group an upload goes into is weighed against the reviewed edition, and so are the conversions offered after
the FLAC goes up into it: what the edition already holds is a dupe risk the site removes.

The runs here go against the local fake tracker of test_uploader_dry_run and a fake image host, never a real one.
"""

from types import SimpleNamespace
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    GROUP_ID,
    FakeTracker,
    Sent,
    _album,
    _returning_async,
    _run_up,
    _success,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

import salmon.uploader
from salmon.errors import RequestFailedError
from salmon.uploader import dupe_checker

# Covers go to the fake image host.
pytestmark = pytest.mark.usefixtures("image_uploads")

CONFIRM_GROUP = "Are you sure you would you like to upload this torrent to this group?"
REVIEWED_IN_GROUP = f"The reviewed edition is already in group {GROUP_ID}:"


def _torrent(torrent_id: int, media: str = "WEB", fmt: str = "FLAC", encoding: str = "Lossless") -> dict[str, Any]:
    """A torrent of the 2020 edition the stubbed review gives (WEB, 2020, Label)."""
    return {
        "id": torrent_id,
        "media": media,
        "format": fmt,
        "encoding": encoding,
        "remastered": True,
        "remasterYear": 2020,
        "remasterTitle": "",
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": "CAT1",
    }


WEB_FLAC = _torrent(11)
WEB_320 = _torrent(12, fmt="MP3", encoding="320")
CD_FLAC = _torrent(13, media="CD")


class GroupTracker(FakeTracker):
    """The fake tracker, whose group holds `group_torrents` and whose search finds that group when `found`."""

    def __init__(self, group_torrents: list[dict[str, Any]], found: bool = False) -> None:
        super().__init__(group_torrents)
        self._found = found

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/ajax.php" and request.query.get("action") == "browse":
            self.sent.append(Sent(request.method, request.path, dict(request.query), []))
            result = {
                "groupId": GROUP_ID,
                "groupName": "Album",
                "groupYear": 2020,
                "artist": "Artist",
                "releaseType": "Album",
                "tags": [],
                "torrents": self._group_torrents,
            }
            return _success({"results": [result] if self._found else []})
        return await super()._handle(request)


def _tags_release(year: int) -> dict[str, Any]:
    """The release as the tags give it, before the review: the review gives year 2020."""
    return {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
        "source": "WEB",
        "year": year,
    }


def _up(monkeypatch, tmp_path, tracker: GroupTracker, args: tuple[str, ...] = (), **kwargs: Any):
    torrents = tmp_path / "torrents"
    torrents.mkdir()
    monkeypatch.setattr(salmon.uploader.cfg.directory, "download_directory", str(tmp_path))
    monkeypatch.setattr(salmon.uploader.cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(salmon.uploader.cfg.directory, "library_dirs", [])
    kwargs.setdefault("multi_tracker_upload", False)
    kwargs.setdefault("construct_rls_data", _returning_async(_tags_release(2020)))
    return _run_up(
        monkeypatch,
        _album(tmp_path / "Album"),
        torrents,
        args,
        tracker=tracker,
        check_spectrals=_returning_async((False, None)),
        **kwargs,
    )


def _uploads(run) -> list[str]:
    """The format and encoding of each torrent the run uploaded, in order."""
    posts = [sent for sent in run.tracker.sent if sent.method == "POST" and sent.query.get("action") == "upload"]
    return [f"{dict(post.fields)['format']} {dict(post.fields)['bitrate']}" for post in posts]


def _group_fetches_before_the_upload(run) -> int:
    count = 0
    for sent in run.tracker.sent:
        if sent.method == "POST":
            break
        count += sent.query.get("action") == "torrentgroup"
    return count


def _group_fetches(run) -> int:
    return sum(sent.query.get("action") == "torrentgroup" for sent in run.tracker.sent)


# The conversions offered after the FLAC goes up


def test_a_conversion_the_edition_holds_is_not_made_with_yyy(monkeypatch, tmp_path) -> None:
    run = _up(monkeypatch, tmp_path, GroupTracker([WEB_FLAC, WEB_320]), input="\n")

    assert run.result.exit_code == 0, run.result.output
    assert _uploads(run) == ["FLAC Lossless", "MP3 V0 (VBR)"]
    assert "DUPE RISK: this edition already has MP3 320; the site removes exact duplicates." in run.result.output
    # A new group: the group printed after the upload is the only one fetched, and the check reads it.
    assert _group_fetches(run) == 1


def test_a_conversion_the_edition_holds_is_not_selected_in_the_prompt(monkeypatch, tmp_path) -> None:
    defaults: list[Any] = []
    prompt = click.prompt

    async def recording(text: str, *args: Any, **kwargs: Any) -> Any:
        if "downconversion" in text.lower() or "formats" in text.lower():
            defaults.append(kwargs.get("default"))
        return await prompt(text, *args, **kwargs)

    monkeypatch.setattr(click, "prompt", recording)
    run = _up(monkeypatch, tmp_path, GroupTracker([WEB_FLAC, WEB_320]), input="\n" * 20, yes_all=False)

    assert run.result.exit_code == 0, run.result.output
    # 1. MP3 320, 2. MP3 V0: only V0 is selected.
    assert defaults == ["2"]
    assert _uploads(run) == ["FLAC Lossless", "MP3 V0 (VBR)"]
    assert "DUPE RISK: this edition already has MP3 320" in run.result.output


def test_each_trackers_upload_leaves_out_what_its_edition_holds(monkeypatch, tmp_path) -> None:
    # RED, then OPS (named with -t: no question between them); a new group on each.
    run = _up(monkeypatch, tmp_path, GroupTracker([WEB_FLAC, WEB_320]), ("-t", "OPS"), input="\n\n")

    assert run.result.exit_code == 0, run.result.output
    assert _uploads(run) == ["FLAC Lossless", "MP3 V0 (VBR)"] * 2
    assert run.result.output.count("DUPE RISK: this edition already has MP3 320") == 2


def test_a_conversion_of_another_edition_is_made(monkeypatch, tmp_path) -> None:
    run = _up(monkeypatch, tmp_path, GroupTracker([{**WEB_320, "remasterYear": 2010}]), input="\n")

    assert run.result.exit_code == 0, run.result.output
    assert _uploads(run) == ["FLAC Lossless", "MP3 320", "MP3 V0 (VBR)"]
    assert "DUPE RISK" not in run.result.output


# The group picked before the review, weighed against the reviewed edition


@pytest.mark.parametrize("in_place", [False, True], ids=["scrape-copies", "scrape-edits-the-tags-release"])
def test_a_review_moving_the_release_into_an_edition_the_group_holds_asks_again(
    monkeypatch, tmp_path, in_place: bool
) -> None:
    fakes: dict[str, Any] = {}
    tags_release = _tags_release(2019)
    if in_place:
        # As combine_metadatas does with its base: the chosen scrape lands in the tags' release itself.
        async def get_metadata(_path, _tags, rls_data):
            rls_data["year"] = 2020
            return {**rls_data, "source": "WEB", "label": "Label", "cover": None}, None

        fakes["get_metadata"] = get_metadata
    # The group (1), then yes: the tags' 2019 edition is not in it. The review gives 2020, which is.
    run = _up(
        monkeypatch,
        tmp_path,
        GroupTracker([WEB_FLAC], found=True),
        input="1\n\n\n",
        construct_rls_data=_returning_async(tags_release),
        **fakes,
    )

    assert run.result.exit_code == 0, run.result.output
    output = run.result.output
    assert output.count(CONFIRM_GROUP) == 2
    assert REVIEWED_IN_GROUP in output
    assert "DUPE RISK: this edition already has 2020 / Label / CAT1 / WEB / FLAC / Lossless" in output
    # Abort was the default the second time.
    assert "Aborting upload..." in output
    assert _uploads(run) == []
    assert _group_fetches(run) == 1


@pytest.mark.parametrize(
    ("group_torrents", "answers"),
    [([CD_FLAC], "1\n\n"), ([WEB_FLAC], "1\ny\n")],
    ids=["edition-not-held", "held-and-confirmed-before-the-review"],
)
def test_a_review_changing_nothing_relevant_asks_nothing_more(monkeypatch, tmp_path, group_torrents, answers) -> None:
    run = _up(monkeypatch, tmp_path, GroupTracker(group_torrents, found=True), input=answers)

    assert run.result.exit_code == 0, run.result.output
    assert run.result.output.count(CONFIRM_GROUP) == 1
    assert REVIEWED_IN_GROUP not in run.result.output
    assert _uploads(run)[0] == "FLAC Lossless"
    # The picked group came from the search results; the re-check is the one fetch before the upload.
    assert _group_fetches_before_the_upload(run) == 1


def test_a_group_given_with_g_is_weighed_against_the_reviewed_edition_and_fetched_once_more(
    monkeypatch, tmp_path
) -> None:
    # Continue with the group given, then yes despite the dupe risk.
    run = _up(monkeypatch, tmp_path, GroupTracker([WEB_FLAC]), ("-g", str(GROUP_ID)), input="\ny\n")

    assert run.result.exit_code == 0, run.result.output
    output = run.result.output
    assert REVIEWED_IN_GROUP in output
    assert "DUPE RISK: this edition already has 2020 / Label / CAT1 / WEB / FLAC / Lossless" in output
    assert _uploads(run)[0] == "FLAC Lossless"
    # The group, given as text: fetched to confirm it, then by the re-check, whose question reuses that fetch.
    assert _group_fetches_before_the_upload(run) == 2


def test_a_skip_flac_upload_group_is_not_fetched_again(monkeypatch, tmp_path) -> None:
    run = _up(monkeypatch, tmp_path, GroupTracker([WEB_FLAC]), ("-g", str(GROUP_ID), "--skip-flac-upload"), input="\n")

    assert run.result.exit_code == 0, run.result.output
    assert REVIEWED_IN_GROUP not in run.result.output
    assert _group_fetches(run) == 1


# recheck_edition itself


def _site(group: dict[str, Any] | Exception) -> Any:
    calls: list[Any] = []

    async def torrentgroup(group_id: Any) -> dict[str, Any]:
        calls.append(group_id)
        if isinstance(group, Exception):
            raise group
        return group

    return SimpleNamespace(
        base_url="https://tracker.test", site_string="TRK", site_code="TRK", torrentgroup=torrentgroup, calls=calls
    )


def _group() -> dict[str, Any]:
    info = {"id": 555, "name": "Album", "year": 2020, "musicInfo": {"artists": [{"name": "Artist"}]}}
    return {"group": info, "torrents": [{**WEB_FLAC, "id": 7}]}


REVIEWED = {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": 2020}


def _answers(monkeypatch, *answers: str) -> list[Any]:
    """Answer click.prompt with `answers` in turn (an empty one takes the default); give the defaults offered."""
    defaults: list[Any] = []
    queue = list(answers)

    async def prompt(*_args: Any, **kwargs: Any) -> Any:
        defaults.append(kwargs.get("default"))
        return (queue.pop(0) if queue else "") or kwargs.get("default")

    monkeypatch.setattr(click, "prompt", prompt)
    return defaults


def test_a_group_id_given_as_text_is_fetched_once(monkeypatch) -> None:
    site = _site(_group())
    defaults = _answers(monkeypatch, "y")

    # As -g gives it: click passes the option's text on.
    result = anyio.run(dupe_checker.recheck_edition, site, "555", REVIEWED, None)  # pyright: ignore[reportArgumentType]

    assert result == "555"
    assert defaults == ["a"]
    assert site.calls == ["555"]


def test_a_new_group_answer_gives_none(monkeypatch) -> None:
    _answers(monkeypatch, "n")

    assert anyio.run(dupe_checker.recheck_edition, _site(_group()), 555, REVIEWED, {**REVIEWED, "year": 2010}) is None


def test_a_torrent_the_pick_already_named_asks_nothing(monkeypatch) -> None:
    defaults = _answers(monkeypatch)

    assert anyio.run(dupe_checker.recheck_edition, _site(_group()), 555, REVIEWED, REVIEWED) == 555
    assert defaults == []


def test_a_failed_recheck_keeps_the_group_and_says_so(monkeypatch, capsys) -> None:
    defaults = _answers(monkeypatch)

    result = anyio.run(dupe_checker.recheck_edition, _site(RequestFailedError("down")), 555, REVIEWED, None)

    assert result == 555
    assert defaults == []
    assert "Could not re-check group 555 on TRK against the reviewed edition (down)" in capsys.readouterr().out
