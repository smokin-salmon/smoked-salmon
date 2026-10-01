"""The test run refuses connections that leave the machine."""

import asyncio
import socket

import aiohttp
import conftest  # pyright: ignore[reportMissingImports]
import pytest
from aiohttp import web
from conftest import NetworkBlockedError  # pyright: ignore[reportMissingImports]


def test_a_public_address_is_refused_and_named() -> None:
    with socket.socket() as sock, pytest.raises(NetworkBlockedError, match=r"192\.0\.2\.1:80"):
        sock.connect(("192.0.2.1", 80))


def test_connect_ex_is_refused_too() -> None:
    with socket.socket() as sock, pytest.raises(NetworkBlockedError, match=r"192\.0\.2\.1:80"):
        sock.connect_ex(("192.0.2.1", 80))


def test_an_ipv6_public_address_is_refused() -> None:
    with socket.socket(socket.AF_INET6) as sock, pytest.raises(NetworkBlockedError, match="2001:db8::1"):
        sock.connect(("2001:db8::1", 80, 0, 0))


def test_an_aiohttp_request_to_a_public_url_is_refused() -> None:
    async def run() -> None:
        async with aiohttp.ClientSession() as session:
            await session.get("http://198.51.100.7/")

    with pytest.raises(aiohttp.ClientConnectionError) as excinfo:
        asyncio.run(run())
    chain = []
    error: BaseException | None = excinfo.value
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(err, NetworkBlockedError) for err in chain), chain
    assert "198.51.100.7" in str(excinfo.value)


def test_loopback_still_works() -> None:
    async def run() -> str:
        async def handle(request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession() as session, session.get(f"http://127.0.0.1:{port}/") as response:
                return await response.text()
        finally:
            await runner.cleanup()

    result = asyncio.run(run())
    assert result == "ok"


@pytest.mark.network
def test_the_network_marker_opts_out(monkeypatch) -> None:
    # Stub the real connect: the opt-out is proven without sending a packet.
    seen = []
    monkeypatch.setattr(conftest, "_real_connect", lambda sock, address: seen.append(address))
    monkeypatch.setattr(conftest, "_real_connect_ex", lambda sock, address: seen.append(address) or 0)
    with socket.socket() as sock:
        sock.connect(("203.0.113.9", 80))
        assert sock.connect_ex(("203.0.113.9", 81)) == 0
    assert seen == [("203.0.113.9", 80), ("203.0.113.9", 81)]
