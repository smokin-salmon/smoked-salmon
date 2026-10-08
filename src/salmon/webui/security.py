"""Which requests ``salmon web`` accepts, by their Host and Origin headers (ADR 0004, section 3).

Plain functions of header values, so they can be checked without a server. The middleware in ``server.py`` applies
them to every request; the job websocket checks its ``Origin`` with ``origin_allowed`` too.
"""

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# The Vite dev server (npm run dev in webui/), accepted only with --dev.
DEV_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_loopback(host: str) -> bool:
    """Whether a bind address only accepts connections from this machine."""
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_host(value: str) -> tuple[str, int | None]:
    """A Host header's name and port: ``example.org:80``, ``[::1]:55155``, ``127.0.0.1``.

    Raises:
        ValueError: The value is not a host with an optional port.
    """
    value = value.strip().lower()
    if value.startswith("["):
        name, sep, rest = value[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            raise ValueError(value)
        port = rest[1:] if rest else None
    else:
        name, sep, port = value.partition(":")
        if not sep:
            port = None
    if not name:
        raise ValueError(value)
    if port is None:
        return name, None
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(value)
    return name, int(port)


def host_allowed(host_header: str | None, bind_host: str, allowed_hosts: Iterable[str]) -> bool:
    """Whether a request's Host is a loopback name, the bind address or an allowed host (DNS rebinding)."""
    if not host_header:
        return False
    try:
        name, _port = split_host(host_header)
    except ValueError:
        return False
    return name in LOOPBACK_NAMES or name == _bind_name(bind_host) or name in _names(allowed_hosts)


def _bind_name(bind_host: str) -> str | None:
    """The bind address as a Host name; none for 0.0.0.0 or ::, which name no machine."""
    name = bind_host.strip("[]").lower()
    try:
        if ipaddress.ip_address(name).is_unspecified:
            return None
    except ValueError:
        pass
    return name


def _names(hosts: Iterable[str]) -> set[str]:
    """The names of ``allowed_hosts`` entries, which may carry a port."""
    names = set()
    for host in hosts:
        try:
            names.add(split_host(host)[0])
        except ValueError:
            names.add(host.strip("[]").lower())
    return names


def origin_allowed(origin: str | None, host_header: str | None, dev: bool = False) -> bool:
    """Whether a request comes from the server's own origin, when the browser says where it comes from.

    The scheme is not compared: behind a TLS reverse proxy the page is https and the request reaches salmon as http.
    The proxy must pass the Host header through, which the port is compared against with the Origin scheme's default.
    """
    if origin is None:
        return True
    if dev and origin in DEV_ORIGINS:
        return True
    if not host_header:
        return False
    try:
        parts = urlsplit(origin)
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return False
    try:
        name, port = split_host(host_header)
    except ValueError:
        return False
    default = _DEFAULT_PORTS[parts.scheme]
    return (parts.hostname, origin_port or default) == (name, port or default)
