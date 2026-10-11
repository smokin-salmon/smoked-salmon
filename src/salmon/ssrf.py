"""salmon web connects to public addresses only, when it fetches a URL it did not choose (#656).

A salmon web job fetches URLs that come from outside salmon: the URLs a browser pastes as metadata sources, the
cover URL a store's page gives, a page an AI review cites. The server's network is often not the browser's (a
seedbox, a NAS, a container), so such a URL could reach what only the server can: a cloud metadata endpoint, a
router, another service on its LAN. While a web job or a web route runs, each connection a store, cover or page
fetch opens refuses any address that is not public. The check is made on the address the connection goes to, as
the connector resolves it, on every connection and so on every redirect hop: not on the URL before the request,
since a name can resolve differently twice and a public page can redirect inward. A refused connection raises
NonPublicAddressError, an aiohttp.ClientError, which callers already handle as a failed request.

The CLI fetches what its user gives it, as before: the guard is on only inside ``public_only()``.

The address rules and the check at the connector are chodeus's (9f576258, 05d4aacd, 43a0197c, dd2489c9).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from aiohttp.abc import ResolveResult
    from aiohttp.tracing import Trace

_on: ContextVar[bool] = ContextVar("public_only", default=False)

# Addresses let through all the same: none in salmon. The test suite's fakes listen on loopback, and a test that
# needs one sets this. Not a setting on purpose: nothing a user could turn on by accident.
ALLOWED: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

# Refused even when is_global holds: is_global lets NAT64 (64:ff9b::/96) through, which is_reserved refuses.
_NON_PUBLIC = ("is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified")


@contextmanager
def public_only() -> Iterator[None]:
    """Refuse every non-public address the block connects to, and every task it starts."""
    token = _on.set(True)
    try:
        yield
    finally:
        _on.reset(token)


def active() -> bool:
    """Whether connections made now must go to public addresses."""
    return _on.get()


def is_public(address: str) -> bool:
    """Whether `address` (an IP address, as text) is one salmon web may connect to.

    Only a globally routable address is: loopback, private, link-local (169.254.169.254 among them), CGNAT,
    reserved, multicast and unspecified addresses are refused, in IPv4, IPv6 and IPv4-mapped IPv6. Anything that is
    not an address is refused too.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # Judged as the IPv4 address it reaches: older Pythons judge ::ffff:127.0.0.1 by its IPv6 form.
        ip = ip.ipv4_mapped
    if any(ip in network for network in ALLOWED):
        return True
    return ip.is_global and not any(getattr(ip, attr) for attr in _NON_PUBLIC)


class NonPublicAddressError(aiohttp.ClientConnectionError):
    """A connection refused because it would go to an address that is not public.

    The message names the host as the URL gave it, never the addresses it resolved to.
    """

    def __init__(self, host: str) -> None:
        super().__init__(f"Refused to connect to {host}: salmon web only fetches from public addresses.")
        self.host = host


def check(host: str, addresses: Iterable[str]) -> None:
    """Refuse `host` unless every address it resolved to is public.

    Raises:
        NonPublicAddressError: One address is not public.
    """
    if not all(is_public(address) for address in addresses):
        raise NonPublicAddressError(host)


async def local_addresses(host: str, port: int) -> list[str]:
    """What `host` resolves to on this machine; empty if it does not resolve here."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


async def check_for_proxy(host: str, port: int) -> None:
    """Refuse `host`, which a proxy will resolve, if it is a non-public address or resolves to one here.

    The proxy resolves the names it is given, possibly not as this machine does: a name resolving to a public
    address here and to a private one at the proxy is not refused, nor a name that does not resolve here.

    Raises:
        NonPublicAddressError: `host` is not public, or one of the addresses it resolves to here is not.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        check(host, await local_addresses(host, port))
    else:
        check(host, [host])


class PublicOnlyConnector(aiohttp.TCPConnector):
    """A direct connector refusing every address that is not public, on each connection it opens."""

    async def _resolve_host(
        self, host: str, port: int, traces: "Sequence[Trace] | None" = None
    ) -> "list[ResolveResult]":
        # Here, not in a resolver: the connector does not ask its resolver about an address the URL gives as is.
        # The connection goes to the addresses returned, and only to them.
        results = await super()._resolve_host(host, port, traces)
        check(host, (result["host"] for result in results))
        return results
