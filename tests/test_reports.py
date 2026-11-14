from functools import partial

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
        "--type",
        "web",
        "--comment",
        "Source evidence",
        "--source-url",
        "https://example.org/album",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert "Source evidence" in result.output
    assert "Sourced from: https://example.org/album" in result.output


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
    assert run("https://redacted.sh/torrents.php?torrentid=1", "--tracker", "OPS").exit_code != 0


def test_red_prompts_and_decline_sends_nothing():
    result = run("123", "--tracker", "RED", input="web\nhttps://example.org\nEvidence\nn\n")
    assert result.exit_code == 0, result.output
    assert "Report type" in result.output
    assert "Evidence" in result.output


@pytest.mark.parametrize(
    "tracker,kind,expected",
    [("RED", "master", "lossyapproval"), ("RED", "web", "lossywebapproval"), ("OPS", "web", "lossyapproval")],
)
@pytest.mark.parametrize("outcome", ["success", "rejected", "unknown", "disconnect", "auth"])
def test_submission_local_tracker(monkeypatch, tracker, kind, expected, outcome):
    from aiohttp import web

    from salmon import trackers

    async def scenario():
        forms = []

        async def handler(request):
            if request.path == "/ajax.php":
                if request.query.get("action") == "torrent":
                    return web.json_response(
                        {
                            "status": "success",
                            "response": {
                                "group": {"id": 50},
                                "torrent": report_torrent(123),
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
                    {"status": "success", "response": {"authkey": "test-auth", "passkey": "test-pass"}}
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
                    "--type",
                    kind,
                    "--comment",
                    "Evidence",
                    "--source-url",
                    "https://example.org",
                ],
                input="y\ny\n",
            )
            assert result.exit_code == (0 if outcome == "success" else 1), result.output
            if outcome == "auth":
                assert forms == []
            else:
                assert len(forms) == 1
                assert forms[0]["type"] == expected
                assert forms[0]["auth"] == "test-auth"
                assert forms[0]["torrentid"] == "123"
                assert forms[0]["extra"] == "Evidence\n\nSourced from: https://example.org\n\n"
            if outcome in ("unknown", "disconnect"):
                assert "may have been submitted" in result.output
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
        "success",
        "from_mp3",
        "original",
        "at_limit",
        "decline",
        "limit",
        "missing_user",
        "hidden_user",
        "missing_media",
        "missing_group",
        "missing_target",
        "changed_uploader",
        "lookup_failure",
        "rejected",
        "unknown",
        "disconnect",
    ],
)
def test_report_related_torrents(monkeypatch, tracker, case):
    from aiohttp import web

    from salmon import trackers

    async def scenario():
        forms = []
        lookups = []
        target, rows = report_group(case)

        async def handler(request):
            if request.path == "/ajax.php":
                action = request.query.get("action")
                if action == "index":
                    data = {"authkey": "test-auth", "passkey": "test-pass"}
                elif action == "torrent":
                    lookups.append((action, request.query["id"]))
                    data = {"group": {} if case == "missing_group" else {"id": 50}, "torrent": target}
                elif action == "torrentgroup":
                    lookups.append((action, request.query["id"]))
                    if case == "lookup_failure":
                        return web.json_response({"status": "failure", "error": "Not found"})
                    data = {"group": {"id": 50, "year": 2019, "recordLabel": "Original label"}, "torrents": rows}
                else:
                    raise AssertionError(action)
                return web.json_response({"status": "success", "response": data})
            if request.path == "/reportsv2.php":
                form = dict(await request.post())
                forms.append(form)
                if form["torrentid"] == "124":
                    if case == "rejected":
                        return web.Response(text="Report rejected")
                    if case == "unknown":
                        return web.Response(status=500)
                    if case == "disconnect":
                        assert request.transport is not None
                        request.transport.close()
                        return web.Response()
                raise web.HTTPFound("/torrents.php?torrentid=" + form["torrentid"])
            return web.Response(text="Torrent")

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = web.AppRunner(app)
        await server.setup()
        await web.TCPSite(server, "127.0.0.1", 0).start()
        client = trackers.get_class(tracker)()
        # These tests count requests; the shared limiter has its own timing tests.
        client._rate_limiter = AsyncLimiter(1000, 1)
        client.base_url = f"http://127.0.0.1:{server.addresses[0][1]}"
        monkeypatch.setattr(trackers, "get_class", lambda code: lambda: client)
        try:
            result = await CliRunner().invoke(
                report,
                ["123", "--tracker", tracker, "--type", "web", "--comment", "Evidence", "--source-url", ""],
                input="y\nn\n" if case == "decline" else "y\ny\n",
            )
            assert result.exit_code == (
                0 if case in ("success", "from_mp3", "original", "at_limit", "decline") else 1
            ), result.output
            expected_ids = (
                ["123", "124", "125"]
                if case in ("success", "from_mp3", "original")
                else (["123", "124"] if case in ("rejected", "unknown", "disconnect") else [])
            )
            if case == "at_limit":
                expected_ids = ["123", *map(str, range(200, 219))]
            assert [form["torrentid"] for form in forms] == expected_ids
            assert all(form["extra"] == "Evidence\n\n" for form in forms)
            assert len(lookups) <= 2
            if case in ("success", "decline", "rejected", "unknown", "disconnect"):
                assert lookups == [("torrent", "123"), ("torrentgroup", "50")]
                assert "123, 124, 125" in result.output
            if case in ("rejected", "unknown", "disconnect"):
                assert "Already reported: 123" in result.output
                assert "Not attempted: 125" in result.output
            if case in ("unknown", "disconnect"):
                assert "124 may have been submitted" in result.output
        finally:
            await server.cleanup()

    anyio.run(scenario)


def report_group(case):
    target = report_torrent(123)
    if case == "missing_user":
        target.pop("userId")
    if case == "missing_media":
        target.pop("media")
    if case == "hidden_user":
        target["userId"] = 0
    if case == "from_mp3":
        target.update(format="MP3", encoding="V2 (VBR)")
    rows = [
        target,
        report_torrent(124, format="MP3", encoding="320"),
        report_torrent(125, format="MP3", encoding="V0 (VBR)"),
        report_torrent(124, format="MP3", encoding="320"),
        report_torrent(126, userId=8),
        report_torrent(127, remasterYear=2021),
        report_torrent(128, media="CD"),
        report_torrent(129, remasterCatalogueNumber="OTHER"),
        report_torrent(130, userId=None),
        report_torrent(131, remasterTitle="Deluxe"),
        report_torrent(132, remasterRecordLabel="Other label"),
    ]
    if case == "from_mp3":
        rows[1] = report_torrent(124, encoding="24bit Lossless")
    if case == "original":
        for row in rows[:4]:
            row.update(
                remastered=False,
                remasterYear=0,
                remasterTitle="",
                remasterRecordLabel="",
                remasterCatalogueNumber="",
            )
    if case == "limit":
        rows = [target, *(report_torrent(i) for i in range(200, 220))]
    if case == "at_limit":
        rows = [target, *(report_torrent(i) for i in range(200, 219))]
    if case == "missing_target":
        rows = rows[1:]
    if case == "changed_uploader":
        rows[0] = report_torrent(123, userId=8)
    return target, rows
