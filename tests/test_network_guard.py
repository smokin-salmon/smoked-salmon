"""The test run refuses connections that leave the machine."""

import asyncio
import socket

import aiohttp
import conftest
import curl_cffi
import pytest
from aiohttp import web
from conftest import NetworkBlockedError


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


def test_a_curl_cffi_request_to_a_public_url_is_refused() -> None:
    # libcurl opens its own sockets, which the socket guard does not see (Bandcamp's browser retry, #985).
    async def run() -> None:
        async with curl_cffi.AsyncSession() as session:
            await session.get("http://198.51.100.7/", timeout=1)

    with pytest.raises(NetworkBlockedError, match=r"198\.51\.100\.7"):
        asyncio.run(run())
    with curl_cffi.Session() as session, pytest.raises(NetworkBlockedError, match=r"198\.51\.100\.7"):
        session.get("http://198.51.100.7/", timeout=1)


def test_a_curl_cffi_request_through_a_public_proxy_is_refused() -> None:
    async def run() -> None:
        async with curl_cffi.AsyncSession() as session:
            await session.get("http://127.0.0.1:9/", proxies={"all": "socks5h://203.0.113.4:1080"}, timeout=1)

    with pytest.raises(NetworkBlockedError, match=r"203\.0\.113\.4"):
        asyncio.run(run())


def test_a_curl_cffi_proxy_without_a_scheme_is_refused_and_named_without_its_password() -> None:
    with curl_cffi.Session() as session, pytest.raises(NetworkBlockedError) as excinfo:
        session.get("http://127.0.0.1:9/", proxy="user:secret@203.0.113.4:1080", timeout=1)
    assert "203.0.113.4:1080" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize("proxy", ["127.0.0.1:9", "localhost:9", "http://[::1]:9"])
def test_a_curl_cffi_request_through_a_loopback_proxy_is_let_through(proxy: str) -> None:
    # Nothing listens on port 9: curl fails to connect, which the guard did not stop.
    async def run() -> None:
        async with curl_cffi.AsyncSession() as session:
            await session.get("http://192.0.2.1/", proxy=proxy, timeout=2)

    with pytest.raises(curl_cffi.CurlError):
        asyncio.run(run())


def test_only_the_curl_cffi_proxy_for_the_urls_scheme_is_judged() -> None:
    proxies: curl_cffi.ProxySpec = {"http": "http://127.0.0.1:9", "https": "http://203.0.113.4:3128"}
    with curl_cffi.Session() as session, pytest.raises(curl_cffi.CurlError):
        session.get("http://192.0.2.1/", proxies=proxies, timeout=2)
    with curl_cffi.Session() as session, pytest.raises(NetworkBlockedError, match=r"203\.0\.113\.4:3128"):
        session.get("https://192.0.2.1/", proxies=proxies, timeout=2)


@pytest.mark.parametrize("variable", ["https_proxy", "HTTPS_PROXY", "ALL_PROXY"])
def test_a_curl_cffi_request_through_the_environments_proxy_is_judged_by_it(monkeypatch, variable: str) -> None:
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "http://203.0.113.4:3128")

    with curl_cffi.Session() as session, pytest.raises(NetworkBlockedError, match=r"203\.0\.113\.4:3128"):
        session.get("https://127.0.0.1:9/", timeout=1)


def test_a_curl_cffi_request_to_loopback_still_works() -> None:
    async def run() -> int:
        async def handle(request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = runner.addresses[0][1]
            async with curl_cffi.AsyncSession() as session:
                response = await session.get(f"http://127.0.0.1:{port}/", proxies={"all": ""}, timeout=5)
                return response.status_code
        finally:
            await runner.cleanup()

    assert asyncio.run(run()) == 200


@pytest.mark.network
def test_the_network_marker_opts_out(monkeypatch) -> None:
    # Stub the real connect: the opt-out is proven without sending a packet.
    seen = []
    monkeypatch.setattr(conftest, "_real_connect", lambda sock, address: seen.append(address))
    monkeypatch.setattr(conftest, "_real_connect_ex", lambda sock, address: seen.append(address) or 0)
    with socket.socket() as sock:
        sock.connect(("203.0.113.9", 80))
        code = sock.connect_ex(("203.0.113.9", 81))
    assert code == 0
    assert seen == [("203.0.113.9", 80), ("203.0.113.9", 81)]
