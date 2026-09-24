"""Image hosts and metadata sources go through their service's proxy (#406).

Each image host's pooled and per-upload sessions, and each store's sessions, get the proxy set for
their service. Tests talk only to local fakes: a store's request goes to a local proxy that logs the
site asked for and refuses it, and a session made without a proxy fails before it can connect.
"""

import asyncio
import contextlib
import gc
import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import anyio
import pytest
from aiohttp import web
from aiohttp_socks import ProxyConnector
from fake_proxy import FakeProxy

import salmon.images as images
import salmon.search
import salmon.sources
import salmon.tagger.sources
from salmon import cfg
from salmon.config.validations import ProxyCfg, ProxyServicesCfg
from salmon.images import red
from salmon.images.base import UPLOAD_TIMEOUT
from salmon.sources import (
    AppleMusicBase,
    BandcampBase,
    BeatportBase,
    DeezerBase,
    DiscogsBase,
    QobuzBase,
    TidalBase,
)
from salmon.sources.base import BaseScraper
from salmon.sources.beatport import TokenStorage
from salmon.sources.musicbrainz import MusicBrainzBase

SERVICES = ProxyServicesCfg.__struct_fields__

# Image hosts that send nothing through BaseImageUploader's sessions: pyimgbox makes its own
# (it ignores the proxy), and the RED host uploads through a RED tracker client.
NOT_THROUGH_OWN_SESSION = {"imgbox", "red"}


def _subclasses(cls: type) -> set[type]:
    return {sub for direct in cls.__subclasses__() for sub in {direct, *_subclasses(direct)}}


def test_every_scraper_names_its_proxy_service() -> None:
    for package in (salmon.sources, salmon.search, salmon.tagger.sources):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            importlib.import_module(module.name)
    scrapers = _subclasses(BaseScraper)
    assert len(scrapers) > 20
    for scraper in scrapers:
        if issubclass(scraper, MusicBrainzBase):
            # musicbrainzngs, not aiohttp: not proxied.
            assert scraper.proxy_service is None
        else:
            assert scraper.proxy_service in SERVICES, scraper


def test_every_image_host_names_its_proxy_service() -> None:
    for name, module in images.HOSTS.items():
        expected = None if name in NOT_THROUGH_OWN_SESSION else name
        assert module.ImageUploader.proxy_service == expected
        assert expected is None or expected in SERVICES


def _only(monkeypatch: pytest.MonkeyPatch, service: str, url: str) -> None:
    """Give `service` the proxy `url`, and every other service one that must not be used."""
    others = dict.fromkeys(SERVICES, "socks5://127.0.0.1:9")
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(**{**others, service: url})))


