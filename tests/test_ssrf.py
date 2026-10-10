"""salmon web connects to public addresses only, when it fetches a URL it did not choose (#656, salmon.ssrf).

Every fetch here goes to a local fake store on 127.0.0.1, or is refused before it connects: an address the guard
refuses is never connected to, so a test naming 10.0.0.1 or 169.254.169.254 would fail on the network guard in
conftest.py (NetworkBlockedError), not pass, if the guard let it through. Names resolve through fakes, never DNS.
"""

import contextlib
import io
import ipaddress
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import anyio
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from fake_proxy import FakeProxy  # pyright: ignore[reportMissingImports]
from PIL import Image

from salmon import cfg, proxy, ssrf
from salmon.config.validations import ProxyCfg, ProxyServicesCfg
from salmon.errors import ScrapeError
from salmon.sources import BandcampBase
from salmon.tagger import ai_review, cover, meta

# Every kind of address the guard refuses, as a URL names it.
REFUSED = [
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.1.1",
    "169.254.169.254",
    "fe80::1",
    "fc00::1",
    "fd12:3456::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "0.0.0.0",
    "::",
    "100.64.0.1",
    "224.0.0.1",
    "ff02::1",
    "255.255.255.255",
    "64:ff9b::a00:1",
    "192.0.2.1",
    "2001:db8::1",
]

LOOPBACK = (ipaddress.ip_network("127.0.0.1/32"),)


def _png() -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(data, "PNG")
    return data.getvalue()


def bandcamp_page(cover_url: str = "") -> str:
    """An album page as Bandcamp's scraper reads it."""
    return f"""<html><body>
      <div id="name-section"><div class="trackTitle">The Album</div><h3>by <span>The Artist</span></h3></div>
      <div id="tralbumArt"><img src="{cover_url}"></div>
      <div class="tralbumData tralbum-credits">released March 4, 2022</div>
    </body></html>"""


class FakeStore:
    """A store on 127.0.0.1: album pages at /album/<name>, a cover at /cover.png, a redirect to ?to= at /moved.

    Every request it gets is kept, by path.
    """

    def __init__(self, page: str | None = None) -> None:
        self.requests: list[str] = []
        self.page = page

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator["FakeStore"]:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._answer)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.port = runner.addresses[0][1]
        try:
            yield self
        finally:
            await runner.cleanup()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.port}{path}"

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request.path)
        if request.path == "/moved":
            raise web.HTTPFound(request.query["to"])
        if request.path == "/cover.png":
            return web.Response(body=_png(), content_type="image/png")
        return web.Response(text=self.page or bandcamp_page(), content_type="text/html")


class FakeResolver(AbstractResolver):
    """Resolves the names given, to the addresses given, and nothing else."""

    def __init__(self, names: dict[str, list[str]]) -> None:
        self.names = names

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host not in self.names:
            raise OSError(f"{host} does not resolve here")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in self.names[host]
        ]

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "proxy", ProxyCfg())


def _run(test: Any, store: FakeStore | None = None) -> None:
    store = store or FakeStore()

    async def main() -> None:
        async with store.serving():
            await test(store)

    anyio.run(main)


async def _get(url: str, names: dict[str, list[str]]) -> str:
    """GET `url` through a guarded connector resolving `names`, following redirects."""
    connector = ssrf.PublicOnlyConnector(resolver=FakeResolver(names))
    async with aiohttp.ClientSession(connector=connector) as session, session.get(url) as response:
        return await response.text()


# --- The address rules ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", REFUSED)
def test_every_internal_address_is_refused(address: str) -> None:
    assert not ssrf.is_public(address)


# Judged only, never connected to.
@pytest.mark.parametrize("address", ["1.1.1.1", "2606:4700:4700::1111", "::ffff:1.1.1.1"])
def test_a_global_address_is_public(address: str) -> None:
    assert ssrf.is_public(address)


@pytest.mark.parametrize("text", ["", "store.test", "127.1", "999.0.0.1"])
def test_what_is_not_an_address_is_refused(text: str) -> None:
    assert not ssrf.is_public(text)


def test_only_the_allowed_addresses_are_let_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    assert ssrf.is_public("127.0.0.1")
    assert ssrf.is_public("::ffff:127.0.0.1")
    assert not ssrf.is_public("127.0.0.2")
    assert not ssrf.is_public("::1")


