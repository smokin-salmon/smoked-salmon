"""DICMusic takes no API key: tracker.dic.api_key is ignored, with a warning (#615)."""

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

import salmon.trackers
import salmon.trackers.dic as dic
from salmon import cfg
from salmon.commands import checkconf
from salmon.common import UploadFiles
from salmon.trackers.dic import DICApi

IGNORED = "tracker.dic.api_key is ignored: DIC does not take API keys, salmon uses its session cookie."


@pytest.fixture(autouse=True)
def _dic_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cfg.tracker.dic is not None
    monkeypatch.setattr(cfg.tracker.dic, "api_key", "a-dic-api-key")
    monkeypatch.setattr(dic, "_api_key_warned", False)


def _dic(runner: web.AppRunner) -> DICApi:
    api = DICApi()
    api.base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    # Shadow the class-wide limiter: each test runs on its own event loop.
    api._rate_limiter = AsyncLimiter(100, 1)
    return api


def _serve_index(app: web.Application, seen: list[web.Request]) -> None:
    async def handle_ajax(request: web.Request) -> web.Response:
        seen.append(request)
        return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})

    app.router.add_route("*", "/ajax.php", handle_ajax)


async def _serve(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


async def _api_call_headers() -> list[tuple[str | None, str | None]]:
    seen: list[web.Request] = []
    app = web.Application()
    _serve_index(app, seen)
    runner = await _serve(app)
    api = _dic(runner)
    try:
        await api.api_call("index")
        return [(request.headers.get("Authorization"), request.headers.get("Cookie")) for request in seen]
    finally:
        await api.close()
        await runner.cleanup()


def test_dic_requests_carry_the_session_cookie_and_no_authorization_header() -> None:
    sent = anyio.run(_api_call_headers)

    assert sent
    for authorization, cookie in sent:
        assert authorization is None
        assert cookie is not None
        assert "session=" in cookie


async def _upload_paths() -> list[str]:
    paths: list[str] = []
    app = web.Application()
    _serve_index(app, [])

    async def handle_upload(request: web.Request) -> web.Response:
        paths.append(request.path_qs)
        return web.Response(
            text=(
                '<a class="tooltip" href="torrents.php?torrentid=7">t</a>'
                '<a class="brackets" href="upload.php?groupid=3">g</a>'
            )
        )

    app.router.add_route("POST", "/upload.php", handle_upload)
    runner = await _serve(app)
    api = _dic(runner)
    api.skip_upload_marks()
    try:
        result = await api.upload({"title": "x"}, UploadFiles(torrent_data=b"torrent"))
        assert result == (7, 3)
        return paths
    finally:
        await api.close()
        await runner.cleanup()


def test_dic_uploads_go_through_the_site_page() -> None:
    paths = anyio.run(_upload_paths)

    assert paths == ["/upload.php"]


def test_dic_never_has_an_api_key() -> None:
    api = DICApi()

    assert not api.api_key


def test_the_ignored_key_is_warned_about_once_per_run(capsys: pytest.CaptureFixture[str]) -> None:
    DICApi()
    DICApi()

    assert capsys.readouterr().out.count(IGNORED) == 1


def test_no_key_no_warning(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cfg.tracker.dic is not None
    monkeypatch.setattr(cfg.tracker.dic, "api_key", None)

    DICApi()

    captured = capsys.readouterr()
    assert IGNORED not in captured.out + captured.err


async def _checkconf_dic(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    seen: list[web.Request] = []
    app = web.Application()
    _serve_index(app, seen)
    runner = await _serve(app)
    made: list[DICApi] = []

    def tracker_class(_tracker: str):
        def make() -> DICApi:
            made.append(_dic(runner))
            return made[-1]

        return make

    monkeypatch.setattr(salmon.trackers, "get_class", tracker_class)
    try:
        assert checkconf.callback is not None
        await checkconf.callback(tracker="DIC", metadata=False, seedbox=False, reset=False)
        return [request.headers.get("Authorization") for request in seen]
    finally:
        for api in made:
            await api.close()
        await runner.cleanup()


def test_checkconf_says_the_key_is_ignored_and_tests_only_the_cookie(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    authorizations = anyio.run(_checkconf_dic, monkeypatch)

    out = capsys.readouterr().out
    assert out.count(IGNORED) == 1
    assert "Session cookie OK" in out
    assert "API authentication" not in out
    assert authorizations
    assert all(authorization is None for authorization in authorizations)
