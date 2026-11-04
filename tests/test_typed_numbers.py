"""A number someone types or pastes that int() refuses, or that names nothing, is refused rather than raising.

"12²" passes isdigit() and int() refuses it. A pasted URL's id went to int() unchecked, and its torrentid to the
tracker as written. Digits of other scripts pass both, and read as an ID or a row nobody meant.
"""

import errno
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from helpers.uploader_dry_run import FakeTracker, Run, _album, _run_up
from helpers.webui_interaction import Browser
from test_uploader_dry_run import dirs, image_uploads  # noqa: F401 (fixtures)
from test_uploader_dupe_checker import FakeGazelleSite, _answering
from test_uploader_skip_flac import _group, _release, _torrent

import salmon.trackers
import salmon.uploader
from salmon import interaction
from salmon.cross_upload import _input_item, is_torrent_reference, torrent_id_of
from salmon.tagger import metadata
from salmon.uploader import dupe_checker, spectrals
from salmon.uploader.dupe_checker import _prompt_for_group_id, _prompt_for_recent_upload_results
from salmon.uploader.given_images import parse_spectral_urls
from salmon.webui.interaction import Picked, WebInteraction

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

BASE = "https://tracker.test"
URL = f"{BASE}/torrents.php"


class RedirectSite(FakeGazelleSite):
    """A tracker that answers every torrent ID with a group, and notes what it was asked."""

    base_url = BASE
    site_string = "RED"

    def __init__(self) -> None:
        self.redirected: list[Any] = []

    async def get_redirect_torrentgroupid(self, torrent_id: int) -> int | None:
        self.redirected.append(torrent_id)
        return 1000 + torrent_id


def _site(site: RedirectSite | None = None) -> tuple[RedirectSite, "BaseGazelleApi"]:
    site = site or RedirectSite()
    return site, cast("BaseGazelleApi", cast("object", site))


PROMPTS = [
    pytest.param(lambda site: _prompt_for_group_id(site, [], False), id="search results"),
    pytest.param(lambda site: _prompt_for_recent_upload_results(site, [], "s", False), id="recent uploads"),
]


@pytest.mark.parametrize("prompt", PROMPTS)
@pytest.mark.parametrize("typed", ["12²", "１２"])
def test_a_group_prompt_asks_again_on_a_number_it_cant_read(monkeypatch, prompt, typed: str) -> None:
    asked = _answering(monkeypatch, typed, "n")
    _, site = _site()

    group = anyio.run(prompt, site)

    assert group is None
    assert len(asked) == 2


@pytest.mark.parametrize("prompt", PROMPTS)
@pytest.mark.parametrize(("answer", "group"), [(" 5000 ", 5000), (f"{URL}?id=5", 5)])
def test_a_group_prompt_reads_a_number_or_a_group_url_as_before(monkeypatch, prompt, answer: str, group: int) -> None:
    _answering(monkeypatch, answer)
    _, site = _site()

    picked = anyio.run(prompt, site)

    assert picked == group


@pytest.mark.parametrize(
    ("prompt", "group"),
    [
        pytest.param(lambda site: _prompt_for_group_id(site, [{"groupId": 11}], False), 11, id="search results"),
        pytest.param(
            lambda site: _prompt_for_recent_upload_results(site, [(7, "Artist", "Title")], "s", False),
            1007,
            id="recent uploads",
        ),
    ],
)
def test_a_group_prompt_reads_0_as_the_first_shown_as_before(monkeypatch, prompt, group: int) -> None:
    _answering(monkeypatch, "0")
    _, site = _site()

    assert anyio.run(prompt, site) == group


@pytest.mark.parametrize("prompt", PROMPTS)
def test_a_group_prompt_asks_again_on_a_url_it_cant_read(monkeypatch, capsys, prompt) -> None:
    asked = _answering(monkeypatch, f"{URL}?id=abc", "n")
    _, site = _site()

    group = anyio.run(prompt, site)

    assert group is None
    assert len(asked) == 2
    assert "Could not find group ID in URL." in capsys.readouterr().out


@pytest.mark.parametrize("value", ["abc", "12²", "１２", "-1", "1_000", "1e3", "0"])
def test_a_group_url_whose_id_names_no_group_asks_again(monkeypatch, capsys, value: str) -> None:
    asked = _answering(monkeypatch, f"{URL}?id={value}", "n")
    _, site = _site()

    group = anyio.run(_prompt_for_group_id, site, [], False)

    assert group is None
    assert len(asked) == 2
    assert "Could not find group ID in URL." in capsys.readouterr().out


