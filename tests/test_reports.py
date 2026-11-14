from functools import partial

import anyio
import pytest
from asyncclick.testing import CliRunner

from salmon.reports import report


def run(*args, input=""):
    return anyio.run(partial(CliRunner().invoke, report, ["lossy", *args], input=input))


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
        client.base_url = f"http://127.0.0.1:{server.addresses[0][1]}"
        monkeypatch.setattr(trackers, "get_class", lambda code: lambda: client)
        try:
            result = await CliRunner().invoke(
                report,
                [
                    "lossy",
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
                input="y\n",
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