# --- At the connection -----------------------------------------------------------------------------


@pytest.mark.parametrize("address", REFUSED)
def test_a_store_fetch_refuses_an_internal_address_before_connecting(address: str) -> None:
    async def test(store: FakeStore) -> None:
        with ssrf.public_only(), pytest.raises(ScrapeError) as refused:
            await BandcampBase().fetch_page(store.url("/album/x", address))
        assert f"Refused to connect to {address}" in str(refused.value)
        assert store.requests == []

    _run(test)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "fe80::1", "::ffff:127.0.0.1"])
def test_a_name_resolving_to_an_internal_address_is_refused_by_name(address: str) -> None:
    async def test(store: FakeStore) -> None:
        with pytest.raises(ssrf.NonPublicAddressError) as refused:
            await _get(store.url("/album/x", "store.test"), {"store.test": [address]})
        # The host as the URL names it, not where it resolved.
        assert str(refused.value).startswith("Refused to connect to store.test:")
        assert address not in str(refused.value)
        assert store.requests == []

    _run(test)


def test_a_name_resolving_to_one_internal_address_among_others_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(store: FakeStore) -> None:
        with pytest.raises(ssrf.NonPublicAddressError):
            await _get(store.url("/album/x", "store.test"), {"store.test": ["127.0.0.1", "10.0.0.1"]})
        assert store.requests == []

    _run(test)


@pytest.mark.parametrize(
    ("target", "refused_host"),
    [
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("http://10.0.0.1/admin", "10.0.0.1"),
        ("http://[::1]:{port}/album/x", "::1"),
        ("http://inner.test:{port}/album/x", "inner.test"),
    ],
)
def test_a_public_page_redirecting_inward_is_refused_at_the_second_hop(
    monkeypatch: pytest.MonkeyPatch, target: str, refused_host: str
) -> None:
    # store.test stands for a public store: its address is let through.
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)
    names = {"store.test": ["127.0.0.1"], "inner.test": ["192.168.1.1"]}

    async def test(store: FakeStore) -> None:
        to = target.format(port=store.port)
        with pytest.raises(ssrf.NonPublicAddressError) as refused:
            await _get(store.url(f"/moved?to={to}", "store.test"), names)
        assert refused.value.host == refused_host
        # The first hop reached the store, the second nothing.
        assert store.requests == ["/moved"]

    _run(test)


def test_an_allowed_address_is_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "ALLOWED", LOOPBACK)

    async def test(store: FakeStore) -> None:
        with ssrf.public_only():
            await BandcampBase().fetch_page(store.url("/album/x"))
        assert store.requests == ["/album/x"]

    _run(test)


def test_a_json_api_fetch_refused_names_the_host() -> None:
    async def test(store: FakeStore) -> None:
        with ssrf.public_only(), pytest.raises(ScrapeError) as refused:
            await BandcampBase().get_json(store.url("/api"))
        assert "Refused to connect to 127.0.0.1" in str(refused.value)
        assert store.requests == []

    _run(test)


# --- Through a proxy ------------------------------------------------------------------------------


def _through(monkeypatch: pytest.MonkeyPatch, url: str, here: dict[str, list[str]]) -> None:
    """Send Bandcamp's requests through the proxy at `url`; names resolve on this machine as `here` says."""
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(bandcamp=url)))

    async def local_addresses(host: str, _port: int) -> list[str]:
        return here.get(host, [])

    monkeypatch.setattr(ssrf, "local_addresses", local_addresses)


@pytest.mark.parametrize(
    ("host", "here"),
    [
        ("store.invalid", {"store.invalid": ["10.0.0.1"]}),
        ("store.invalid", {"store.invalid": ["1.1.1.1", "169.254.169.254"]}),
        ("127.0.0.1", {}),
        ("::ffff:127.0.0.1", {}),
    ],
)
def test_with_a_proxy_a_site_internal_here_is_refused_before_the_proxy_is_asked(
    monkeypatch: pytest.MonkeyPatch, host: str, here: dict[str, list[str]]
) -> None:
    fake_proxy = FakeProxy("socks5")

    async def test(store: FakeStore) -> None:
        _through(monkeypatch, await fake_proxy.start(), here)
        try:
            with ssrf.public_only(), pytest.raises(ScrapeError) as refused:
                await BandcampBase().fetch_page(store.url("/album/x", host))
        finally:
            await fake_proxy.stop()
        assert f"Refused to connect to {host}" in str(refused.value)
        assert fake_proxy.connections == 0
        assert store.requests == []

    _run(test)


