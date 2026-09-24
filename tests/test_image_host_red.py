"""RED's image host, reached through a RED tracker client (#469).

A cover upload to RED's image host is a request to RED like any other: it goes through the
shared rate limiter, with the configured user agent, the API key and no cookie, and it is not
sent again once it may have reached RED. Tests talk only to a local fake RED.
"""

import asyncio
from pathlib import Path

import anyio
import msgspec
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

import salmon.uploader
from salmon import cfg
from salmon.config.validations import ImageUploader
from salmon.errors import ImageUploadFailed
from salmon.images import red, upload_cover
from salmon.trackers.base import BaseGazelleApi
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi

API_KEY = "red-api-key"
USER_AGENT = "salmon-test-agent"
COVER_URL = "https://redacted.sh/image/cover.jpg"
REASON = "The image is too large"
BARE_URL = "https://redacted.sh/i/abc123.jpg"
QUERY_URL = "https://redacted.sh/i/abc123.jpg?imgauth=deadbeef&size=large"


class CountingLimiter(AsyncLimiter):
    def __init__(self) -> None:
        super().__init__(100, 1)
        self.acquired = 0

    async def acquire(self, amount: float = 1) -> None:
        await super().acquire(amount)
        self.acquired += 1


class FakeRed:
    """A local RED answering the index, a search and image uploads, with a log of what it received."""

    def __init__(self, *uploads: str, url: str = COVER_URL) -> None:
        # How each image upload in turn is answered: "ok", "reject" or "drop".
        self.uploads = list(uploads) or ["ok"]
        self.hits: list[str] = []
        # (User-Agent, Authorization, Cookie) of each request.
        self.sent: set[tuple[str | None, str | None, str | None]] = set()
        # The URL returned by a successful upload.
        self.url = url

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", self.ajax)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def ajax(self, request: web.Request) -> web.Response:
        action = request.query["action"]
        self.hits.append(f"{request.method} {action}")
        headers = request.headers
        self.sent.add((headers.get("User-Agent"), headers.get("Authorization"), headers.get("Cookie")))
        response: dict = {}
        if action == "index":
            response = {"authkey": "a", "passkey": "p"}
        elif action == "browse":
            response = {"results": []}
        elif action == "upload_image":
            assert (await request.post())["file"]
            answer = self.uploads.pop(0)
            if answer == "reject":
                return web.json_response({"status": "failure", "error": REASON}, status=400)
            if answer == "drop":
                # RED has the image, but the connection drops before its answer.
                assert request.transport is not None
                request.transport.close()
            response = {"url": self.url}
        return web.json_response({"status": "success", "response": response})


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cfg.tracker.red is not None
    monkeypatch.setattr(cfg.tracker.red, "api_key", API_KEY)
    monkeypatch.setattr(cfg.upload, "user_agent", USER_AGENT)
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    image = {"cover_uploader": "imgbox", "red": {"cover_uploader": "red"}, "ops": {"cover_uploader": "red"}}
    monkeypatch.setattr(cfg, "image", msgspec.convert(image, ImageUploader))


@pytest.fixture(autouse=True)
def limiter(monkeypatch: pytest.MonkeyPatch) -> CountingLimiter:
    # Class-wide, as in the real code, so every RED client shares it.
    limiter = CountingLimiter()
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", limiter)
    return limiter


@pytest.fixture
def release(tmp_path: Path) -> Path:
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return tmp_path


def _point_red_at(monkeypatch: pytest.MonkeyPatch, base_url: str) -> list[RedApi]:
    """Make every RedApi talk to the fake RED. Returns the clients made."""
    clients: list[RedApi] = []
    real_init = RedApi.__init__

    def local_init(self: RedApi) -> None:
        real_init(self)
        self.base_url = base_url
        clients.append(self)

    monkeypatch.setattr(RedApi, "__init__", local_init)
    return clients


def test_a_cover_for_red_goes_up_through_the_runs_client(monkeypatch, release, limiter) -> None:
    fake = FakeRed()

    async def run() -> tuple[bool, str | None]:
        clients = _point_red_at(monkeypatch, await fake.start())
        api = RedApi()
        try:
            # The group search, earlier in the run.
            await api.api_call("browse", {"searchstr": "artist album"})
            result = await salmon.uploader.resolve_cover_url(api, None, {}, str(release), None, False)
            assert clients == [api]
            return result
        finally:
            await api.close()
            await fake.runner.cleanup()

    assert anyio.run(run) == (True, COVER_URL)
    assert fake.hits == ["GET index", "GET browse", "POST upload_image"]
    assert limiter.acquired == len(fake.hits)
    assert fake.sent == {(USER_AGENT, API_KEY, None)}


