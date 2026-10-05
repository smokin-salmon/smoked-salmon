import contextlib
import os
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner

import salmon.trackers
import salmon.uploader
from salmon.uploader import dupe_checker


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)


class FakeSite:
    """A tracker that answers torrentgroup from memory and counts the calls."""

    site_code = "RED"
    site_string = "RED"
    base_url = "https://tracker.test"

    def __init__(self, group: dict[str, Any] | None = None) -> None:
        self.group = group
        self.torrentgroup_calls = 0

    async def torrentgroup(self, group_id: int) -> dict[str, Any]:
        self.torrentgroup_calls += 1
        assert self.group is not None
        return self.group


def _torrent(
    torrent_id: int,
    media: str = "WEB",
    format_: str = "FLAC",
    encoding: str = "Lossless",
    catno: str = "CAT1",
    title: str = "",
    year: int = 2020,
) -> dict:
    return {
        "id": torrent_id,
        "media": media,
        "format": format_,
        "encoding": encoding,
        "remastered": True,
        "remasterYear": year,
        "remasterTitle": title,
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": catno,
    }


def _group(*torrents: dict) -> dict[str, Any]:
    return {
        "group": {
            "id": 5,
            "name": "Album",
            "year": 2020,
            "recordLabel": "Label",
            "catalogueNumber": "",
            "musicInfo": {"artists": [{"name": "Artist"}]},
        },
        "torrents": list(torrents),
    }


def _release(**overrides: Any) -> dict[str, Any]:
    return {
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "year": 2020,
        "catno": "CAT1",
        "edition_title": None,
        "artists": [("Artist", "main")],
        "title": "Album",
        **overrides,
    }


def _answers(monkeypatch, *answers: str) -> list[str]:
    """Answer the prompts with answers in turn. Returns the prompts asked."""
    asked: list[str] = []

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        asked.append(text)
        return answers[len(asked) - 1]

    monkeypatch.setattr(salmon.uploader.click, "prompt", fake_prompt)
    return asked


def _choose(group: dict[str, Any], **release: Any) -> int | None:
    chosen = anyio.run(dupe_checker.choose_source_flac, group, _release(**release))
    return chosen["id"] if chosen else None


def test_one_matching_flac_is_the_transcode_source(monkeypatch) -> None:
    asked = _answers(monkeypatch)
    group = _group(
        _torrent(10, format_="MP3", encoding="320"),
        _torrent(11, media="CD"),
        _torrent(12, encoding="24bit Lossless"),
        _torrent(13),
    )

    assert _choose(group) == 13
    assert asked == []


def test_one_matching_flac_needs_no_prompt_with_yes_all(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)

    assert _choose(_group(_torrent(13))) == 13


def test_24bit_release_is_a_transcode_of_the_24bit_flac() -> None:
    group = _group(_torrent(11), _torrent(12, encoding="24bit Lossless"))

    assert _choose(group, encoding="24bit Lossless") == 12


def test_no_matching_flac_stops(monkeypatch) -> None:
    asked = _answers(monkeypatch)
    group = _group(_torrent(10, format_="MP3", encoding="V0 (VBR)"), _torrent(11, media="CD"))

    assert _choose(group) is None
    assert asked == []


@pytest.mark.parametrize(
    ("other", "release"),
    [
        ({"year": 2011}, {}),
        ({"title": "Deluxe"}, {"edition_title": "Remastered"}),
    ],
    ids=["year", "edition title"],
)
def test_the_flac_of_this_edition_is_picked_over_one_of_another(
    monkeypatch, other: dict[str, Any], release: dict[str, Any]
) -> None:
    """Two FLACs differing only by one edition field: the one of this release's edition is the source."""
    asked = _answers(monkeypatch)
    this_edition: dict[str, Any] = {"title": "Remastered"} if "title" in other else {}
    group = _group(_torrent(11, **other), _torrent(12, **this_edition), _torrent(13, **other))

    assert _choose(group, **release) == 12
    assert asked == []


