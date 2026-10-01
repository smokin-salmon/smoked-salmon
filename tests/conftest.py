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

import os
import tempfile
from pathlib import Path

_DEFAULT_CONFIG = Path(__file__).parent.parent / "src" / "salmon" / "data" / "config.default.toml"

_root = Path(tempfile.mkdtemp(prefix="salmon-tests-"))
_music = _root / "music"
_torrents = _root / "torrents"
_music.mkdir()
_torrents.mkdir()

_config = _DEFAULT_CONFIG.read_text(encoding="utf-8")
for _old, _new in (
    ("download_directory = '.music'", f"download_directory = '{_music.as_posix()}'"),
    ("dottorrents_dir = '.torrents'", f"dottorrents_dir = '{_torrents.as_posix()}'"),
):
    assert _old in _config, f"default config no longer contains {_old!r}; update tests/conftest.py"
    _config = _config.replace(_old, _new)

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

import ipaddress  # noqa: E402
import socket  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402


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
    if not isinstance(host, str):
        return False
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
    target = ":".join(str(part) for part in parts[:2]) or repr(address)
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


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest):
    global _network_allowed
    _network_allowed = request.node.get_closest_marker("network") is not None
    try:
        yield
    finally:
        _network_allowed = False
