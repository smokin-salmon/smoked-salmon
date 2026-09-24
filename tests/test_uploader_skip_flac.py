from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner

import salmon.trackers
import salmon.uploader


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


def _torrent(torrent_id: int, media: str = "WEB", format_: str = "FLAC", encoding: str = "Lossless") -> dict:
    return {
        "id": torrent_id,
        "media": media,
        "format": format_,
        "encoding": encoding,
        "remastered": True,
        "remasterYear": 2020,
        "remasterTitle": "",
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": f"CAT{torrent_id}",
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


def _answers(monkeypatch, *answers: str) -> list[str]:
    """Answer the prompts with answers in turn. Returns the prompts asked."""
    asked: list[str] = []

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        asked.append(text)
        return answers[len(asked) - 1]

    monkeypatch.setattr(salmon.uploader.click, "prompt", fake_prompt)
    return asked


def _choose(group: dict[str, Any], media: str = "WEB", encoding: str = "Lossless") -> str | None:
    return anyio.run(salmon.uploader.choose_source_flac, FakeSite(), group, media, encoding)  # type: ignore[arg-type]


def test_one_matching_flac_is_the_transcode_source(monkeypatch) -> None:
    asked = _answers(monkeypatch)
    group = _group(
        _torrent(10, format_="MP3", encoding="320"),
        _torrent(11, media="CD"),
        _torrent(12, encoding="24bit Lossless"),
        _torrent(13),
    )

    assert _choose(group) == "https://tracker.test/torrents.php?torrentid=13"
    assert asked == []


def test_one_matching_flac_needs_no_prompt_with_yes_all(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)

    assert _choose(_group(_torrent(13))) == "https://tracker.test/torrents.php?torrentid=13"


def test_24bit_release_is_a_transcode_of_the_24bit_flac(monkeypatch) -> None:
    group = _group(_torrent(11), _torrent(12, encoding="24bit Lossless"))

    assert _choose(group, encoding="24bit Lossless") == "https://tracker.test/torrents.php?torrentid=12"


def test_no_matching_flac_stops(monkeypatch) -> None:
    asked = _answers(monkeypatch)
    group = _group(_torrent(10, format_="MP3", encoding="V0 (VBR)"), _torrent(11, media="CD"))

    assert _choose(group) is None
    assert asked == []


def test_several_matching_flacs_ask_which_one(monkeypatch) -> None:
    asked = _answers(monkeypatch, "3", "x", "2")
    group = _group(_torrent(11), _torrent(10, format_="MP3", encoding="320"), _torrent(12))

    assert _choose(group) == "https://tracker.test/torrents.php?torrentid=12"
    assert len(asked) == 3


def test_several_matching_flacs_can_abort(monkeypatch) -> None:
    _answers(monkeypatch, "a")

    assert _choose(_group(_torrent(11), _torrent(12))) is None


def test_several_matching_flacs_stop_with_yes_all(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    asked = _answers(monkeypatch)

    assert _choose(_group(_torrent(11), _torrent(12))) is None
    assert asked == []


@pytest.mark.parametrize(
    ("format_", "encoding"),
    [("MP3", "320"), ("MP3", "V0 (VBR)"), ("AAC", "256")],
)
def test_lossy_release_stops_before_any_check_or_upload(monkeypatch, format_: str, encoding: str) -> None:
    calls: list[str] = []

    def record(name: str, result: Any = None):
        def fake(*_args, **_kwargs) -> Any:
            calls.append(name)
            return result

        return fake

    def record_async(name: str):
        async def fake(*_args, **_kwargs) -> None:
            calls.append(name)

        return fake

    monkeypatch.setattr(salmon.uploader, "gather_audio_info", record("gather_audio_info", {}))
    monkeypatch.setattr(salmon.uploader, "check_hybrid", record("check_hybrid", False))
    monkeypatch.setattr(salmon.uploader, "standardize_tags", record("standardize_tags"))
    monkeypatch.setattr(salmon.uploader, "gather_tags", record("gather_tags", {}))
    monkeypatch.setattr(
        salmon.uploader, "construct_rls_data", record("rls_data", {"format": format_, "encoding": encoding})
    )
    for name in ("mqa_test", "choose_source_flac", "check_existing_group", "upload_and_report"):
        monkeypatch.setattr(salmon.uploader, name, record_async(name))
    site = FakeSite(_group(_torrent(11)))

    async def run() -> None:
        await salmon.uploader.upload(
            site,  # type: ignore[arg-type]
            "/release",
            5,
            "WEB",
            None,
            (),
            None,
            flac_group=_group(_torrent(11)),
        )

    anyio.run(run)

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


def test_transcodes_are_uploaded_as_transcodes_of_the_chosen_flac(monkeypatch) -> None:
    calls: list[str] = []
    transcoded_from: list[str] = []

    def returning(result: Any = None, record: str | None = None):
        def fake(*_args, **_kwargs) -> Any:
            if record:
                calls.append(record)
            return result

        return fake

    def returning_async(result: Any = None, record: str | None = None):
        async def fake(*_args, **_kwargs) -> Any:
            if record:
                calls.append(record)
            return result

        return fake

    async def fake_execute(_tasks, _path, *args) -> None:
        transcoded_from.append(args[-1])

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            pass

    rls_data = {"format": "FLAC", "encoding": "Lossless", "artists": [], "title": "Album", "catno": None}
    metadata = {"cover": None, "artists": [], "title": "Album", "catno": None}
    for name, fake in {
        "gather_audio_info": returning({}),
        "check_hybrid": returning(False),
        "standardize_tags": returning(),
        "gather_tags": returning({}),
        "construct_rls_data": returning(rls_data),
        "mqa_test": returning_async(),
        "check_spectrals": returning_async((False, None)),
        "get_metadata": returning_async((metadata, None)),
        "edit_metadata": returning_async(("/release", metadata, {}, {})),
        "concat_track_data": returning({}),
        "get_spectrals_path": returning("/spectrals"),
        "handle_spectrals_upload_and_deletion": returning_async(),
        "resolve_cover_url": returning_async((True, None)),
        "UploadManager": FakeUploadManager,
        "check_requests": returning_async(record="check_requests"),
        "upload_and_report": returning_async(record="upload_and_report"),
        "print_torrents": returning_async(record="print_torrents"),
        "prompt_downconversion_choice": returning_async([{"name": "MP3 V0"}]),
        "execute_downconversion_tasks": fake_execute,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "check_requests", True)
    monkeypatch.setattr(salmon.uploader.click, "confirm", returning(True, record="confirm"))
    monkeypatch.setattr(salmon.trackers, "choose_tracker", returning_async(record="choose_tracker"))
    site = FakeSite()

    async def run() -> None:
        await salmon.uploader.upload(
            site,  # type: ignore[arg-type]
            "/release",
            5,
            "WEB",
            None,
            (),
            None,
            flac_group=_group(_torrent(10, format_="MP3", encoding="320"), _torrent(11)),
        )

    anyio.run(run)

    # The FLAC is not uploaded, requests are not searched, the downconversion menu opens without asking
    # first, and the transcodes name the FLAC they come from.
    assert transcoded_from == ["https://tracker.test/torrents.php?torrentid=11"]
    assert calls == []
    assert site.torrentgroup_calls == 0