def test_flacs_differing_only_by_catalogue_number_are_both_offered(monkeypatch) -> None:
    """Catalogue numbers come in different conventions (a label's number, a UPC): they never tell editions apart."""
    asked = _answers(monkeypatch, "2")
    group = _group(_torrent(11, catno="0123456789012"), _torrent(12))

    assert _choose(group) == 12
    assert len(asked) == 1


def test_a_flac_of_another_edition_only_stops(monkeypatch) -> None:
    asked = _answers(monkeypatch)

    assert _choose(_group(_torrent(11, year=2011))) is None
    assert asked == []


def test_an_edition_field_missing_on_either_side_still_matches() -> None:
    assert _choose(_group(_torrent(11)), edition_title="Deluxe") == 11
    assert _choose(_group(_torrent(11, title="Deluxe"))) == 11


def test_a_remaster_with_no_year_matches_any_year() -> None:
    """The group's year is the original release's; a remaster missing its own year does not take it."""
    group = _group(_torrent(11, year=0))
    group["group"]["year"] = 2011

    assert _choose(group) == 11


def test_several_matching_flacs_ask_which_one(monkeypatch) -> None:
    asked = _answers(monkeypatch, "3", "x", "2")
    group = _group(_torrent(11), _torrent(10, format_="MP3", encoding="320"), _torrent(12))

    assert _choose(group) == 12
    assert len(asked) == 3


def test_several_matching_flacs_can_abort(monkeypatch) -> None:
    _answers(monkeypatch, "a")

    assert _choose(_group(_torrent(11), _torrent(12))) is None