@pytest.mark.parametrize("value", ["abc", "12²", "１２", "-1", "1_000", "0"])
def test_a_group_urls_torrentid_is_not_sent_unless_its_a_number(monkeypatch, capsys, value: str) -> None:
    asked = _answering(monkeypatch, f"{URL}?torrentid={value}", "n")
    fake, site = _site()

    group = anyio.run(_prompt_for_group_id, site, [], False)

    assert group is None
    assert len(asked) == 2
    assert fake.redirected == []
    assert "Could not get group ID from torrent ID." in capsys.readouterr().out


def test_a_bad_id_beside_a_torrentid_asks_again_and_sends_nothing(monkeypatch, capsys) -> None:
    asked = _answering(monkeypatch, f"{URL}?id=abc&torrentid=7", "n")
    fake, site = _site()

    group = anyio.run(_prompt_for_group_id, site, [], False)

    assert group is None
    assert len(asked) == 2
    assert fake.redirected == []
    assert "Could not find group ID in URL." in capsys.readouterr().out


@pytest.mark.parametrize("prompt", PROMPTS)
@pytest.mark.parametrize("value", ["7", "%207"])
def test_a_group_urls_torrentid_is_sent_as_a_number(monkeypatch, prompt, value: str) -> None:
    _answering(monkeypatch, f"{URL}?torrentid={value}")
    fake, site = _site()

    group = anyio.run(prompt, site)

    assert group == 1007
    assert fake.redirected == [7]


@pytest.mark.parametrize(("query", "group"), [("id=5&torrentid=7", 5), ("id=%205", 5)])
def test_a_group_urls_id_is_read_as_before(monkeypatch, query: str, group: int) -> None:
    _answering(monkeypatch, f"{URL}?{query}")
    fake, site = _site()

    picked = anyio.run(_prompt_for_group_id, site, [], False)

    assert picked == group
    assert fake.redirected == []


def test_a_group_url_with_neither_asks_again_as_before(monkeypatch, capsys) -> None:
    _answering(monkeypatch, f"{URL}?x=1", "n")
    _, site = _site()

    group = anyio.run(_prompt_for_group_id, site, [], False)

    assert group is None
    assert "Could not find group ID in URL." in capsys.readouterr().out


class NoGroupSite(RedirectSite):
    def __init__(self, answer: int | None) -> None:
        super().__init__()
        self.answer = answer

    async def get_redirect_torrentgroupid(self, torrent_id: int) -> int | None:
        return self.answer


@pytest.mark.parametrize("answer", [None, 0])
def test_the_search_results_prompt_says_when_a_torrents_group_cant_be_found(monkeypatch, capsys, answer) -> None:
    # It asked again without a word; the recent uploads' prompt already said so.
    asked = _answering(monkeypatch, f"{URL}?torrentid=7", "n")
    _, site = _site(NoGroupSite(answer))

    group = anyio.run(_prompt_for_group_id, site, [], False)

    assert group is None
    assert len(asked) == 2
    assert "Could not get group ID from torrent ID." in capsys.readouterr().out


@pytest.mark.parametrize("prompt", PROMPTS)
def test_a_salmon_web_job_is_asked_again_on_a_group_url_it_cant_read(prompt) -> None:
    browser = Browser([Picked(texts=[f"{URL}?id=abc"]), Picked(picks=["n"])])
    _, site = _site()

    async def run() -> int | None:
        with interaction.using(WebInteraction(browser)):
            return await prompt(site)

    group = anyio.run(run)

    assert group is None
    assert len(browser.questions) == 2


def _flacs() -> tuple[dict[str, Any], dict[str, Any]]:
    return _group(_torrent(11), _torrent(12)), _release()


@pytest.mark.parametrize("typed", ["12²", "１", "٢"])
def test_the_flac_prompt_asks_again_on_a_number_it_cant_read(monkeypatch, capsys, typed: str) -> None:
    _answering(monkeypatch, typed, "a")

    chosen = anyio.run(dupe_checker.choose_source_flac, *_flacs())

    assert chosen is None
    assert "Enter a number from 1 to" in capsys.readouterr().out


def test_the_flac_prompt_asks_again_on_0_as_before(monkeypatch, capsys) -> None:
    _answering(monkeypatch, "0", "a")

    chosen = anyio.run(dupe_checker.choose_source_flac, *_flacs())

    assert chosen is None
    assert "Enter a number from 1 to" in capsys.readouterr().out


SPECTRALS = {1: "01.flac", 3: "03.flac"}


@pytest.mark.parametrize("typed", ["12²", "３", "1 ٣"])
def test_the_spectrals_prompt_asks_again_on_an_id_it_cant_read(monkeypatch, capsys, typed: str) -> None:
    _answering(monkeypatch, typed, "1")

    chosen = anyio.run(spectrals.prompt_spectrals, SPECTRALS, False, False)

    assert chosen == {1: "01.flac"}
    assert "Invalid IDs." in capsys.readouterr().out


