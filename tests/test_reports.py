from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from aiolimiter import AsyncLimiter
from asyncclick.testing import CliRunner

from salmon.reports import report


def run(*args, input=""):
    return anyio.run(partial(CliRunner().invoke, report, list(args), input=input))


@pytest.mark.parametrize(
    "target",
    [
        "0",
        "-1",
        "abc",
        "https://evil.test/torrents.php?torrentid=1",
        "https://redacted.sh/torrents.php?id=1",
        "https://redacted.sh/torrents.php?torrentid=1&torrentid=2",
    ],
)
def test_invalid_target(target):
    result = run(target, "--tracker", "RED", "--dry-run")
    assert result.exit_code != 0


def test_dry_run_without_audio():
    result = run(
        "123",
        "--tracker",
        "RED",
        "--comment",
        "Source evidence",
        "--source-url",
        "https://example.org/album",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert "Source evidence" in result.output
    assert "Sourced from: https://example.org/album" in result.output


def test_folder_validation_and_offline_preview(tmp_path):
    args = ("123", "--tracker", "OPS", "--comment", "Evidence", "--source-url", "", "--print-only", "--folder")
    track = tmp_path / "01.flac"
    result = run(*args, str(track))
    assert "existing directory" in result.output
    result = run(*args, str(tmp_path))
    assert "no audio files" in result.output
    track.touch()
    result = run(*args, str(track))
    assert "existing directory" in result.output
    result = run(*args, str(tmp_path))
    assert result.exit_code == 0, result.output
    assert "1 audio files" in result.output
    assert "neither generated nor uploaded" in result.output


@pytest.mark.parametrize("tracker", ["RED", "OPS"])
@pytest.mark.parametrize("failure,proceed", [(None, True), ("generate", False), ("upload", False), ("upload", True)])
def test_report_folder_evidence(monkeypatch, tmp_path, tracker, failure, proceed):
    from salmon import cfg, reports

    (tmp_path / "01.flac").touch()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(scratch))
    client = AsyncMock()
    monkeypatch.setattr(reports.trackers, "get_class", lambda _: lambda: client)
    monkeypatch.setattr(reports, "verified_report_media", AsyncMock(return_value="WEB"))
    monkeypatch.setattr(reports, "gather_audio_info", lambda _: {})
    generate = AsyncMock(return_value={1: "01.flac"})
    upload = AsyncMock(return_value={1: ["https://example.org/full", "https://example.org/zoom"]})
    if failure:
        (generate if failure == "generate" else upload).side_effect = ValueError("spectral failure")
    monkeypatch.setattr(reports.spectrals, "generate_spectrals_all", generate)
    monkeypatch.setattr(reports.spectrals, "compress_chosen_spectrals", AsyncMock())
    monkeypatch.setattr(reports.spectrals, "upload_spectrals", upload)
    result = run(
        "123",
        "--tracker",
        tracker,
        "--folder",
        str(tmp_path),
        "--comment",
        "Evidence",
        "--source-url",
        "",
        input="y\n" + ("y\ny\n" if proceed else "n\n"),
    )
    assert result.exit_code == (0 if proceed else 1), result.output
    if failure:
        assert "spectral failure" in result.output
    assert generate.await_args is not None
    if failure != "generate":
        assert upload.await_args is not None
        assert upload.await_args.args[2] == tracker
        assert upload.await_args.kwargs["ask_host"] is False
    # Spectrals are made in tmp_dir, not the system temp folder.
    assert Path(generate.await_args.args[1]).parent == scratch
    if proceed:
        body = client.report_lossy_master.await_args.args[1]
        assert body in result.output
        assert ("[img=https://example.org/full]" in body) == (failure is None)
    else:
        client.report_lossy_master.assert_not_awaited()