def test_several_matching_flacs_stop_with_yes_all(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    asked = _answers(monkeypatch)

    assert _choose(_group(_torrent(11), _torrent(12))) is None
    assert asked == []


def _stub(monkeypatch, fakes: dict[str, Any]) -> None:
    for name, fake in fakes.items():
        monkeypatch.setattr(salmon.uploader, name, fake)


def _returning(result: Any = None, calls: list[str] | None = None, record: str | None = None):
    def fake(*_args, **_kwargs) -> Any:
        if calls is not None and record:
            calls.append(record)
        return result

    return fake


def _returning_async(result: Any = None, calls: list[str] | None = None, record: str | None = None):
    async def fake(*_args, **_kwargs) -> Any:
        if calls is not None and record:
            calls.append(record)
        return result

    return fake


@pytest.mark.parametrize("names", [["01.mp3", "02.mp3"], ["01.m4a"], ["01.flac", "02.mp3"], []])
def test_a_release_that_is_not_all_flac_stops_before_the_copy(monkeypatch, tmp_path, names: list[str]) -> None:
    calls: list[str] = []
    release = tmp_path / "release"
    release.mkdir()
    for name in names:
        (release / name).write_bytes(b"audio")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(salmon.uploader.cfg.directory, "download_directory", str(downloads))
    _stub(
        monkeypatch,
        {
            name: _returning(None, calls, name)
            for name in ("staged_source", "gather_audio_info", "standardize_tags", "construct_rls_data")
        },
    )
    site = FakeSite(_group(_torrent(11)))

    anyio.run(
        lambda: salmon.uploader.upload(
            site,  # type: ignore[arg-type]
            str(release),
            5,
            "WEB",
            None,
            (),
            None,
            flac_group=_group(_torrent(11)),
        )
    )

    assert calls == []
    assert os.listdir(downloads) == []
    assert site.torrentgroup_calls == 0


@contextlib.contextmanager
def _staged_as_is(path: str, scratch: bool):
    yield path, None


@pytest.mark.parametrize(
    ("format_", "encoding"),
    [("MP3", "320"), ("MP3", "V0 (VBR)"), ("AAC", "256")],
)
def test_lossy_release_stops_before_any_check_or_upload(monkeypatch, tmp_path, format_: str, encoding: str) -> None:
    """Files named .flac that hold something else are only caught once the release data is known."""
    calls: list[str] = []
    (tmp_path / "01.flac").write_bytes(b"audio")
    _stub(
        monkeypatch,
        {
            "staged_source": _staged_as_is,
            "gather_audio_info": _returning({}, calls, "gather_audio_info"),
            "check_hybrid": _returning(False, calls, "check_hybrid"),
            "standardize_tags": _returning(None, calls, "standardize_tags"),
            "gather_tags": _returning({}, calls, "gather_tags"),
            "construct_rls_data": _returning({"format": format_, "encoding": encoding}, calls, "rls_data"),
            **{
                name: _returning_async(None, calls, name)
                for name in ("mqa_test", "choose_source_flac", "check_existing_group", "upload_and_report")
            },
        },
    )
    site = FakeSite(_group(_torrent(11)))

    anyio.run(
        lambda: salmon.uploader.upload(
            site,  # type: ignore[arg-type]
            str(tmp_path),
            5,
            "WEB",
            None,
            (),
            None,
            flac_group=_group(_torrent(11)),
        )
    )

    assert calls[-1] == "rls_data"
    assert site.torrentgroup_calls == 0


async def _invoke(monkeypatch, tmp_path, *args: str, group: dict[str, Any] | None = None):
    """Run `salmon up` on tmp_path with args. Returns the result, the fake site and upload's kwargs."""
    site = FakeSite(group)
    uploads: list[dict[str, Any]] = []

    async def fake_upload(*_args, **kwargs) -> None:
        uploads.append(kwargs)

    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: lambda: site)
    monkeypatch.setattr(salmon.uploader, "upload", fake_upload)
    result = await CliRunner().invoke(salmon.uploader.up, [str(tmp_path), "-t", "RED", *args], input="y\n")
    return result, site, uploads


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["-s", "WEB", "--skip-flac-upload"], "--skip-flac-upload requires --group-id."),
        (
            ["-s", "WEB", "-g", "5", "--skip-flac-upload", "-r", "7"],
            "--skip-flac-upload cannot be used with --request.",
        ),
        (
            ["-s", "WEB", "-g", "5", "--skip-flac-upload", "-a"],
            "--skip-flac-upload cannot be used with --spectrals-after.",
        ),
    ],
)
def test_skip_flac_upload_usage_errors(monkeypatch, tmp_path, args: list[str], message: str) -> None:
    async def run():
        return await _invoke(monkeypatch, tmp_path, *args)

    result, site, uploads = anyio.run(run)

    assert result.exit_code == 2
    assert message in result.output
    assert site.torrentgroup_calls == 0
    assert uploads == []


def test_skip_flac_upload_reuses_the_group_fetched_for_confirmation(monkeypatch, tmp_path) -> None:
    group = _group(_torrent(11))

    async def run():
        return await _invoke(monkeypatch, tmp_path, "-g", "5", "-s", "WEB", "--skip-flac-upload", group=group)

    result, site, uploads = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert site.torrentgroup_calls == 1
    assert [u["flac_group"] for u in uploads] == [group]


def test_group_upload_without_the_flag_uploads_the_flac(monkeypatch, tmp_path) -> None:
    async def run():
        return await _invoke(monkeypatch, tmp_path, "-g", "5", "-s", "WEB", group=_group(_torrent(11)))

    result, _site, uploads = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert [u["flac_group"] for u in uploads] == [None]


