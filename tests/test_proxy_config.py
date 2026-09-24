"""The [proxy] config section: which proxy each service gets, and which URLs are refused (#406).

Without a proxy, every service must connect as it did before proxies existed. These tests build
configs and connectors only: nothing here opens a connection.
"""

import re
import traceback
from pathlib import Path

import aiohttp
import anyio
import msgspec
import pytest
from aiohttp_socks import ProxyConnector

from salmon import cfg, proxy
from salmon.config import _parse_config
from salmon.config.validations import ProxyCfg, ProxyServicesCfg

_DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"

SERVICES = ProxyServicesCfg.__struct_fields__


def _parse(tmp_path: Path, section: str) -> ProxyCfg:
    """Load the shipped config with `section` appended, as a user's config would be."""
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir()
    torrents.mkdir()
    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    text = text.replace("download_directory = '.music'", f"download_directory = '{music.as_posix()}'")
    text = text.replace("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{torrents.as_posix()}'")
    path = tmp_path / "config.toml"
    path.write_text(text + "\n" + section, encoding="utf-8")
    return _parse_config(path).proxy


@pytest.mark.parametrize(
    "section",
    ["", "[proxy]\n", '[proxy]\nurl = ""\n', '[proxy.services]\nred = ""\nqobuz = ""\n'],
    ids=["no section", "empty section", "empty url", "empty services"],
)
def test_without_a_proxy_every_service_connects_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    monkeypatch.setattr(cfg, "proxy", _parse(tmp_path, section))

    async def main() -> None:
        for service in SERVICES:
            assert proxy.proxy_url(service) is None
            # A session gets aiohttp's own default connector, as it did before.
            assert proxy.session_kwargs(service) == {}
            # A pool keeps its plain connector, with its limit.
            connector = proxy.connector(service, limit=2)
            assert type(connector) is aiohttp.TCPConnector
            assert connector.limit == 2
            await connector.close()

    anyio.run(main)


_SHIPPED_PROXY_SECTION = _DEFAULT_CONFIG.read_text(encoding="utf-8").split("# PROXY SETTINGS", 1)[1]


def _uncommented_proxy_section() -> str:
    """What a user uncomments: the tables and settings, not the comments explaining them."""
    lines = _SHIPPED_PROXY_SECTION.splitlines()
    return "\n".join(line[2:] for line in lines if re.match(r"# (\[|\w+ = )", line))


def test_the_shipped_proxy_section_uncommented_with_a_url_proxies_every_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user sets the url to their proxy, and leaves the rest as shipped.
    section, count = re.subn(
        r"^url = .*$", 'url = "socks5://127.0.0.1:1080"', _uncommented_proxy_section(), flags=re.MULTILINE
    )
    assert count == 1
    monkeypatch.setattr(cfg, "proxy", _parse(tmp_path, section))
    assert {service: proxy.proxy_url(service) for service in SERVICES} == dict.fromkeys(
        SERVICES, "socks5://127.0.0.1:1080"
    )


def test_the_shipped_proxy_section_uncommented_as_is_is_refused(tmp_path: Path) -> None:
    # Its url is a placeholder: forgetting to set it fails loudly instead of connecting directly.
    with pytest.raises(msgspec.ValidationError, match="proxy.url"):
        _parse(tmp_path, _uncommented_proxy_section())


def test_the_shipped_proxy_section_names_every_service() -> None:
    services = _SHIPPED_PROXY_SECTION.split("# [proxy.services]", 1)[1]
    assert [service for service in SERVICES if not re.search(rf"\b{service}\b", services)] == []


def test_a_services_own_proxy_wins_and_an_empty_one_connects_it_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    services = ProxyServicesCfg(qobuz="http://127.0.0.1:3128", bandcamp="")
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(url="socks5://127.0.0.1:1080", services=services))
    assert proxy.proxy_url("qobuz") == "http://127.0.0.1:3128"
    assert proxy.proxy_url("bandcamp") is None
    assert {proxy.proxy_url(service) for service in SERVICES if service not in ("qobuz", "bandcamp")} == {
        "socks5://127.0.0.1:1080"
    }


def test_a_service_can_have_a_proxy_of_its_own_with_none_for_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(services=ProxyServicesCfg(tidal="socks5://127.0.0.1:1080")))
    assert proxy.proxy_url("tidal") == "socks5://127.0.0.1:1080"
    assert {proxy.proxy_url(service) for service in SERVICES if service != "tidal"} == {None}


@pytest.mark.parametrize("scheme", ["http", "socks4", "socks4a", "socks5", "socks5h", "SOCKS5"])
def test_a_proxy_connector_keeps_the_pools_arguments(monkeypatch: pytest.MonkeyPatch, scheme: str) -> None:
    monkeypatch.setattr(cfg, "proxy", ProxyCfg(url=f"{scheme}://user:p%40ss@127.0.0.1:1080"))

    async def main() -> None:
        connector = proxy.connector("red", limit=2)
        assert isinstance(connector, ProxyConnector)
        assert connector.limit == 2
        await connector.close()
        (session_connector,) = proxy.session_kwargs("qobuz").values()
        assert isinstance(session_connector, ProxyConnector)
        await session_connector.close()

    anyio.run(main)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:3128",
        "ftp://127.0.0.1:21",
        "socks5://127.0.0.1",
        "127.0.0.1:1080",
        "socks5://user:hunter2@:1080",
        # An unencoded / cuts the password short, into what urlsplit then reads as a port.
        "socks5://user:hunter2/x@127.0.0.1:1080",
        "socks5://user:hunter2@127.0.0.1:99999",
    ],
)
@pytest.mark.parametrize("key", ["url", "services.qobuz"])
def test_an_unusable_proxy_url_is_refused_without_repeating_it(tmp_path: Path, key: str, url: str) -> None:
    section = f'[proxy]\nurl = "{url}"\n' if key == "url" else f'[proxy.services]\nqobuz = "{url}"\n'
    with pytest.raises(msgspec.ValidationError) as raised:
        _parse(tmp_path, section)
    message = str(raised.value)
    assert f"proxy.{key}" in message
    assert "socks5h" in message
    # The URL may hold the proxy's password: not in the message, nor anywhere in the traceback.
    shown = "".join(traceback.format_exception(raised.value))
    assert "hunter2" not in shown
    assert url not in shown