def test_ops_does_not_prompt_for_type():
    result = run(
        "https://orpheus.network/torrents.php?torrentid=123",
        "--comment",
        "Evidence",
        "--source-url",
        "https://example.org",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert "Report type" not in result.output


def test_conflicting_tracker():
    result = run(
        "https://redacted.sh/torrents.php?torrentid=1",
        "--tracker",
        "OPS",
        "--comment",
        "Evidence",
        "--source-url",
        "https://example.org",
        "--dry-run",
    )
    assert result.exit_code != 0
    assert "The permalink and --tracker name different trackers." in result.output


def test_red_prompts_and_decline_sends_nothing():
    result = run("123", "--tracker", "RED", input="https://example.org\nEvidence\nn\n")
    assert result.exit_code == 0, result.output
    assert "Report type" not in result.output
    assert "Evidence" in result.output


@pytest.mark.parametrize(
    "tracker,media,expected",
    [("RED", "CD", "lossyapproval"), ("RED", "WEB", "lossywebapproval"), ("OPS", "WEB", "lossyapproval")],
)
@pytest.mark.parametrize("outcome", ["success", "rejected", "unknown", "disconnect", "auth"])
def test_submission_local_tracker(monkeypatch, tracker, media, expected, outcome):
    from aiohttp import web

    from salmon import trackers

    async def scenario():
        forms = []
        requests = []

        async def handler(request):
            requests.append(request.path)
            if request.path == "/ajax.php":
                if request.query.get("action") == "torrent":
                    return web.json_response(
                        {
                            "status": "success",
                            "response": {
                                "group": {"id": 50},
                                "torrent": report_torrent(123, media=media),
                            },
                        }
                    )
                if request.query.get("action") == "torrentgroup":
                    return web.json_response(
                        {
                            "status": "success",
                            "response": {
                                "group": {"id": 50},
                                "torrents": [report_torrent(123)],
                            },
                        }
                    )
                if outcome == "auth":
                    return web.json_response({"status": "failure", "error": "Invalid API key"})
                return web.json_response(
                    {"status": "success", "response": {"authkey": "test-auth", "passkey": "test-pass", "id": 7}}
                )
            if request.path == "/reportsv2.php":
                forms.append(dict(await request.post()))
                if outcome == "disconnect":
                    assert request.transport is not None
                    request.transport.close()
                    return web.Response()
                if outcome == "unknown":
                    return web.Response(status=500)
                if outcome == "rejected":
                    return web.Response(text="Report rejected")
                raise web.HTTPFound("/torrents.php?torrentid=123")
            return web.Response(text="Torrent")

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = web.AppRunner(app)
        await server.setup()
        await web.TCPSite(server, "127.0.0.1", 0).start()
        cls = trackers.get_class(tracker)
        client = cls()
        client.api_key = ""
        client._rate_limiter = AsyncLimiter(1000, 1)
        client.base_url = f"http://127.0.0.1:{server.addresses[0][1]}"
        monkeypatch.setattr(trackers, "get_class", lambda code: lambda: client)
        try:
            result = await CliRunner().invoke(
                report,
                [
                    "123",
                    "--tracker",
                    tracker,
                    "--comment",
                    "Evidence",
                    "--source-url",
                    "https://example.org",
                ],
                input="y\ny\n",
            )
            assert result.exit_code == (0 if outcome == "success" else 1), result.output
            if outcome == "success":
                assert len(requests) == 4
            if outcome == "auth":
                assert forms == []
            else:
                assert len(forms) == 1
                assert forms[0]["type"] == expected
                assert forms[0]["auth"] == "test-auth"
                assert forms[0]["torrentid"] == "123"
                assert forms[0]["extra"] == "Evidence\n\nSourced from: https://example.org\n\n"
            if outcome in ("unknown", "disconnect"):
                assert "Check torrent 123" in str(result.exception)
        finally:
            await server.cleanup()

    anyio.run(scenario)


@pytest.mark.parametrize("host", ["REDACTED.SH", "Orpheus.Network"])
def test_permalink_hostname_is_case_insensitive(host):
    from salmon.reports import parse_target

    expected = "RED" if host == "REDACTED.SH" else "OPS"
    assert parse_target(f"https://{host}/torrents.php?torrentid=123", None) == (123, expected)


def report_torrent(torrent_id, **overrides):
    return {
        "id": torrent_id,
        "userId": 7,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remastered": True,
        "remasterYear": 2020,
        "remasterTitle": "",
        "remasterRecordLabel": "Label",
        "remasterCatalogueNumber": "CAT1",
        **overrides,
    }


@pytest.mark.parametrize("tracker", ["RED", "OPS"])
@pytest.mark.parametrize(
    "case",
    [
        "flac",
        "v0",
        "320",
        "other_user",
        "hidden",
        "missing_user",
        "missing_account",
        "credential_mismatch",
        "wrong_id",
        "missing_media",
    ],
)
def test_conservative_targets(tracker, case):

    import msgspec

    from salmon.errors import RequestError
    from salmon.reports import verified_report_media
    from salmon.trackers import get_class
    from salmon.trackers.base import HttpResponse

    class Client(get_class(tracker)):
        async def _request(self, *args, **kwargs):
            assert kwargs["prefer_api_key"] is False
            return HttpResponse(
                url="http://127.0.0.1/ajax.php",
                status=200,
                text=msgspec.json.encode(
                    {
                        "status": "success",
                        "response": {
                            "id": None if case == "missing_account" else 7,
                            "authkey": "different" if case == "credential_mismatch" else "test-auth",
                        },
                    }
                ).decode(),
            )

        async def api_call(self, action, params=None):
            assert action == "torrent", "No group or transcode discovery allowed"
            target = report_torrent(123)
            if case in ("v0", "320"):
                target.update(format="MP3", encoding="V0 (VBR)" if case == "v0" else "320")
            if case == "other_user":
                target["userId"] = 8
            if case == "hidden":
                target["userId"] = 0
            if case == "missing_user":
                target.pop("userId")
            if case == "wrong_id":
                target["id"] = 456
            if case == "missing_media":
                target.pop("media")
            return {"group": {"id": 50}, "torrent": target}

    client = Client()
    client.api_key = "test-key"
    client.authkey = "test-auth"
    if case == "flac":
        assert anyio.run(verified_report_media, client, 123) == "WEB"
    else:
        with pytest.raises(RequestError):
            anyio.run(verified_report_media, client, 123)


@pytest.mark.parametrize("uploader,passes", [(8, False), (7, True)])
def test_cookie_only_ownership(uploader, passes):
    from salmon.errors import RequestError
    from salmon.reports import verified_report_media
    from salmon.trackers import get_class

    class Client(get_class("RED")):
        async def _request(self, *args, **kwargs):
            raise AssertionError("Cookie-only mode reuses the index from authenticate")

        async def api_call(self, action, params=None):
            return {"group": {"id": 50}, "torrent": report_torrent(123, userId=uploader)}

    client = Client()
    client.api_key = ""
    client.account_id = 7
    if passes:
        assert anyio.run(verified_report_media, client, 123) == "WEB"
    else:
        with pytest.raises(RequestError):
            anyio.run(verified_report_media, client, 123)