def _flow(
    monkeypatch, group: dict[str, Any], source: str = "WEB", reviewed: dict[str, Any] | None = None, **fakes: Any
) -> tuple[list[str], list[tuple]]:
    """Run upload() with --skip-flac-upload over stubbed seams. Returns the calls and the transcode runs.

    `reviewed` overrides fields of the metadata as the review leaves it.
    """
    calls: list[str] = []
    transcoded: list[tuple] = []

    async def fake_execute(tasks, _path, *args, **_kwargs) -> None:
        transcoded.append(([t["name"] for t in tasks], args[-1]))

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            pass

    rls_data = _release(source=source)
    metadata = {**_release(source=source), "cover": None, **(reviewed or {})}
    track_data = {"01.flac": {"sample rate": 44100}}
    _stub(
        monkeypatch,
        {
            "validate_skip_flac_source": _returning(None),
            "staged_source": _staged_as_is,
            "gather_audio_info": _returning({}),
            "check_hybrid": _returning(False),
            "standardize_tags": _returning(),
            "gather_tags": _returning({}),
            "construct_rls_data": _returning(rls_data),
            "mqa_test": _returning_async(None, calls, "mqa_test"),
            "_check_logs": _returning_async(None, calls, "log_check"),
            "check_spectrals": _returning_async((False, None), calls, "check_spectrals"),
            "get_metadata": _returning_async((metadata, None)),
            "edit_metadata": _returning_async(("/release", metadata, {}, {})),
            "concat_track_data": _returning(track_data),
            "resolve_cover_url": _returning_async((True, None)),
            "UploadManager": FakeUploadManager,
            "check_requests": _returning_async(None, calls, "check_requests"),
            "upload_and_report": _returning_async(None, calls, "upload_and_report"),
            "print_torrents": _returning_async(None, calls, "print_torrents"),
            "execute_downconversion_tasks": fake_execute,
            **fakes,
        },
    )
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "check_requests", True)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(salmon.uploader.cfg.image, "auto_compress_cover", False)
    monkeypatch.setattr(salmon.uploader.click, "confirm", _returning(True, calls, "confirm"))
    monkeypatch.setattr(salmon.trackers, "choose_tracker", _returning_async(None, calls, "choose_tracker"))
    # Another tracker is configured, so going on to it would be possible.
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", True)
    site = FakeSite()

    anyio.run(
        lambda: salmon.uploader.upload(
            site,  # type: ignore[arg-type]
            "/release",
            5,
            source,
            None,
            (),
            None,
            flac_group=group,
        )
    )
    assert site.torrentgroup_calls == 0
    return calls, transcoded


def test_transcodes_are_uploaded_as_transcodes_of_the_chosen_flac(monkeypatch) -> None:
    _answers(monkeypatch, "*")
    group = _group(_torrent(10, format_="MP3", encoding="320", year=2011), _torrent(11))

    calls, transcoded = _flow(monkeypatch, group)

    # No FLAC upload, no request search, no downconversion confirm, no other tracker.
    assert transcoded == [(["MP3 320", "MP3 V0"], "https://tracker.test/torrents.php?torrentid=11")]
    assert calls == ["mqa_test", "check_spectrals"]


def test_the_source_flac_is_picked_on_the_reviewed_metadata(monkeypatch) -> None:
    _answers(monkeypatch, "*")
    group = _group(_torrent(11), _torrent(12, year=2021))

    _calls, transcoded = _flow(monkeypatch, group, reviewed={"year": 2021})

    assert transcoded == [(["MP3 320", "MP3 V0"], "https://tracker.test/torrents.php?torrentid=12")]


def test_no_source_flac_in_the_edition_stops_before_any_upload(monkeypatch) -> None:
    calls, transcoded = _flow(monkeypatch, _group(_torrent(11, year=2011)))

    assert transcoded == []
    assert "upload_and_report" not in calls


def test_several_source_flacs_stop_with_yes_all_before_any_upload(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)

    calls, transcoded = _flow(monkeypatch, _group(_torrent(11), _torrent(12)))

    assert transcoded == []
    assert "upload_and_report" not in calls


