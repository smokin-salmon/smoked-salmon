"""The test run refuses connections that leave the machine."""

import asyncio
import socket

import aiohttp
import pytest
from aiohttp import web
from conftest import NetworkBlockedError  # pyright: ignore[reportMissingImports]


def test_a_public_address_is_refused_and_named() -> None:
    with socket.socket() as sock, pytest.raises(NetworkBlockedError, match=r"93\.184\.215\.14:80"):
        sock.connect(("93.184.215.14", 80))


def test_connect_ex_is_refused_too() -> None:
    with socket.socket() as sock, pytest.raises(NetworkBlockedError, match=r"93\.184\.215\.14:80"):
        sock.connect_ex(("93.184.215.14", 80))


def test_an_ipv6_public_address_is_refused() -> None:
    with socket.socket(socket.AF_INET6) as sock, pytest.raises(NetworkBlockedError, match="2001:db8::1"):
        sock.connect(("2001:db8::1", 80, 0, 0))


def test_an_aiohttp_request_to_a_public_url_is_refused() -> None:
    async def run() -> None:
        async with aiohttp.ClientSession() as session:
            await session.get("http://93.184.215.14/")

    with pytest.raises(aiohttp.ClientConnectionError) as excinfo:
        asyncio.run(run())
    assert isinstance(excinfo.value.__cause__ or excinfo.value, (NetworkBlockedError, aiohttp.ClientConnectorError))
    assert "93.184.215.14" in str(excinfo.value)


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

    assert asyncio.run(run()) == "ok"


@pytest.mark.network
def test_the_network_marker_opts_out() -> None:
    with socket.socket() as sock:
        sock.settimeout(0.01)
        try:
            sock.connect(("192.0.2.1", 80))  # TEST-NET-1: never answers
        except NetworkBlockedError:
            pytest.fail("the network marker did not lift the guard")
        except OSError:
            pass