@pytest.mark.parametrize("here", [{"store.invalid": ["1.1.1.1"]}, {}], ids=["public here", "unknown here"])
def test_with_a_proxy_on_loopback_a_public_site_goes_through_it(
    monkeypatch: pytest.MonkeyPatch, here: dict[str, list[str]]
) -> None:
    # The proxy's own address is the user's choice, often on their LAN: never refused. A name this machine cannot
    # resolve is left to the proxy, as is a name the proxy resolves differently: neither is covered.
    fake_proxy = FakeProxy("socks5")

    async def test(store: FakeStore) -> None:
        _through(monkeypatch, await fake_proxy.start(), here)
        try:
            with ssrf.public_only():
                await BandcampBase().fetch_page(store.url("/album/x", "store.invalid"))
        finally:
            await fake_proxy.stop()
        assert fake_proxy.targets == [("store.invalid", store.port)]
        assert fake_proxy.by_name == [True]
        assert store.requests == ["/album/x"]

    _run(test)


# --- Where the guard is off, and what it leaves alone ------------------------------------------------


def test_the_cli_fetches_what_its_user_gives_it(capsys: pytest.CaptureFixture[str]) -> None:
    async def test(store: FakeStore) -> None:
        url = store.url("/album/x")
        assert meta.callback is not None
        await meta.callback(url)
        assert store.requests == ["/album/x"]
        assert "The Album" in capsys.readouterr().out
        # The same URL in salmon web is refused.
        with ssrf.public_only(), pytest.raises(ScrapeError):
            await BandcampBase().fetch_page(url)
        assert store.requests == ["/album/x"]

    _run(test)


def test_without_the_guard_connectors_are_as_before() -> None:
    assert proxy.session_kwargs("bandcamp") == {}
    assert proxy.session_kwargs(None) == {}

    async def main() -> None:
        connector = proxy.connector("catbox", limit=2)
        assert type(connector) is aiohttp.TCPConnector
        await connector.close()

    anyio.run(main)


def test_a_tracker_pool_and_an_image_host_are_never_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    # They connect to their own hosts only: with a proxy, no name is looked up here either.
    looked_up: list[str] = []

    async def local_addresses(host: str, _port: int) -> list[str]:
        looked_up.append(host)
        return []

    monkeypatch.setattr(ssrf, "local_addresses", local_addresses)

    async def main() -> None:
        with ssrf.public_only():
            tracker = proxy.connector("red", fixed_hosts=True, limit=2)
            image_host = proxy.session_kwargs("catbox", fixed_hosts=True)
            store = proxy.connector("bandcamp")
        assert type(tracker) is aiohttp.TCPConnector
        assert image_host == {}
        assert isinstance(store, ssrf.PublicOnlyConnector)
        await tracker.close()
        await store.close()

    anyio.run(main)
    assert looked_up == []


def test_the_guard_ends_with_its_block() -> None:
    with ssrf.public_only():
        assert ssrf.active()
    assert not ssrf.active()


# --- Covers and the pages an AI review cites ---------------------------------------------------------


def test_a_cover_url_is_refused_and_the_run_goes_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def test(store: FakeStore) -> None:
        with ssrf.public_only():
            assert await cover._download_cover(str(tmp_path), store.url("/cover.png")) is None
        assert "Failed to download cover image (ERROR Refused to connect to 127.0.0.1" in capsys.readouterr().out
        assert store.requests == []
        # The CLI downloads it.
        assert await cover._download_cover(str(tmp_path), store.url("/cover.png")) is not None
        assert store.requests == ["/cover.png"]

    _run(test)


def test_a_cited_page_is_refused_in_salmon_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_review, "_page_texts", {})
    page = "<p>Released by Example Label.</p>"

    async def test(store: FakeStore) -> None:
        url = store.url("/release")
        with ssrf.public_only():
            assert not await ai_review._url_explicitly_names_label(url, "Example Label")
        assert store.requests == []
        assert await ai_review._url_explicitly_names_label(url, "Example Label")
        assert store.requests == ["/release"]

    _run(test, FakeStore(page))