@pytest.mark.parametrize("typed", ["2", "1 2", "1 abc"])
def test_the_spectrals_prompt_asks_again_on_an_id_it_doesnt_have_as_before(monkeypatch, capsys, typed: str) -> None:
    _answering(monkeypatch, typed, "1")

    chosen = anyio.run(spectrals.prompt_spectrals, SPECTRALS, False, False)

    assert chosen == {1: "01.flac"}
    assert "Invalid IDs." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("typed", "chosen"),
    [("3 1", {3: "03.flac", 1: "01.flac"}), ("1 1", {1: "01.flac"}), (" 3 ", {3: "03.flac"}), ("  ", {})],
)
def test_the_spectrals_prompt_reads_ids_as_before(monkeypatch, typed: str, chosen: dict[int, str]) -> None:
    _answering(monkeypatch, typed)

    picked = anyio.run(spectrals.prompt_spectrals, SPECTRALS, False, False)

    assert picked is not None
    assert list(picked.items()) == list(chosen.items())


def test_a_configured_default_in_other_digits_is_given_to_the_prompt_in_plain_digits(monkeypatch) -> None:
    # The config's pattern takes it, and with -yyy the prompt reads it again each time round: refused, it never ends.
    monkeypatch.setattr(spectrals.cfg.image, "default_spectral_ids", "1٣")

    assert spectrals._default_spectral_selection({13: "13.flac"}, False) == "13"


@pytest.mark.parametrize("typed", ["12²", "３"])
def test_the_metadata_prompt_passes_over_a_number_it_cant_read(monkeypatch, typed: str) -> None:
    def no_scraper() -> None:
        raise AssertionError("a store was asked")

    _answering(monkeypatch, typed, "a")
    monkeypatch.setattr(metadata, "METASOURCES", {"Store": SimpleNamespace(Scraper=no_scraper)})

    with pytest.raises(click.Abort):
        anyio.run(metadata._select_choice, {3: ("Store", "y")}, {"urls": []})


def test_the_metadata_prompt_reads_a_row_beside_a_number_it_cant_read(monkeypatch) -> None:
    asked = []

    class Scraper:
        def scrape_release_from_id(self, release_id: str) -> None:
            asked.append(release_id)
            raise click.Abort

    _answering(monkeypatch, "٤ 3")
    monkeypatch.setattr(metadata, "METASOURCES", {"Store": SimpleNamespace(Scraper=Scraper)})

    with pytest.raises(click.Abort):
        anyio.run(metadata._select_choice, {3: ("Store", "y"), 4: ("Store", "z")}, {"urls": []})

    assert asked == ["y"]


@pytest.mark.parametrize("track", ["12²", "１２"])
def test_a_given_spectral_url_needs_a_track_number(track: str) -> None:
    with pytest.raises(ValueError, match="is not a track number"):
        parse_spectral_urls([f"{track}=https://images.test/a.png"])


def test_a_given_spectral_url_refuses_no_track_as_before() -> None:
    with pytest.raises(ValueError, match="is not a track number"):
        parse_spectral_urls(["-1=https://images.test/a.png"])


def test_a_given_spectral_url_reads_a_track_number_as_before() -> None:
    assert parse_spectral_urls([" 2 =https://images.test/a.png"]) == ((2, ("https://images.test/a.png",)),)


@pytest.mark.parametrize("value", ["12²", "１２"])
def test_a_cross_uploads_id_it_cant_read_is_no_reference_and_is_refused(value: str) -> None:
    assert not is_torrent_reference(value)
    with pytest.raises(click.UsageError):
        torrent_id_of(value, _site()[1])


@pytest.mark.parametrize("value", ["12²", "１２"])
def test_a_cross_uploads_url_needs_a_torrentid_in_plain_digits(value: str) -> None:
    with pytest.raises(click.UsageError, match="numeric torrentid"):
        torrent_id_of(f"{URL}?torrentid={value}", _site()[1])


@pytest.mark.parametrize("value", ["9" * 5000, "a" * 5000], ids=["5000 digits", "5000 letters"])
def test_a_cross_uploads_input_too_long_for_a_path_is_refused(monkeypatch, value: str) -> None:
    # Before Python 3.14 is_file() raises this itself, and from 3.14 it answers False, so raise it on every version.
    def too_long(_path: Path) -> bool:
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(Path, "is_file", too_long)
    _, site = _site()

    with pytest.raises(click.UsageError, match="is not a RED torrent ID or URL"):
        _input_item(value, site)


@pytest.mark.parametrize("value", ["abc", ""])
def test_a_cross_uploads_url_needs_a_torrentid_as_before(value: str) -> None:
    with pytest.raises(click.UsageError, match="numeric torrentid"):
        torrent_id_of(f"{URL}?torrentid={value}", _site()[1])