def test_the_usual_checks_still_run(monkeypatch) -> None:
    """MQA, log and spectral checks run as for any upload; the flag skips none of them."""
    _answers(monkeypatch, "*")
    group = _group(_torrent(11, media="CD"))

    calls, transcoded = _flow(monkeypatch, group, source="CD")

    assert calls == ["mqa_test", "log_check", "check_spectrals"]
    assert transcoded


def _default_answer(asked: list[str]):
    async def fake_prompt(text: str, *_args, default: str = "", **_kwargs) -> str:
        asked.append(text)
        return default

    return fake_prompt


def test_formats_the_edition_already_holds_are_dupe_risks_left_out(monkeypatch, capsys) -> None:
    monkeypatch.setattr(salmon.uploader.click, "prompt", _default_answer([]))
    group = _group(_torrent(11), _torrent(12, format_="MP3", encoding="320"))

    _calls, transcoded = _flow(monkeypatch, group)

    assert transcoded == [(["MP3 V0"], "https://tracker.test/torrents.php?torrentid=11")]
    assert "DUPE RISK: this edition already has MP3 320" in capsys.readouterr().out


def test_held_formats_are_left_out_with_yes_all(monkeypatch, capsys) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    group = _group(_torrent(11), _torrent(12, format_="MP3", encoding="V0 (VBR)"))

    _calls, transcoded = _flow(monkeypatch, group)

    assert transcoded == [(["MP3 320"], "https://tracker.test/torrents.php?torrentid=11")]
    assert "DUPE RISK: this edition already has MP3 V0" in capsys.readouterr().out


def test_a_held_format_can_still_be_picked_by_number(monkeypatch) -> None:
    _answers(monkeypatch, "1 2")
    group = _group(_torrent(11), _torrent(12, format_="MP3", encoding="320"))

    _calls, transcoded = _flow(monkeypatch, group)

    assert transcoded == [(["MP3 320", "MP3 V0"], "https://tracker.test/torrents.php?torrentid=11")]


def test_a_held_format_of_another_edition_is_offered(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    group = _group(_torrent(11), _torrent(12, format_="MP3", encoding="320", year=2011))

    _calls, transcoded = _flow(monkeypatch, group)

    assert transcoded == [(["MP3 320", "MP3 V0"], "https://tracker.test/torrents.php?torrentid=11")]


def test_a_held_format_with_another_catalogue_number_is_held() -> None:
    group = _group(_torrent(11), _torrent(12, format_="MP3", encoding="320", catno="0123456789012"))
    formats = {"MP3 320": ("MP3", "320"), "MP3 V0": ("MP3", "V0 (VBR)")}

    assert dupe_checker.held_formats(group, _release(), group["torrents"][0], formats) == {"MP3 320"}


def test_the_16bit_flac_of_the_edition_is_held_for_a_24bit_source(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    group = _group(_torrent(11, encoding="24bit Lossless"), _torrent(12))

    _calls, transcoded = _flow(
        monkeypatch,
        group,
        construct_rls_data=_returning(_release(encoding="24bit Lossless")),
        get_metadata=_returning_async(({**_release(encoding="24bit Lossless"), "cover": None}, None)),
        edit_metadata=_returning_async(("/release", {**_release(encoding="24bit Lossless"), "cover": None}, {}, {})),
        concat_track_data=_returning({"01.flac": {"sample rate": 96000}}),
    )

    assert transcoded == [(["MP3 320", "MP3 V0"], "https://tracker.test/torrents.php?torrentid=11")]


def test_delete_music_folder_never_deletes_the_source(monkeypatch, capsys) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(salmon.uploader.shutil, "rmtree", lambda path, *_a, **_k: deleted.append(path))

    async def delete_answer(*_args, **_kwargs):
        raise salmon.uploader.AbortAndDeleteFolder

    _calls, transcoded = _flow(monkeypatch, _group(_torrent(11)), check_spectrals=delete_answer)

    assert deleted == []
    assert transcoded == []
    assert "Not deleting the music folder" in capsys.readouterr().out
