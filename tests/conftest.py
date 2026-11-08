"""Give the test run its own configuration.

``salmon`` loads and validates its config at import time, and exits when none
is found. Point it at a copy of the default config, with its directories moved
into a temporary location, before any test module imports the package.

This also keeps a developer's real config out of the test run: it sets
``SALMON_CONFIG_DIR`` to that temporary location, which ``find_config_path()``
prefers over a repo-root ``config.toml`` (a documented dev convenience) since
an explicitly set ``SALMON_CONFIG_DIR`` beats that implicit fallback. It also
sets ``XDG_CONFIG_HOME`` for anything that reads the platform config dir
directly, but that alone would not stop a repo-root ``config.toml`` or an
already-set ``SALMON_CONFIG_DIR`` from winning.
"""

import atexit
import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# A dependency: if it does not load, the run fails here rather than going on without its network guard (below).
import curl_cffi
import pytest
from helpers import servers
from helpers.shipped_config import shipped_config

# A test server stopped as a client connects would otherwise fail the run at random, from garbage collection, on
# Python 3.13 and 3.14 (tests/helpers/servers.py).
servers.install()

_root = Path(tempfile.mkdtemp(prefix="salmon-tests-"))
# Each process removes its own: with xdist, the controller and every worker load this file.
atexit.register(shutil.rmtree, _root, ignore_errors=True)
_music = _root / "music"
_torrents = _root / "torrents"
_music.mkdir()
_torrents.mkdir()

_config = shipped_config(_music, _torrents)

_config_dir = _root / "config" / "smoked-salmon"
_config_dir.mkdir(parents=True)
(_config_dir / "config.toml").write_text(_config, encoding="utf-8")

os.environ["XDG_CONFIG_HOME"] = str(_root / "config")
os.environ["SALMON_CONFIG_DIR"] = str(_config_dir)


# --- Network guard -----------------------------------------------------------
# A test that reaches a real service (an image host, a store, a tracker) is a
# bug: it must run against a local fake. Refuse every connection that is not to
# loopback or a unix socket, at the socket level so aiohttp, requests, urllib3
# and httpx are all covered. Tests that truly need the network opt out with
# ``@pytest.mark.network``; CI deselects them.
#
# ``socket.getaddrinfo`` is not guarded on purpose: a name only a local fake
# proxy resolves (tests/test_trackers_proxy.py) must keep working, aiohttp may
# resolve through c-ares instead, and the connect guard names the address anyway.


class NetworkBlockedError(OSError):
    """A test tried to connect to a non-loopback address."""


_network_allowed = False
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(sock: socket.socket, address: Any) -> bool:
    if sock.family == socket.AF_UNIX:
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return isinstance(host, str) and _is_local_host(host)


def _is_local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _check(sock: socket.socket, address: Any) -> None:
    if _network_allowed or _is_local(sock, address):
        return
    parts = tuple(address) if isinstance(address, tuple) else ()
    _refuse(":".join(str(part) for part in parts[:2]) or repr(address))


def _refuse(target: str) -> None:
    raise NetworkBlockedError(
        f"test tried to connect to {target}, which is not loopback. Give the test a local fake "
        "server, or mark it @pytest.mark.network if it really needs the network."
    )


def _guarded_connect(self: socket.socket, address, /):
    _check(self, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address, /):
    _check(self, address)
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


# curl_cffi (Bandcamp's browser retry) connects through libcurl's own sockets, which the guard above does not see.
# A Session or AsyncSession request is refused before it starts, by the host it would connect to first (its websocket
# and the low-level Curl are not guarded: nothing uses them).
def _curl_target(session: Any, args: tuple, kwargs: dict[str, Any]) -> str:
    """The proxy a curl_cffi request goes through, chosen as curl_cffi and libcurl choose it, else its URL."""
    url = kwargs.get("url", args[1] if len(args) > 1 else "")
    scheme = urlsplit(url).scheme
    proxies = {"all": kwargs["proxy"]} if kwargs.get("proxy") else kwargs.get("proxies")
    if proxies is None:
        proxies = getattr(session, "proxies", None) or {}
    proxy = proxies.get(scheme, proxies.get("all"))
    if proxy is None:
        # libcurl then takes one from the environment (NO_PROXY left out: the guard refuses rather than misses).
        upper = [f"{scheme.upper()}_PROXY"] if scheme != "http" else []  # libcurl ignores HTTP_PROXY
        names = [f"{scheme}_proxy", *upper, "all_proxy", "ALL_PROXY"]
        proxy = next((os.environ[name] for name in names if os.environ.get(name)), "")
    return proxy or url


def _guarded_curl_request(real: Any) -> Any:
    def request(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not _network_allowed:
            target = _curl_target(self, args, kwargs)
            # libcurl reads a proxy without a scheme as http://.
            parts = urlsplit(target if "://" in target else f"http://{target}")
            if not _is_local_host(parts.hostname or ""):
                # host:port, without the credentials a proxy URL may hold.
                _refuse(parts.netloc.rpartition("@")[2])
        return real(self, *args, **kwargs)

    return request


for _session_class in (curl_cffi.Session, curl_cffi.AsyncSession):
    _session_class.request = _guarded_curl_request(_session_class.request)  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _spectral_image_check(monkeypatch: pytest.MonkeyPatch):
    """Tests write placeholder bytes as spectrals: upload_spectrals does not decode them. A test of that check
    overrides this fixture with one that does nothing."""
    from salmon.uploader import spectrals

    monkeypatch.setattr(spectrals, "check_image", lambda _path: "PNG")


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest):
    global _network_allowed
    _network_allowed = request.node.get_closest_marker("network") is not None
    try:
        yield
    finally:
        _network_allowed = False
