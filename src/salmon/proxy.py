"""Send a service's HTTP requests through the proxy the config gives it ([proxy] in config.default.toml).

Opt-in: a service without a proxy gets the same connector and session as it would without this module.

While salmon web's guard is on (``ssrf.public_only()``), the connectors made here refuse non-public addresses, except
those of a service that connects to its own fixed hosts only: a tracker, an image host.
"""

import asyncio
import ssl
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError, ProxyTimeoutError

from salmon import ssrf

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiohttp import ClientRequest, ClientTimeout
    from aiohttp.abc import ResolveResult
    from aiohttp.client_proto import ResponseHandler
    from aiohttp.tracing import Trace

# The proxy URL schemes salmon takes, each mapped to the one aiohttp_socks knows it by. The proxy
# always resolves hostnames, so the name of the site a request goes to is not looked up locally
# (but by salmon web's guard, see _PublicOnlyProxyConnector):
# socks4 works as SOCKS4A, and socks5 as socks5h.
SCHEMES = {"http": "http", "socks4": "socks4", "socks4a": "socks4", "socks5": "socks5", "socks5h": "socks5"}


def is_valid_url(url: str) -> bool:
    """Whether `url` is a proxy URL salmon can use: scheme://[user:password@]host:port."""
    try:
        parts = urlsplit(url)
        return parts.scheme in SCHEMES and bool(parts.hostname) and parts.port is not None
    except ValueError:  # An invalid port or IPv6 address
        return False


def proxy_url(service: str | None) -> str | None:
    """Get the URL of the proxy for `service`, or None to connect directly.

    The service's own setting wins, and an empty one connects it directly; else [proxy] url applies.

    Args:
        service: The service's key in [proxy.services], or None for traffic no proxy setting covers.
    """
    if service is None:
        return None
    # Not at import time: salmon.config imports this module to validate the config cfg is built from.
    from salmon import cfg

    url = getattr(cfg.proxy.services, service)
    if url is None:
        url = cfg.proxy.url
    return url or None


def connector(service: str | None, *, fixed_hosts: bool = False, **kwargs: Any) -> aiohttp.TCPConnector:
    """Make a connector for `service`'s requests: through its proxy, else a direct TCPConnector.

    While salmon web's guard is on, the connector refuses non-public addresses (see salmon.ssrf).

    Args:
        service: The service's key in [proxy.services], or None.
        fixed_hosts: For a service that only connects to its own hosts, which salmon chose (a tracker, an image
            host): the guard leaves it alone.
        **kwargs: TCPConnector arguments, such as limit, kept with a proxy too.
    """
    url = proxy_url(service)
    guarded = ssrf.active() and not fixed_hosts
    if url is None:
        return ssrf.PublicOnlyConnector(**kwargs) if guarded else aiohttp.TCPConnector(**kwargs)
    return _proxy_connector(url, guarded, **kwargs)


def session_kwargs(service: str | None, *, fixed_hosts: bool = False) -> dict[str, Any]:
    """Get the ClientSession arguments sending `service`'s requests through its proxy: none without one, unless
    salmon web's guard is on (see salmon.ssrf). `fixed_hosts` as for connector()."""
    if proxy_url(service) is None and (fixed_hosts or not ssrf.active()):
        return {}
    return {"connector": connector(service, fixed_hosts=fixed_hosts)}


def _proxy_connector(url: str, guarded: bool = False, **kwargs: Any) -> ProxyConnector:
    parts = urlsplit(url)
    url = parts._replace(scheme=SCHEMES[parts.scheme]).geturl()
    cls = _PublicOnlyProxyConnector if guarded else _ProxyConnector
    return cls.from_url(url, rdns=True, **kwargs)


class _ProxyConnector(ProxyConnector):
    """A ProxyConnector failing with aiohttp's errors, as a direct connection does.

    aiohttp_socks raises errors of its own, and lets errors from the handshake with the site
    through as they are: none of them is an aiohttp.ClientError, so code catching aiohttp's errors
    would miss them. They all happen before the request is written, so each becomes the error a
    direct connection failing at that step raises: a ConnectionTimeoutError or a
    ClientConnectorError, which the tracker client retries as a request the tracker never got,
    except a ClientConnectorCertificateError, which it does not retry.
    """

    async def _wrap_create_connection(
        self,
        *args: Any,
        addr_infos: Any,
        req: "ClientRequest",
        timeout: "ClientTimeout",
        client_error: type[Exception] = aiohttp.ClientConnectorError,
        **kwargs: Any,
    ) -> tuple[asyncio.Transport, "ResponseHandler"]:
        try:
            return await super()._wrap_create_connection(
                *args, addr_infos=addr_infos, req=req, timeout=timeout, client_error=client_error, **kwargs
            )
        except (ProxyTimeoutError, TimeoutError) as exc:
            raise aiohttp.ConnectionTimeoutError(f"proxy: {exc}") from exc
        except ssl.CertificateError as exc:
            raise aiohttp.ClientConnectorCertificateError(req.connection_key, exc) from exc
        except ssl.SSLError as exc:
            raise aiohttp.ClientConnectorSSLError(req.connection_key, exc) from exc
        except (ProxyConnectionError, ProxyError, OSError, EOFError) as exc:
            raise aiohttp.ClientProxyConnectionError(req.connection_key, OSError(None, f"proxy: {exc}")) from exc


class _PublicOnlyProxyConnector(_ProxyConnector):
    """A proxy connector refusing a site that is not public, for salmon web's guard (see salmon.ssrf).

    The proxy's own address is not checked: it is the user's to choose, and often on their LAN. The proxy resolves
    the site's name, so the name is looked up here first and refused if it resolves to a non-public address here;
    it can still resolve differently at the proxy.
    """

    async def _resolve_host(
        self, host: str, port: int, traces: "Sequence[Trace] | None" = None
    ) -> "list[ResolveResult]":
        # The site as the URL names it: the proxy connector does not resolve it.
        results = await super()._resolve_host(host, port, traces)
        await ssrf.check_for_proxy(host, port)
        return results