def test_a_cover_for_ops_goes_up_through_one_red_client_of_its_own(monkeypatch, release, limiter) -> None:
    fake = FakeRed("reject", "ok")

    async def retry(*_args, **_kwargs) -> str:
        return "r"

    monkeypatch.setattr(salmon.uploader.click, "prompt", retry)

    async def run() -> tuple[tuple[bool, str | None], list[RedApi]]:
        clients = _point_red_at(monkeypatch, await fake.start())
        try:
            return await salmon.uploader.resolve_cover_url(OpsApi(), None, {}, str(release), None, False), clients
        finally:
            await fake.runner.cleanup()

    result, clients = anyio.run(run)
    assert result == (True, COVER_URL)
    # No index: the image upload needs the API key only, not the authkey.
    assert fake.hits == ["POST upload_image", "POST upload_image"]
    assert limiter.acquired == len(fake.hits)
    assert fake.sent == {(USER_AGENT, API_KEY, None)}
    # One client for the retry too, closed once the cover is up.
    assert len(clients) == 1
    assert clients[0]._session is None


def test_a_rejected_image_fails_with_reds_reason_printed_once(monkeypatch, release, capsys) -> None:
    fake = FakeRed("reject", "reject")
    cover = str(release / "cover.jpg")

    async def run() -> str | None:
        _point_red_at(monkeypatch, await fake.start())
        api = RedApi()
        try:
            with pytest.raises(ImageUploadFailed, match=f"RED rejected the image: {REASON}"):
                await red.ImageUploader(api).upload_file(cover)
            capsys.readouterr()
            return await upload_cover(cover, "red", api)
        finally:
            await api.close()
            await fake.runner.cleanup()

    assert anyio.run(run) is None
    assert capsys.readouterr().out.count(REASON) == 1
    assert fake.hits == ["POST upload_image", "POST upload_image"]


def test_gathered_uploads_through_one_client_do_not_cut_each_other_off(monkeypatch, release, limiter) -> None:
    # `salmon images up -i red` gathers its uploads through one uploader, and so one client.
    fake = FakeRed("ok", "ok", "ok")

    async def run() -> list[tuple[str, None]]:
        _point_red_at(monkeypatch, await fake.start())
        api = RedApi()
        uploader = red.ImageUploader(api)
        try:
            return await asyncio.gather(*(uploader.upload_file(str(release / "cover.jpg")) for _ in range(3)))
        finally:
            await api.close()
            await fake.runner.cleanup()

    assert anyio.run(run) == [(COVER_URL, None)] * 3
    assert fake.hits == ["POST upload_image"] * 3
    assert limiter.acquired == len(fake.hits)


def test_a_dropped_connection_after_the_upload_is_not_retried(monkeypatch, release) -> None:
    fake = FakeRed("drop")

    async def run() -> None:
        _point_red_at(monkeypatch, await fake.start())
        api = RedApi()
        try:
            with pytest.raises(ImageUploadFailed, match="Network error"):
                await red.ImageUploader(api).upload_file(str(release / "cover.jpg"))
        finally:
            await api.close()
            await fake.runner.cleanup()

    anyio.run(run)
    assert fake.hits == ["POST upload_image"]


@pytest.mark.parametrize("url", [BARE_URL, QUERY_URL])
def test_the_url_red_returns_is_passed_through_unchanged(monkeypatch, release, url: str) -> None:
    # Pins what #428 promised: RED's response.url comes back exactly as RED sent it, bare or
    # with a query string, with nothing added or stripped (no imgauth appended, for example).
    fake = FakeRed("ok", url=url)

    async def run() -> tuple[str, None]:
        _point_red_at(monkeypatch, await fake.start())
        api = RedApi()
        try:
            return await red.ImageUploader(api).upload_file(str(release / "cover.jpg"))
        finally:
            await api.close()
            await fake.runner.cleanup()

    assert anyio.run(run) == (url, None)