@pytest.mark.parametrize(("value", "torrent"), [("0", 0), (" 12 ", 12), (f"{URL}?torrentid=12", 12)])
def test_a_cross_uploads_input_is_read_as_before(value: str, torrent: int) -> None:
    assert is_torrent_reference(value)
    assert torrent_id_of(value, _site()[1]) == torrent


@pytest.mark.parametrize("given", ["abc", "12²", "-1", "0", ""])
def test_up_refuses_a_group_id_that_isnt_one_before_asking_the_tracker(monkeypatch, tmp_path: Path, given: str) -> None:
    def no_tracker(_tracker: str) -> None:
        raise AssertionError("the tracker was asked")

    monkeypatch.setattr(salmon.trackers, "get_class", no_tracker)

    async def invoke() -> Any:
        return await CliRunner().invoke(salmon.uploader.up, [str(tmp_path), "-t", "RED", "-g", given, "-s", "WEB"])

    result = anyio.run(invoke)

    assert result.exit_code == 2
    assert "--group-id" in result.output


@pytest.mark.parametrize(("given", "group"), [("1", 1), ("5", 5)])
def test_up_gives_the_group_id_as_a_number(monkeypatch, tmp_path: Path, given: str, group: int) -> None:
    seen = []

    async def confirm(_site: object, group_id: object, _source: object) -> dict:
        seen.append(group_id)
        return {}

    async def fake_upload(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: lambda: object())
    monkeypatch.setattr(salmon.uploader, "confirm_group_upload", confirm)
    monkeypatch.setattr(salmon.uploader, "upload", fake_upload)

    async def invoke() -> Any:
        return await CliRunner().invoke(salmon.uploader.up, [str(tmp_path), "-t", "RED", "-g", given, "-s", "WEB"])

    result = anyio.run(invoke)

    assert result.exit_code == 0, result.output
    assert seen == [group]


def _up_with_group(monkeypatch, album: Path, torrents: Path, *args: str) -> Run:
    return _run_up(
        monkeypatch,
        _album(album),
        torrents,
        args=args,
        input="\n" * 5,
        trackers=("RED",),
        multi_tracker_upload=False,
        tracker=FakeTracker(group_torrents=[]),
    )


@pytest.mark.parametrize("given", ["+1", "1_000", "１２", "٣", "12²", "   "])
def test_up_reads_a_group_id_by_the_prompts_rule(monkeypatch, tmp_path, dirs, image_uploads, given: str) -> None:  # noqa: F811
    run = _up_with_group(monkeypatch, tmp_path / "Album", dirs[2], "-g", given)

    assert run.result.exit_code == 2
    assert "is not a group ID" in run.result.output
    assert run.tracker.sent == []


def _sent(run: Run) -> list[tuple[str, str, dict[str, str], list[tuple[str, str]]]]:
    """Each request in order, with the text fields of its form; the fake's address, a new port each run, taken out."""
    assert run.result.exit_code == 0, run.result.output
    return [
        (
            sent.method,
            sent.path,
            sent.query,
            [(k, v.replace(run.tracker.url, "")) for k, v in sent.fields if isinstance(v, str)],
        )
        for sent in run.tracker.sent
    ]


def test_up_sends_a_group_id_as_a_number_as_before(monkeypatch, tmp_path, dirs, image_uploads) -> None:  # noqa: F811
    run = _up_with_group(monkeypatch, tmp_path / "Album", dirs[2], "-g", "12")

    assert next(sent.query["id"] for sent in run.tracker.sent if sent.query.get("action") == "torrentgroup") == "12"
    # The FLAC's: the fake then answers with a group of its own, for what follows.
    upload = next(sent for sent in run.tracker.sent if sent.query.get("action") == "upload")
    assert dict(upload.fields)["groupid"] == "12"


@pytest.mark.parametrize("given", ["012", " 12 "])
def test_up_reads_a_padded_group_id_as_the_same_group(monkeypatch, tmp_path, dirs, image_uploads, given: str) -> None:  # noqa: F811
    # As a prompt reads it. Master sent it as text: id=012.
    _library, downloads, torrents = dirs
    plain = _up_with_group(monkeypatch, tmp_path / "plain" / "Album", torrents, "-g", "12")
    image_uploads.clear()  # Its URLs count up, and go in the form.
    shutil.rmtree(downloads)
    shutil.rmtree(torrents)
    downloads.mkdir()
    torrents.mkdir()
    padded = _up_with_group(monkeypatch, tmp_path / "padded" / "Album", torrents, "-g", given)

    assert _sent(padded) == _sent(plain)


def test_up_without_a_group_id_searches_as_before(monkeypatch, tmp_path, dirs, image_uploads) -> None:  # noqa: F811
    run = _up_with_group(monkeypatch, tmp_path / "Album", dirs[2])

    assert run.result.exit_code == 0, run.result.output
    assert any(sent.query.get("action") == "browse" for sent in run.tracker.sent)