@pytest.fixture
def sessions_need_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a session made without a proxy connector, before it can connect anywhere."""
    real_init = aiohttp.ClientSession.__init__

    def init(self: aiohttp.ClientSession, *args: Any, **kwargs: Any) -> None:
        if not isinstance(kwargs.get("connector"), ProxyConnector):
            raise AssertionError("a session without a proxy")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", init)


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg.metadata.tidal, "client_id", "an-id")
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", "a-secret")
    monkeypatch.setattr(cfg.metadata.beatport, "username", "a-user")
    monkeypatch.setattr(cfg.metadata.beatport, "password", "a-password")
    monkeypatch.setattr(TidalBase, "_access_token", None)
    monkeypatch.setattr(AppleMusicBase, "_token", None)


_TOKENS = TokenStorage(bearer="b", expires=0, refresh_token="r", client_id="c")

# Each place a store opens a session: (service, what opens it, the site it asks for).
CALLS: list[tuple[str, Callable[[], Awaitable[Any]], str]] = [
    ("qobuz", lambda: QobuzBase().get_json("/album/get"), "www.qobuz.com"),
    ("tidal", lambda: TidalBase._ensure_token(), "auth.tidal.com"),
    ("bandcamp", lambda: BandcampBase().fetch_page("https://bandcamp.com/search/"), "bandcamp.com"),
    ("deezer", lambda: DeezerBase().get_json("/album/1"), "api.deezer.com"),
    ("deezer", lambda: DeezerBase()._ensure_api_token(), "www.deezer.com"),
    ("deezer", lambda: DeezerBase().get_internal_api_data("/ajax/gw-light.php"), "www.deezer.com"),
    ("beatport", lambda: BeatportBase().refresh_bearer_token(_TOKENS), "api.beatport.com"),
    ("beatport", lambda: BeatportBase().initial_setup(), "api.beatport.com"),
    ("discogs", lambda: DiscogsBase().get_json("/releases/1"), "api.discogs.com"),
    ("apple_music", lambda: AppleMusicBase._get_token(), "music.apple.com"),
]


@pytest.mark.parametrize(("service", "call", "site"), CALLS)
@pytest.mark.usefixtures("sessions_need_a_proxy")
def test_a_store_request_goes_through_its_services_proxy(
    monkeypatch: pytest.MonkeyPatch, service: str, call: Callable[[], Awaitable[Any]], site: str
) -> None:
    _set_credentials(monkeypatch)
    proxy = FakeProxy("socks5", refuse=True)

    async def main() -> None:
        _only(monkeypatch, service, await proxy.start())
        try:
            with contextlib.suppress(Exception):
                await call()
        finally:
            await proxy.stop()

    anyio.run(main)
    assert proxy.targets[:1] == [(site, 443)]
    assert proxy.by_name[:1] == [True]


class FakeHost:
    """A local image host answering as catbox does, counting the connections made to it."""

    def __init__(self) -> None:
        self.received = 0
        self.connections: set[int] = set()

    async def start(self) -> int:
        app = web.Application()
        app.router.add_post("/{tail:.*}", self._upload)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return self.runner.addresses[0][1]

    async def _upload(self, request: web.Request) -> web.Response:
        await request.post()
        assert request.transport is not None
        self.connections.add(request.transport.get_extra_info("peername")[1])
        self.received += 1
        await asyncio.sleep(0.05)
        return web.Response(text=f"https://img.example/{self.received}.png")


def _run_with_host(
    monkeypatch: pytest.MonkeyPatch, body: Callable[[FakeHost, FakeProxy], Awaitable[None]], proxied: bool
) -> None:
    """Run a test against a fake catbox named under .invalid, through a proxy, then check nothing was left open."""
    unclosed: list[str] = []
    monkeypatch.setattr(images.click, "secho", lambda *args, **kwargs: None)
    real_post = aiohttp.ClientSession.post

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        host, proxy = FakeHost(), FakeProxy("socks5")
        port = await host.start()
        url = await proxy.start()
        if proxied:
            _only(monkeypatch, "catbox", url)
        else:
            # Every service but catbox has this proxy.
            monkeypatch.setattr(
                cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(**{**dict.fromkeys(SERVICES, url), "catbox": ""}))
            )
        local = f"http://{'images.invalid' if proxied else '127.0.0.1'}:{port}"

        def local_post(self: aiohttp.ClientSession, target: str, *args: Any, **kwargs: Any) -> Any:
            assert target.startswith("https://catbox.moe/"), target
            return real_post(self, target.replace("https://catbox.moe", local, 1), *args, **kwargs)

        monkeypatch.setattr(aiohttp.ClientSession, "post", local_post)
        try:
            await body(host, proxy)
        finally:
            await proxy.stop()
            await host.runner.cleanup()
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []


def _images(tmp_path: Path, count: int) -> tuple[str, ...]:
    paths = []
    for index in range(count):
        path = tmp_path / f"image{index}.png"
        path.write_bytes(b"png")
        paths.append(str(path))
    return tuple(paths)


@pytest.mark.parametrize("proxied", [True, False])
def test_a_batch_of_images_goes_through_the_hosts_proxy_on_the_same_few_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxied: bool
) -> None:
    monkeypatch.setattr(images, "UPLOAD_CONNECTIONS", 2)
    paths = _images(tmp_path, 6)

    async def body(host: FakeHost, proxy: FakeProxy) -> None:
        urls = await images.upload_images(paths, images.HOSTS["catbox"])
        assert len(urls) == 6
        assert host.received == 6
        # Two reused connections, each a tunnel through the proxy when there is one.
        assert len(host.connections) == 2
        assert proxy.connections == (2 if proxied else 0)
        assert set(proxy.targets) <= {("images.invalid", host.runner.addresses[0][1])}

    _run_with_host(monkeypatch, body, proxied)


@pytest.mark.parametrize("proxied", [True, False])
def test_a_single_upload_goes_through_the_hosts_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxied: bool
) -> None:
    (path,) = _images(tmp_path, 1)

    async def body(host: FakeHost, proxy: FakeProxy) -> None:
        # Outside a batch: a session for this upload alone.
        await images.HOSTS["catbox"].ImageUploader().upload_file(path)
        assert host.received == 1
        assert proxy.connections == (1 if proxied else 0)

    _run_with_host(monkeypatch, body, proxied)


@pytest.mark.parametrize("proxied", [True, False])
@pytest.mark.parametrize("host", sorted(set(images.HOSTS) - NOT_THROUGH_OWN_SESSION))
def test_an_image_hosts_sessions_keep_their_arguments(
    monkeypatch: pytest.MonkeyPatch, host: str, proxied: bool
) -> None:
    _only(monkeypatch, host, "socks5://127.0.0.1:1080" if proxied else "")
    expected = ProxyConnector if proxied else aiohttp.TCPConnector

    async def main() -> None:
        uploader = images.HOSTS[host].ImageUploader()
        async with uploader.connections(3), uploader._http_session() as pooled:
            assert isinstance(pooled.connector, expected)
            assert pooled.connector.limit == 3
            assert pooled.timeout is UPLOAD_TIMEOUT
        async with uploader._http_session() as own:
            assert isinstance(own.connector, expected)
            assert own.timeout is UPLOAD_TIMEOUT
            if not proxied:
                assert type(own.connector) is aiohttp.TCPConnector

    anyio.run(main)


def test_the_red_image_host_uploads_through_reds_tracker_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    _only(monkeypatch, "red", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(cfg.tracker.red, "api_key", "a-key")

    async def main() -> None:
        uploader = red.ImageUploader()
        session = uploader.api._http_session()
        assert isinstance(session.connector, ProxyConnector)
        assert session.connector._proxy_port == 1080
        await uploader.api.close()

    anyio.run(main)
