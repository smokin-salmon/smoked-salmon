"""BaseGazelleApi._request(binary=True): an answer read as bytes, and no more than _MAX_BINARY_BODY of it.

Every request goes to a local fake tracker on 127.0.0.1.
"""

import anyio
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

from salmon.errors import RequestFailedError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi

IMAGE = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x80\xfe" * 64


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "fake-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        self._rate_limiter = AsyncLimiter(100, 1)
        self._authenticated = True


async def _serve(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _get(handler, path: str, **kwargs) -> tuple[base.HttpResponse, int]:
    """GET path from a fake tracker answering with handler; give the response and how many requests it got."""
    requests = 0

    async def counting(request: web.Request) -> web.StreamResponse:
        nonlocal requests
        requests += 1
        return await handler(request)

    runner, url = await _serve(counting)
    api = FakeApi(url)
    try:
        return await api._request("GET", f"{url}{path}", needs_authkey=False, **kwargs), requests
    finally:
        await api.close()
        await runner.cleanup()


async def _image(_request: web.Request) -> web.Response:
    return web.Response(body=IMAGE, content_type="image/jpeg")


def test_a_binary_answer_comes_back_as_its_bytes() -> None:
    response, _ = anyio.run(lambda: _get(_image, "/i/cover.jpg", binary=True))
    assert response.content == IMAGE
    assert response.text == ""


def test_a_text_request_reads_the_answer_as_text_as_before() -> None:
    # Decoded with the charset the answer declares, as resp.text() does: the text callers are unchanged.
    async def latin1(_request: web.Request) -> web.Response:
        return web.Response(body="Café".encode("latin-1"), content_type="text/html", charset="latin-1")

    response, _ = anyio.run(lambda: _get(latin1, "/torrents.php"))
    assert response.text == "Café"
    assert response.content == b""


def test_an_error_answer_to_a_binary_request_is_read_as_text() -> None:
    async def missing(_request: web.Request) -> web.Response:
        return web.json_response({"status": "failure", "error": "no such image"}, status=404)

    with pytest.raises(RequestFailedError, match="no such image"):
        anyio.run(lambda: _get(missing, "/i/gone.jpg", binary=True))


def test_a_declared_length_over_the_cap_is_refused_before_reading(monkeypatch) -> None:
    monkeypatch.setattr(base, "_MAX_BINARY_BODY", len(IMAGE) - 1)
    with pytest.raises(RequestFailedError, match="not reading the rest"):
        anyio.run(lambda: _get(_image, "/i/cover.jpg", binary=True))


def test_a_streamed_answer_stops_being_read_at_the_cap_and_is_not_asked_again(monkeypatch) -> None:
    monkeypatch.setattr(base, "_MAX_BINARY_BODY", 256 * 1024)
    chunk = b"\x00" * (64 * 1024)
    total = 1024  # 64 MiB, far more than the socket buffers between the two ends hold
    written: list[int] = []
    requests: list[int] = []

    async def endless(request: web.Request) -> web.StreamResponse:
        requests.append(1)
        response = web.StreamResponse(headers={"Content-Type": "image/png"})
        await response.prepare(request)
        sent = 0
        try:
            for _ in range(total):
                await response.write(chunk)
                sent += 1
        except (ConnectionError, RuntimeError):
            pass
        finally:
            written.append(sent)
        return response

    async def run() -> None:
        runner, url = await _serve(endless)
        api = FakeApi(url)
        try:
            with pytest.raises(RequestFailedError, match="not reading the rest"):
                await api._request("GET", f"{url}/i/huge.png", needs_authkey=False, binary=True)
            # The server stops once the client has gone.
            with anyio.fail_after(10):
                while not written:
                    await anyio.sleep(0.01)
        finally:
            await api.close()
            await runner.cleanup()

    anyio.run(run)
    assert requests == [1]
    assert written[0] < total
